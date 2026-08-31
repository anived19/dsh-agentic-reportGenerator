---
name: credit-report-format
description: Enforces absolute data fidelity and strict 13-section skeleton mapping for synthesis output.
---

# Credit Report Formatting Rules

## Absolute Data Fidelity
- You must exclusively use real, verified data extracted from the tools.
- You are strictly forbidden from padding numbers, fabricating revenue metrics, or inserting fictional profit statements.
- If data is missing, it must be marked as unavailable (N/A) rather than hallucinated.

## 13-Section Skeleton Mapping
You must map the consolidated data to these exact headers in the final output:

**Dashboard Header**: 
Entity Name, CIN, PAN, Vintage, Recommendation, Recommended Limit, FINOSCALE SCORE, FINANCIAL SCORE, BUSINESS & MGMT, HYGIENE SCORE, BANKING SCORE, REVENUE (LATEST FY), EBITDA & PAT, TOTAL DEBT/EQUITY, NET WORTH, and CREDIT RATING.

1. **01 CASE PROPOSAL ENTITY INFORMATION**: Table of fundamental company details.
2. **02 SHAREHOLDING PATTERN**: Table of owners and exact percentages.
3. **03 PROFILE**: Qualitative operational and manufacturing summary.
4. **04 RATING & UTILIZATION**: Formal agency ratings.
5. **05 BANKING FACILITIES**: Table of FB and NFB limits and utilization.
6. **06 ACTIVE GSTIN DETAILS**: Table mapping GSTINs to specific states.
7. **07 COMPLIANCE CHECK**: Market Feedback, GSTR, and PF status.
8. **08 TURNOVER ANALYSIS**: YoY historical revenue.
9. **09 EBITDA & PAT**: Margins and absolute figures.
10. **10 FINANCIAL ANALYSIS**: Multi-year table covering Profitability, Leverage, Borrowings, Cost Structure, Debt Service, and Working Capital.
11. **11 AML & ADVERSE CHECKS**: Summary of regulatory screening outcomes.
12. **12 COMFORTS & DISCOMFORTS**: Bulleted risk and strength assessment.
13. **13 FINAL CREDIT RECOMMENDATION**: Explicit exposure limit and rationales.
