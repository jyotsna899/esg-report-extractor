from extractor import extract_esg_metrics, extract_text_from_pdf


def extract_one_pdf(path, company_name, on_status):
    def page_cb(current, total):
        fraction = current / max(total, 1)
        on_status(0.45 * fraction, f"Reading {current}/{total}")

    def metric_cb(current, total, status_text):
        fraction = current / max(total, 1)
        on_status(0.45 + 0.55 * fraction, status_text)

    text = extract_text_from_pdf(
        path,
        max_pages=100_000,
        start_page=1,
        progress_callback=page_cb,
    )
    metrics, debug_log, topic_index, page_map = extract_esg_metrics(
        text, company_name=company_name, progress_callback=metric_cb,
    )
    return {
        "name": company_name,
        "metrics": metrics,
        "topic_index": topic_index,
        "page_map": page_map,
        "full_text": text,
        "chat_history": [],
        "summary": "",
        "debug_log": debug_log,
    }
