"""
Annual report intake and processing tools.
"""
import logging
import os
from pathlib import Path
from typing import Optional

from config import settings
from utils.retry import retry_on_transient_error

try:
    import pymupdf4llm
except ImportError:
    pymupdf4llm = None
    
try:
    import fitz
except ImportError:
    fitz = None

logger = logging.getLogger("annual_report_tools")

@retry_on_transient_error(max_attempts=3)
def fetch_annual_report(company_or_ticker: str) -> dict:
    """
    Downloads the annual report PDF for the given company into the session's isolated temporary directory.
    Uses Tavily search to find the PDF URL if not cached.
    Returns a dict with 'status' ("success" or "not_found") and 'pdf_path' or 'reason'.
    """
    from tools.search_tools import search_web_news
    import httpx
    
    session_id = os.environ.get("FINOSCALE_SESSION_ID", "default_session")
    isolated_dir = settings.cache_dir / "sessions" / session_id / "reports"
    isolated_dir.mkdir(parents=True, exist_ok=True)
    
    pdf_path = isolated_dir / f"{company_or_ticker.replace('.', '_')}_annual_report.pdf"
    
    # Cache hit: If we already downloaded it, do not burn a search token
    if pdf_path.exists():
        return {"status": "success", "pdf_path": str(pdf_path)}

    if os.environ.get("FORCE_ANNUAL_REPORT_NOT_FOUND") == "1":
        return {"status": "not_found", "reason": "forced by FORCE_ANNUAL_REPORT_NOT_FOUND"}

    try:
        query = f"{company_or_ticker} annual report filetype:pdf"
        results = search_web_news(query=query, depth="basic", max_results=3)
        
        pdf_url = None
        for r in results:
            if r.get("url", "").endswith(".pdf"):
                pdf_url = r["url"]
                break
                
        if not pdf_url:
            return {"status": "not_found", "reason": f"Could not find an annual report PDF for {company_or_ticker}"}
            
    except Exception as e:
        # If the search budget is exhausted or query fails, return a graceful fallback
        # so the agent doesn't crash the entire session.
        return {"status": "not_found", "reason": f"Search Limit Exceeded or Error: {e}"}
        
    logger.info(f"Downloading annual report from {pdf_url} to {pdf_path}")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            resp = client.get(pdf_url)
            resp.raise_for_status()
            pdf_path.write_bytes(resp.content)
    except Exception as e:
        return {"status": "not_found", "reason": f"Failed to download PDF from {pdf_url}: {e}"}
        
    return {"status": "success", "pdf_path": str(pdf_path)}


def parse_report_text(pdf_path: str) -> list[dict]:
    """
    Parses a PDF annual report page-by-page using pymupdf4llm (no external API calls).
    Returns a list of dicts: {"page_num": int, "text": str}.
    """
    if not pymupdf4llm:
        raise ImportError("pymupdf4llm is required to parse annual reports.")
        
    logger.info(f"Parsing PDF text from {pdf_path}")
    md_text = pymupdf4llm.to_markdown(pdf_path, page_chunks=True)
    
    pages = []
    for chunk in md_text:
        page_num = chunk.get("metadata", {}).get("page", 0) + 1
        text = chunk.get("text", "").strip()
        pages.append({
            "page_num": page_num,
            "text": text
        })
        
    return pages


def run_ocr_fallback(pdf_path: str, page_numbers: list[int]) -> list[dict]:
    """
    Targeted, strictly lazy OCR fallback for empty or scanned pages only.
    """
    if not fitz:
        raise ImportError("PyMuPDF (fitz) is required for OCR fallback.")
        
    logger.info(f"Running targeted OCR fallback on {pdf_path} for pages {page_numbers}")
    
    results = []
    doc = fitz.open(pdf_path)
    for p_num in page_numbers:
        if 1 <= p_num <= len(doc):
            page = doc[p_num - 1]
            text = page.get_text("text").strip()
            
            if not text:
                text = "[OCR Failed or Image Only Page]"
                
            results.append({
                "page_num": p_num,
                "text": text
            })
            
    return results


def build_section_index(pages: list[dict]) -> dict:
    """
    Heuristic keyword/TOC matcher that returns strictly bounded page ranges 
    for each of the 4 ScoreCategory values. 
    This ensures context isolation so the Gemini token limit is not exceeded.
    Returns a dict mapping category to a list of page numbers (capped at 40).
    """
    finances_keywords = ["consolidated financial", "balance sheet", "profit and loss", "cash flow", "notes to financial"]
    business_keywords = ["management discussion", "mda", "business review", "operational highlights", "chairman's message"]
    hygiene_keywords = ["corporate governance", "auditor's report", "board of directors", "remuneration", "risk management"]
    banking_keywords = ["capital adequacy", "npa", "basel", "net interest margin", "nim"]
    
    index = {
        "Finances": [],
        "Business & Management": [],
        "Hygiene": [],
        "Banking": []
    }
    
    for p in pages:
        text_lower = p["text"].lower()
        p_num = p["page_num"]
        
        if any(k in text_lower for k in finances_keywords):
            index["Finances"].append(p_num)
        if any(k in text_lower for k in business_keywords):
            index["Business & Management"].append(p_num)
        if any(k in text_lower for k in hygiene_keywords):
            index["Hygiene"].append(p_num)
        if any(k in text_lower for k in banking_keywords):
            index["Banking"].append(p_num)
            
    bounded_index = {}
    for cat, p_nums in index.items():
        unique_sorted = sorted(list(set(p_nums)))[:40]
        bounded_index[cat] = unique_sorted
        
    return bounded_index

def build_fallback_dossier(state: "AgentState") -> dict[str, str]:
    """
    Builds category-bounded text dossiers from data already gathered in AgentState
    during this session, for use when no annual report PDF is available. Output shape
    mirrors what build_section_index()'s downstream consumer (get_category_text) expects:
    dict[category_name] -> non-empty text blob. Every returned string must open with a
    clear provenance line.
    """
    provenance = (
        "DATA SOURCE: Live market/compliance data gathered this session — no annual report\n"
        "filing was available for this run. Do not cite page numbers; cite the originating\n"
        "tool/metric instead.\n\n"
    )
    
    dossier = {}
    
    # 1. Finances
    f_lines = [provenance, f"Fallback Financial Data for {state.company_name or state.ticker}"]
    if state.market_data:
        f_lines.append("\n--- Market Data & Fundamentals ---")
        for k, v in state.market_data.items():
            if k not in ["quarterly_financials", "moneycontrol_data"] and not isinstance(v, (dict, list)):
                f_lines.append(f"{k}: {v}")
                
        q_fin = state.market_data.get("quarterly_financials", [])
        if q_fin:
            f_lines.append("\n--- Quarterly Financials ---")
            for q in q_fin:
                f_lines.append(str(q))
                
    if state.sector_metrics:
        f_lines.append("\n--- Sector Metrics ---")
        if state.sector_metrics.retail:
            f_lines.append("Retail/Consumer Metrics:")
            for k, v in state.sector_metrics.retail.model_dump().items():
                f_lines.append(f"  {k}: {v}")
        if state.sector_metrics.saas:
            f_lines.append("SaaS Metrics:")
            for k, v in state.sector_metrics.saas.model_dump().items():
                f_lines.append(f"  {k}: {v}")
    
    if len(f_lines) <= 2:
        f_lines.append("No further financial data available.")
    dossier["Finances"] = "\n".join(f_lines).strip()

    # 2. Business & Management
    b_lines = [provenance, f"Fallback Business & Management Data for {state.company_name or state.ticker}"]
    if state.sentiment_findings:
        b_lines.append(f"\nSentiment Summary: {state.sentiment_findings.sentiment_summary}")
        b_lines.append(f"Overall Sentiment: {state.sentiment_findings.overall_sentiment.value}")
        
        if state.sentiment_findings.key_catalysts:
            b_lines.append("\nKey Catalysts (Opportunities/Strengths):")
            for cat in state.sentiment_findings.key_catalysts:
                b_lines.append(f"- {cat.claim}")
                
        if state.sentiment_findings.key_risks:
            b_lines.append("\nKey Risks (Threats/Weaknesses):")
            for risk in state.sentiment_findings.key_risks:
                b_lines.append(f"- {risk.claim}")
                
    if state.anomaly_findings:
        b_lines.append("\n--- Anomaly Findings ---")
        for af in state.anomaly_findings:
            b_lines.append(f"Anomaly: {af.anomaly_type} | Impacted: {af.metric_impacted} | Severity: {af.severity}")
            
    if state.peer_benchmarks:
        b_lines.append("\n--- Peer Benchmarks ---")
        b_lines.append(f"Industry: {state.peer_benchmarks.industry}")
        b_lines.append(f"Summary: {state.peer_benchmarks.industry_summary}")
        
    if state.market_data and "business_description" in state.market_data:
        b_lines.append(f"\nBusiness Description: {state.market_data['business_description']}")
        
    if len(b_lines) <= 2:
        b_lines.append("No further business/management data available.")
    dossier["Business & Management"] = "\n".join(b_lines).strip()

    # 3. Hygiene
    h_lines = [provenance, f"Fallback Hygiene & Governance Data for {state.company_name or state.ticker}"]
    if state.market_data:
        h_lines.append("\n--- Ownership Data ---")
        h_lines.append(f"Promoter Holding: {state.market_data.get('promoter_holding_pct', 'N/A')}%")
        h_lines.append(f"Institutional Holding: {state.market_data.get('institutional_holding_pct', 'N/A')}%")
        h_lines.append(f"Public Holding: {state.market_data.get('public_holding_pct', 'N/A')}%")
        
        mc = state.market_data.get("moneycontrol_data", {})
        if mc:
            h_lines.append("\n--- Additional Local Exchange Data (Moneycontrol) ---")
            for k, v in mc.items():
                if not isinstance(v, (dict, list)):
                    h_lines.append(f"{k}: {v}")
                    
    if state.aml_result and state.aml_result.findings:
        h_lines.append("\n--- AML / Compliance Screening Findings ---")
        for f in state.aml_result.findings:
            h_lines.append(f"Severity: {f.severity.value} | Source: {f.source_name} | Finding: {f.finding_summary}")
            
    if state.cro_audit_report:
        h_lines.append(f"\n--- CRO Audit Passed: {state.cro_audit_report.audit_passed} ---")
            
    if len(h_lines) <= 2:
        h_lines.append("No further hygiene data available.")
    dossier["Hygiene"] = "\n".join(h_lines).strip()

    # 4. Banking
    ba_lines = [provenance, f"Fallback Banking Sector Data for {state.company_name or state.ticker}"]
    if state.sector_metrics and state.sector_metrics.banking:
        ba_lines.append("\n--- Banking Metrics ---")
        for k, v in state.sector_metrics.banking.model_dump().items():
            ba_lines.append(f"{k}: {v}")
    else:
        ba_lines.append("Not a banking entity — N/A")
    dossier["Banking"] = "\n".join(ba_lines).strip()

    return dossier
