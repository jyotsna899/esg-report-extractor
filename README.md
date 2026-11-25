# AI-Powered ESG Report Extractor

## v2 extraction engine (default)

The original single-prompt extractor read flattened PDF text, so BRSR tables
(Male/Female/Total × FY columns) lost their structure and values came from the
wrong row or year. v2 rebuilds the core around accuracy:

| Step | What happens | Where |
|---|---|---|
| 1. Find pages | Keyword + heading scoring picks ~10–16 relevant pages per section group (general, governance, employees, environment, community, financial) | `esg_v2/page_finder.py` |
| 2. Extract raw datapoints | Those pages are sent to Gemini **as a PDF** (tables keep their layout), one call per group, with a strict JSON schema. The model only copies figures + units exactly as printed — no maths | `esg_v2/llm.py` |
| 3. Units | Rs./lakh/crore/million, GJ/MWh/TJ, kL/ML, kg/MT, … converted in Python | `esg_v2/units.py` |
| 4. Verify | Every number must appear in the page text (page auto-corrected if found on another page); sanity rules (0–100 %, parts ≤ totals, renewable + non-renewable = total, 10× YoY jumps, computed vs company-reported intensity) | `esg_v2/validate.py`, `esg_v2/engine.py` |
| 5. KPIs | The 39 KPIs computed in Python from `config/kpis.csv` | `esg_v2/formulas.py` |
| 6. Export | Long format: Company · FY · KPI · Value · Unit · Type · Source page(s) · Formula/inputs · Quote · Status · Review flags | `esg_v2/export.py` |

### Edit without touching code (`config/`)

- `datapoints.csv` — the ~66 raw figures to extract: BRSR reference, extraction instruction, page-finder keywords.
- `kpis.csv` — the 39 KPIs: type (Direct/Derived), output unit, **formula** over datapoint ids
  (e.g. `renewable_energy / total_energy * 100`). Alternatives with `||` — the first whose inputs exist is used.
  `Helper` rows (e.g. `REVENUE`) are reusable building blocks, not exported.
- `output_columns.csv` — the export template: rename, reorder or delete rows to change the columns.

### Run

```bash
pip install -r requirements.txt
echo 'GEMINI_API_KEY=your-key' > .env          # optional: GEMINI_MODEL=gemini-3.5-flash (default)

streamlit run app.py                           # UI — use "KPI Template" downloads
python -m esg_v2.cli report.pdf --company "Acme" --out out/acme.xlsx
python -m esg_v2.cli reports_folder/ --out out/all.xlsx   # bulk, resumes from out/cache/
```

The .xlsx has three sheets: **KPI Output** (template), **Needs Review** (only rows with flags)
and **Raw Datapoints** (every printed figure, unit, page and quote — the audit trail).

Settings: `ESG_ENGINE=legacy` switches back to the old extractor; `ESG_PARALLEL=3` sets
concurrent section calls (lower it if you hit rate limits).

### Measure accuracy

Fill `eval/answer_key_template.csv` for 3–5 companies by hand, then:

```bash
python eval/evaluate.py eval/answer_key.csv out/all.xlsx
```

It prints overall and per-KPI accuracy (worst first) and every mismatch with its page.
Fix the worst KPIs by refining that datapoint's `instruction` / `keywords` in `config/datapoints.csv`.

---

## Original documentation (legacy engine)


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

## Output Formats & Comparison Tables

1. **Company Multi-Year Historical Trend Table**:
   `S. No. | Unit | Env./Social/Gov. | KPI | FY2024 | FY2025 | FY2026 ...`
   Enables side-by-side KPI comparison of a company across multiple Financial Years.

2. **Comparative Analysis Table (FY2026 / Selected FY)**:
   `S. No. | KPI Name | Unit | Company 1 | Company 2 | Company 3 ...`
   Enables side-by-side KPI benchmarking across multiple companies for a specific financial year.

3. **Multi-Sheet Excel Workbook (`.xlsx`) & CSV Exports**:
   - Multi-sheet Excel workbook (`.xlsx`) containing:
     - Sheet 1: `Comparative Analysis ({target_fy})`
     - Sheet 2: `All Companies Consolidated Multi-Year Trend`
     - Subsequent Sheets: Per-company multi-year breakdown tabs
   - Direct CSV exports for both individual company multi-year tables and cross-company comparative tables, plus bulk ZIP download.

## Tests

```bash
python -m pytest tests/
```

## Next steps / possible extensions

- Confidence scoring on extracted values
- Table extraction (pdfplumber tables) for reports with heavy tabular data
