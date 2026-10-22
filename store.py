import json
import os

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


def load_reports(path=None):
    path = path or store_path()
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    reports = payload.get("reports", []) if isinstance(payload, dict) else payload
    for report in reports:
        report["page_map"] = {int(key): value for key, value in (report.get("page_map") or {}).items()}
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
        item["page_map"] = {str(key): value for key, value in (item.get("page_map") or {}).items()}
        payload.append(item)
    _atomic_write_json(path, {"reports": payload})


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
