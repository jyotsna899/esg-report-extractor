# AI-Powered ESG Report Extractor

Extracts a fixed set of 39 ESG and financial KPIs from company
sustainability / BRSR reports (PDF), capturing both the current and the
previous financial year when the report shows them side by side.

## The problem

ESG/sustainability reports run 80–150+ pages. Analysts manually read
through them to pull out emissions data, diversity stats, and governance
metrics for benchmarking or compliance checks — slow, repetitive, and
error-prone work.

## What this does

1. **Reads** any company's sustainability/ESG/BRSR report PDF
2. **Extracts only the KPIs in [kpis.py](kpis.py)**, which are E1–E11,
   S1–S16, G1–G10 and F1–F2 (e.g. renewable energy share, energy and
   emission intensity per Rs. crore of revenue, LTIFR, POSH complaints,
   CSR budget vs spend, board diversity, assurance status, revenue/EBITDA/PAT,
   green capex). Each KPI gets a current-year and a previous-year value.
3. **Standardises units in code, not in the LLM.** The model copies numbers
   and units exactly as printed. Python then converts them (e.g. "GJ per
   rupee of turnover" → MJ per Rs. crore, Rs. lakh → Rs. crore) or computes
   ratios from stated inputs (e.g. CSR spent ÷ budgeted, emissions ÷ F1
   revenue). Every such value is marked `derived` and keeps the formula
   used. Units it can't convert safely (e.g. PPP-adjusted intensities) are
   kept as printed and marked `reported_unit_mismatch`.
4. **Outputs** the same rows for every company (undisclosed KPIs included),
   so companies line up by KPI ID in the Compare tab and CSV exports

## Why it matters

This mirrors real work done by ESG analysts and consultants preparing
clients for CSRD/BRSR compliance — turning hours of manual PDF reading
into a few minutes of automated extraction, with a list of undisclosed KPIs
built in.

## Tech stack

- `pdfplumber` — PDF text extraction
- Google Gemini API — structured KPI extraction
  via prompting, no model training/fine-tuning required. Gemini's
  1M-token context window means most full reports (100+ pages) are
  processed in a **single API call** — no manual chunking needed.
- `Streamlit` — demo interface
- `pandas` — data structuring and CSV/JSON export

## Running it

```bash
pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"

# CLI version
python extractor.py path/to/report.pdf "Company Name"

# Web app version
streamlit run app.py
```

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com/apikey) —
Gemini's free tier is generous enough to fully test this project at no cost.

## Example output

One record per KPI:

```json
{
  "kpi_id": "E2",
  "kpi_name": "Energy Intensity",
  "category": "Environment",
  "required_unit": "MJ per Rs. crore of revenue",
  "current":  {"fy": "FY 2024-25", "value": 123000.0, "unit": "MJ per Rs. crore", "components": {}, "text": null},
  "previous": {"fy": "FY 2023-24", "value": 150000.0, "unit": "MJ per Rs. crore", "components": {}, "text": null},
  "status": "derived",
  "derivation": "current: 1.23e-05 GJ per rupee of turnover × 1000 × 1e+07 = 123,000.00 MJ per Rs. crore; ...",
  "source_context": "Energy intensity per rupee of turnover 0.0000123 GJ/₹",
  "pages": [84],
  "company": "Example Corp"
}
```

`status` is one of `reported`, `derived`, `reported_unit_mismatch` or
`not_disclosed`. Composite KPIs (e.g. S10 POSH complaints, F1
revenue/EBITDA/PAT) put their parts in `components`.

## Tests

```bash
python -m pytest tests/
```

## Next steps / possible extensions

- Trend tracking across more than two years from repeat report uploads
- Confidence scoring on extracted values
- Table extraction (pdfplumber tables) for reports with heavy tabular data
