import pytest
import os
import json
from harness.synthesis import render_credit_scoring_markdown
from schemas import ScoreCategoryResult, ScoreCategory, PageCitedClaim

def test_render_credit_scoring_markdown_full():
    results = [
        ScoreCategoryResult(score_category=ScoreCategory.FINANCES, score_value=85, comforts=[PageCitedClaim(claim="strong cash flow", page_citation="p1")], raw_evidence_snippets=""),
        ScoreCategoryResult(score_category=ScoreCategory.BUSINESS_MANAGEMENT, score_value=90, discomforts=[PageCitedClaim(claim="attrition", page_citation="p2")], raw_evidence_snippets=""),
        ScoreCategoryResult(score_category=ScoreCategory.HYGIENE, score_value=95, raw_evidence_snippets="good"),
        ScoreCategoryResult(score_category=ScoreCategory.BANKING, score_value=80, raw_evidence_snippets="decent"),
    ]
    md = render_credit_scoring_markdown(results)
    assert "| Finances | 85/100 | strong cash flow |" in md
    assert "| Business & Management | 90/100 | attrition |" in md
    assert "| Hygiene | 95/100 | good |" in md
    assert "| Banking | 80/100 | decent |" in md
    assert "**Average score: 87.5/100** across 4 of 4 pillars evaluated" in md
    assert "Not scored this run" not in md

def test_render_credit_scoring_markdown_partial():
    results = [
        ScoreCategoryResult(score_category=ScoreCategory.FINANCES, score_value=85, raw_evidence_snippets="strong cash flow"),
        ScoreCategoryResult(score_category=ScoreCategory.HYGIENE, score_value=95, raw_evidence_snippets="good"),
    ]
    md = render_credit_scoring_markdown(results)
    assert "| Finances | 85/100 | strong cash flow |" in md
    assert "Not scored this run" in md
    assert "Banking" in md
    assert "Business & Management" in md

def test_render_credit_scoring_markdown_empty():
    assert render_credit_scoring_markdown([]) == ""
