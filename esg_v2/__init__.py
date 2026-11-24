"""
ESG extractor v2 — accuracy-first extraction core.

Flow (one report):
    1. pdf_pages    read page text (used only to FIND pages and to VERIFY numbers)
    2. page_finder  pick the handful of pages relevant to each BRSR section group
    3. llm          send those pages to Gemini as a real PDF (tables keep their layout)
                    and ask only for RAW datapoints, copied exactly as printed
    4. units        convert raw values to canonical units in Python
    5. validate     check every number really appears on the page; sanity rules
    6. formulas     compute the 39 KPIs in Python from config/kpis.csv
    7. export       write the long-format template from config/output_columns.csv

Everything the analyst team may want to tweak lives in config/*.csv,
not in code: datapoints (what to extract, where), KPI formulas and the
output template.
"""
