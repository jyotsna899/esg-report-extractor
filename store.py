import json
import os
import re
import shutil


def data_dir():
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(folder, exist_ok=True)
    return folder


def store_path():
    return os.path.join(data_dir(), "reports.json")


def job_path():
    return os.path.join(data_dir(), "job.json")


def pending_job_path():
    return os.path.join(data_dir(), "pending_job.json")


def job_input_dir():
    return os.path.join(data_dir(), "job_input")


def _atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def _rebuild_page_map(text):
    pages = {}
    if not text:
        return pages
    matches = list(re.finditer(r"--- Page (\d+) ---\n", text))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages[int(match.group(1))] = text[start:end].strip()
    return pages


def load_reports(path=None):
    path = path or store_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    reports = payload.get("reports", []) if isinstance(payload, dict) else payload
    for report in reports:
        raw_map = report.get("page_map") or {}
        full_text = report.get("full_text") or ""
        if raw_map:
            report["page_map"] = {int(key): value for key, value in raw_map.items()}
        elif full_text:
            report["page_map"] = _rebuild_page_map(full_text)
        else:
            report["page_map"] = {}
        report.setdefault("chat_history", [])
        report.setdefault("metrics", [])
        report.setdefault("topic_index", [])
        report.setdefault("full_text", "")
        report.setdefault("summary", "")
    return reports


def save_reports(reports, path=None):
    path = path or store_path()
    payload = []
    for report in reports:
        item = dict(report)
        if item.get("full_text"):
            item.pop("page_map", None)
        else:
            item["page_map"] = {str(key): value for key, value in (item.get("page_map") or {}).items()}
        payload.append(item)
    _atomic_write_json(path, {"reports": payload})


def upsert_report(new_report, path=None):
    reports = load_reports(path=path)
    new_name = new_report.get("name")
    new_source = new_report.get("source_file")
    replaced = False
    for idx, existing in enumerate(reports):
        same_source = bool(new_source and existing.get("source_file") == new_source)
        same_name = bool(new_name and existing.get("name") == new_name)
        if same_source or same_name:
            reports[idx] = new_report
            replaced = True
            break
    if not replaced:
        reports.append(new_report)
    save_reports(reports, path=path)
    return reports


def default_job():
    return {
        "status": "idle",
        "message": "",
        "progress": 0.0,
        "pid": None,
        "ready_count": 0,
        "total_count": 0,
        "current_index": 0,
        "error": None,
    }


def load_job():
    path = job_path()
    if not os.path.isfile(path):
        return default_job()
    with open(path, encoding="utf-8") as handle:
        job = json.load(handle)
    merged = default_job()
    merged.update(job)
    return merged


def save_job(**fields):
    job = load_job()
    job.update(fields)
    _atomic_write_json(job_path(), job)


def load_pending_job():
    path = pending_job_path()
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save_pending_job(payload):
    _atomic_write_json(pending_job_path(), payload)


def remaining_pending_items():
    pending = load_pending_job()
    if not pending:
        return []
    items = pending.get("items") or []
    remaining = []
    for index, item in enumerate(items):
        if item.get("done"):
            continue
        pdf_path = item.get("path") or ""
        if os.path.isfile(pdf_path):
            remaining.append((index, item))
    return remaining


def mark_pending_item_done(index):
    pending = load_pending_job()
    if not pending:
        return
    items = pending.get("items") or []
    if 0 <= index < len(items):
        items[index]["done"] = True
        pdf_path = items[index].get("path") or ""
        if pdf_path and os.path.isfile(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass
    save_pending_job(pending)
    if all(item.get("done") for item in items):
        clear_pending_job()


def clear_pending_job():
    path = pending_job_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
    input_dir = job_input_dir()
    if os.path.isdir(input_dir):
        shutil.rmtree(input_dir, ignore_errors=True)
