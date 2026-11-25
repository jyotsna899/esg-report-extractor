"""
Entry point used by the Streamlit app and the background job.

ESG_ENGINE=v2 (default): accuracy-first core in esg_v2/ — section-wise page
selection, native-PDF extraction of raw datapoints, Python-side units,
grounding checks and formulas from config/kpis.csv.
ESG_ENGINE=legacy: the previous single-prompt extractor (extractor.py).
"""

import gc
import os


def _engine() -> str:
    return os.environ.get("ESG_ENGINE", "v2").strip().lower()


def _extract_v2(path, company_name, on_status):
    import extractor  # noqa: F401  (importing it loads GEMINI_API_KEY from .env)
    from esg_v2.engine import extract_report

    report = extract_report(path, company_name, on_status=on_status)
    gc.collect()
    return report


def _extract_legacy(path, company_name, on_status):
    from extractor import extract_kpis, extract_text_from_pdf

    def page_cb(current, total):
        fraction = current / max(total, 1)
        on_status(0.45 * fraction, f"Reading {current}/{total}")

    def kpi_cb(current, total, status_text):
        fraction = current / max(total, 1)
        on_status(0.45 + 0.55 * fraction, status_text)

    text = extract_text_from_pdf(
        path,
        max_pages=100_000,
        start_page=1,
        progress_callback=page_cb,
    )
    kpis, debug_log, topic_index, page_map = extract_kpis(
        text, company_name=company_name, progress_callback=kpi_cb,
    )
    gc.collect()
    return {
        "name": company_name,
        "kpis": kpis,
        "topic_index": topic_index,
        "page_map": page_map,
        "full_text": text,
        "chat_history": [],
        "summary": "",
        "debug_log": debug_log,
    }


def extract_one_pdf(path, company_name, on_status):
    if _engine() == "legacy":
        return _extract_legacy(path, company_name, on_status)
    return _extract_v2(path, company_name, on_status)
