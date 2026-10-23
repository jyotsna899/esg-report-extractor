import gc
import os
import subprocess
import sys
import traceback

from pipeline import extract_one_pdf
from store import (
    clear_pending_job,
    load_pending_job,
    load_reports,
    mark_pending_item_done,
    save_job,
    upsert_report,
)


def _already_extracted(reports, company_name, source_file):
    for report in reports:
        if not report.get("metrics"):
            continue
        if source_file and report.get("source_file") == source_file:
            return True
        if company_name and report.get("name") == company_name:
            return True
    return False


def run_single_item(index):
    pending = load_pending_job()
    if not pending:
        return 1
    items = pending.get("items") or []
    total = len(items)
    if index < 0 or index >= total:
        return 1

    item = items[index]
    company_name = item["company"]
    pdf_path = item["path"]
    source_file = item.get("source_file", os.path.basename(pdf_path))

    def on_status(fraction, status_text):
        overall = (index + max(0.0, min(fraction, 1.0))) / max(total, 1)
        save_job(
            status="running",
            progress=overall,
            message=f"{company_name} ({index + 1}/{total}): {status_text}",
            current_index=index + 1,
            total_count=total,
        )

    try:
        report = extract_one_pdf(pdf_path, company_name, on_status)
        report["source_file"] = source_file
    except Exception as exc:
        report = {
            "name": company_name,
            "source_file": source_file,
            "metrics": [],
            "topic_index": [],
            "page_map": {},
            "full_text": "",
            "chat_history": [],
            "summary": "",
            "debug_log": [{"chunk": 0, "status": "error", "detail": str(exc)}],
            "error": str(exc),
        }

    reports = upsert_report(report)
    mark_pending_item_done(index)
    ready_count = sum(1 for entry in reports if entry.get("metrics"))
    save_job(
        status="running",
        progress=(index + 1) / max(total, 1),
        message=f"Saved {company_name} ({index + 1}/{total}).",
        ready_count=ready_count,
        current_index=index + 1,
        total_count=total,
    )
    gc.collect()
    return 0


def run_job():
    pending = load_pending_job()
    if not pending:
        save_job(status="failed", message="No pending job found.")
        return 1

    items = pending.get("items") or []
    total = len(items)
    if not total:
        save_job(status="failed", message="No PDFs in pending job.")
        return 1

    root = os.path.dirname(os.path.abspath(__file__))
    try:
        for index, item in enumerate(items):
            company_name = item["company"]
            pdf_path = item["path"]
            source_file = item.get("source_file", os.path.basename(pdf_path))

            reports = load_reports()
            if item.get("done") or _already_extracted(reports, company_name, source_file):
                if not item.get("done"):
                    mark_pending_item_done(index)
                continue

            if not os.path.isfile(pdf_path):
                report = {
                    "name": company_name,
                    "source_file": source_file,
                    "metrics": [],
                    "topic_index": [],
                    "page_map": {},
                    "full_text": "",
                    "chat_history": [],
                    "summary": "",
                    "debug_log": [{"chunk": 0, "status": "error", "detail": "Queued PDF file is missing."}],
                    "error": "Queued PDF file is missing. Upload this PDF again to extract it.",
                }
                upsert_report(report)
                mark_pending_item_done(index)
                continue

            result = subprocess.run(
                [sys.executable, os.path.join(root, "background_job.py"), "--single-index", str(index)],
                cwd=root,
            )
            if result.returncode != 0:
                reports = load_reports()
                ready_count = sum(1 for entry in reports if entry.get("metrics"))
                save_job(
                    status="failed",
                    message=(
                        f"Stopped while processing {company_name} (PDF {index + 1} of {total}). "
                        f"{ready_count} completed report(s) are saved. You can resume from {company_name}."
                    ),
                    ready_count=ready_count,
                    current_index=index + 1,
                    total_count=total,
                    error=f"Worker exited with code {result.returncode} on {company_name}",
                )
                return result.returncode

        reports = load_reports()
        ready_count = sum(1 for report in reports if report.get("metrics"))
        clear_pending_job()
        save_job(
            status="completed",
            progress=1.0,
            message=f"Finished. {ready_count} of {len(reports)} saved reports produced metrics.",
            ready_count=ready_count,
            total_count=total,
            current_index=total,
        )
        return 0
    except Exception:
        reports = load_reports()
        ready_count = sum(1 for report in reports if report.get("metrics"))
        save_job(
            status="failed",
            message=f"Extraction interrupted. {ready_count} completed report(s) are saved and ready to view.",
            ready_count=ready_count,
            error=traceback.format_exc(),
        )
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--single-index":
        sys.exit(run_single_item(int(sys.argv[2])))
    sys.exit(run_job())
