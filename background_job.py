import os
import shutil
import sys
import traceback

from pipeline import extract_one_pdf
from store import job_input_dir, load_job, load_pending_job, save_job, save_reports


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

    reports = []
    save_reports(reports)
    try:
        for index, item in enumerate(items):
            company_name = item["company"]
            pdf_path = item["path"]
            source_file = item.get("source_file", os.path.basename(pdf_path))

            def on_status(fraction, status_text, index=index, company_name=company_name):
                overall = (index + max(0.0, min(fraction, 1.0))) / total
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
                reports.append(report)
            except Exception as exc:
                reports.append({
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
                })
            save_reports(reports)

        ready_count = sum(1 for report in reports if report.get("metrics"))
        save_job(
            status="completed",
            progress=1.0,
            message=f"Finished. {ready_count} of {total} files produced metrics.",
            ready_count=ready_count,
            total_count=total,
            current_index=total,
        )
        return 0
    except Exception:
        save_job(
            status="failed",
            message="Background extraction failed.",
            error=traceback.format_exc(),
        )
        return 1
    finally:
        input_dir = job_input_dir()
        if os.path.isdir(input_dir):
            shutil.rmtree(input_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run_job())
