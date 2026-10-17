# AI-Powered ESG Report Extractor

Automatically extracts structured ESG metrics from company sustainability
reports (PDF), mapping each one to GRI Standards codes and BRSR Core
principles — and flags disclosure gaps against SEBI's BRSR Core framework.

## The problem

ESG/sustainability reports run 80–150+ pages. Analysts manually read
through them to pull out emissions data, diversity stats, and governance
metrics for benchmarking or compliance checks — slow, repetitive, and
error-prone work.

## What this does

1. **Reads** any company's sustainability/ESG/BRSR report PDF
2. **Extracts** every quantifiable ESG metric using an LLM, structured as:
   metric name, value, unit, year, category (E/S/G), GRI code, BRSR
   principle
3. **Checks coverage** against SEBI's 9 BRSR Core principles and flags
   which ones the company hasn't disclosed data for
4. **Outputs** a clean table + downloadable CSV/JSON, viewable in a
   simple web app

## Why it matters

This mirrors real work done by ESG analysts and consultants preparing
clients for CSRD/BRSR compliance — turning hours of manual PDF reading
into a few minutes of automated extraction, with a compliance gap check
built in.

## Tech stack

- `pdfplumber` — PDF text extraction
- Google Gemini API (`gemini-2.5-flash`) — structured metric extraction
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

For each metric found:

```json
{
  "metric_name": "Scope 1 GHG Emissions",
  "value": 12500,
  "unit": "tCO2e",
  "year": 2024,
  "category": "Environmental",
  "gri_code": "GRI 305-1",
  "brsr_principle": 6,
  "source_context": "Environment section, emissions table",
  "company": "Example Corp"
}
```

## Next steps / possible extensions

- Multi-company comparison view (benchmark 3-4 companies side by side)
- Year-over-year trend tracking for repeat report uploads
- Confidence scoring on extracted values
- Table extraction (pdfplumber tables) for reports with heavy tabular data
