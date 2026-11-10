"""
ESG Report Extractor — Streamlit Demo
---------------------------------------
Upload a company sustainability/ESG/BRSR report PDF and get:
  - Detail tab: the fixed KPI list (current + previous year) by category + an AI summary
  - Chat tab: ask follow-up questions, grounded in the extracted data,
    escalating to the real report text only when it needs to
  - Compare tab: line two (or more) companies up side by side

Run locally with:  streamlit run app.py
"""

import gc
import json
import os
import io
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import pandas as pd
import streamlit as st

try:
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use("Agg")  # no display needed, just rendering to PNG bytes
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from fpdf import FPDF
    FPDF_AVAILABLE = True
except ImportError:
    FPDF_AVAILABLE = False

from extractor import answer_chat_question, generate_summary
from kpis import (
    CATEGORIES,
    KPI_BY_ID,
    KPI_CATALOG,
    company_multi_year_frame,
    comparative_analysis_frame,
    consolidated_multi_year_frame,
    coverage as kpi_coverage,
    export_excel_workbook,
    extract_report_dominant_years,
    format_year,
    get_kpi_value_for_fy,
    headline_value,
    is_disclosed,
    is_legacy_report,
    report_ready,
    standardize_fy,
    yoy_change,
)
from pipeline import extract_one_pdf
from runtime import apply_streamlit_secrets, is_streamlit_cloud
from store import (
    clear_all_reports,
    clear_pending_job,
    data_dir,
    job_input_dir,
    load_job,
    load_pending_job,
    load_report_page_map,
    load_report_text,
    load_reports,
    mark_pending_item_done,
    remaining_pending_items,
    save_job,
    save_pending_job,
    save_reports,
    upsert_report,
)

st.set_page_config(page_title="ESG Report Extractor", layout="wide")
apply_streamlit_secrets()

if not os.environ.get("GEMINI_API_KEY"):
    st.error(
        "GEMINI_API_KEY is not set. Extraction and chat will fail until you add it.\n\n"
        "Local: put it in `.env` or run `export GEMINI_API_KEY=...` before Streamlit.\n\n"
        "Streamlit Cloud: Manage app → Settings → Secrets:\n\n"
        "GEMINI_API_KEY = \"your-key-here\"\n\n"
        "Get a key at https://aistudio.google.com/apikey"
    )

st.title("📊 AI-Powered ESG Extraction & Comparative Benchmarking Platform")
st.caption(
    "Upload one or more sustainability / ESG / BRSR report PDFs. "
    "The tool extracts a fixed set of ESG and financial KPIs (current and previous "
    "year), converts them to standard units, compares the companies, and downloads CSV files."
)

def _stored_mtime():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "reports.json")
    if not os.path.isfile(path):
        return None
    return os.path.getmtime(path)


def _sync_reports():
    mtime = _stored_mtime()
    if st.session_state.get("_reports_mtime") != mtime or "reports" not in st.session_state:
        st.session_state["reports"] = load_reports()
        st.session_state["_reports_mtime"] = mtime


def _persist_reports(reports):
    save_reports(reports)
    st.session_state["reports"] = reports
    st.session_state["_reports_mtime"] = _stored_mtime()


_sync_reports()


def _job_process_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(success and exit_code.value == STILL_ACTIVE)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _job_is_running(job):
    if job.get("status") != "running":
        return False
    pid = job.get("pid")
    if pid is None:
        if is_streamlit_cloud() and st.session_state.get("_inline_step_active"):
            return True
        remaining = remaining_pending_items()
        if remaining:
            next_name = remaining[0][1].get("company", "next PDF")
            save_job(
                status="failed",
                message=f"Extraction paused before {next_name} ({len(remaining)} remaining). Saved reports are available below.",
            )
        else:
            save_job(status="completed", message="Finished.", progress=1.0)
        return False
    if not _job_process_alive(pid):
        remaining = remaining_pending_items()
        if remaining:
            next_name = remaining[0][1].get("company", "next PDF")
            save_job(
                status="failed",
                message=f"Worker stopped at {next_name} ({len(remaining)} remaining). Completed reports are saved below.",
            )
        else:
            save_job(status="failed", message="Background worker stopped before finishing.")
        return False
    return True


def _spawn_background_extraction(items):
    root = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(data_dir(), "job.log")
    with open(log_path, "a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, os.path.join(root, "background_job.py")],
            cwd=root,
            start_new_session=True,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    done_count = sum(1 for item in items if item.get("done"))
    save_job(
        status="running",
        message="Starting one-by-one extraction...",
        progress=done_count / max(len(items), 1),
        pid=process.pid,
        ready_count=sum(1 for r in st.session_state.get("reports", []) if report_ready(r)),
        total_count=len(items),
        current_index=done_count,
        error=None,
    )


@st.fragment(run_every=5)
def _poll_background_job():
    _sync_reports()
    job = load_job()
    if job.get("status") == "running" and _job_is_running(job):
        st.info(
            f"**Scan in progress** — {job.get('message', 'Working...')} "
            "You can close this tab; extraction continues on this computer."
        )
        st.progress(float(job.get("progress") or 0.0))


def _display_name_from_filename(filename: str) -> str:
    stem = os.path.splitext(filename)[0].replace("_", " ").replace("-", " ")
    return " ".join(stem.split()) or "Unknown"


def _unique_company_name(name: str, used: set[str]) -> str:
    base = name.strip() or "Unknown"
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base} ({n})"
        n += 1
    used.add(candidate)
    return candidate


def _safe_download_stem(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name).strip("_")
    return cleaned or "report"


# ---------------------------------------------------------------------
# KPI table helpers — every report has the same rows, keyed by KPI ID
# ---------------------------------------------------------------------
STATUS_LABELS = {
    "reported": "Reported",
    "derived": "Derived",
    "reported_unit_mismatch": "Reported (unit differs)",
    "not_disclosed": "Not disclosed",
}


def _kpi_unit(record: dict) -> str:
    cur, prev = record.get("current") or {}, record.get("previous") or {}
    return cur.get("unit") or prev.get("unit") or record.get("required_unit") or ""


def kpi_frame(kpis: list[dict], company: str | None = None) -> pd.DataFrame:
    """One row per KPI. Every cell is a string, so pyarrow never sees mixed types."""
    rows = []
    for r in kpis or []:
        cur, prev = r.get("current") or {}, r.get("previous") or {}
        change = yoy_change(r)
        row = {"Company": company} if company is not None else {}
        row.update({
            "ID": r["kpi_id"],
            "KPI": r["kpi_name"],
            "Category": r["category"],
            "Current FY": cur.get("fy") or "",
            "Current": format_year(cur),
            "Previous FY": prev.get("fy") or "",
            "Previous": format_year(prev),
            "YoY %": "" if change is None else f"{change:+.1f}%",
            "Unit": _kpi_unit(r),
            "Status": STATUS_LABELS.get(r.get("status"), r.get("status") or ""),
            "Derivation": r.get("derivation") or "",
            "Pages": ", ".join(str(p) for p in r.get("pages") or []),
            "Source": r.get("source_context") or "",
        })
        rows.append(row)
    return pd.DataFrame(rows)


def _df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Encodes a DataFrame into UTF-8 with BOM (utf-8-sig) so Excel never shows mojibake (â€)."""
    if df is None or df.empty:
        return b""
    return df.to_csv(index=False).encode("utf-8-sig")


def _combined_kpi_csv(reports: list[dict]) -> bytes:
    frames = [kpi_frame(report["kpis"], company=report["name"]) for report in reports if report_ready(report)]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return b""
    return _df_to_csv_bytes(pd.concat(frames, ignore_index=True))


def _combined_multi_year_csv(reports: list[dict]) -> bytes:
    df = consolidated_multi_year_frame(reports)
    return _df_to_csv_bytes(df)


def _reports_csv_zip(reports: list[dict], target_fy: str = "FY2026") -> bytes:
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for report in reports:
            if not report_ready(report):
                continue
            stem = _safe_download_stem(report["name"])
            filename = f"{stem}_multi_year.csv"
            n = 2
            while filename in used:
                filename = f"{stem}_multi_year_{n}.csv"
                n += 1
            used.add(filename)
            archive.writestr(filename, _df_to_csv_bytes(company_multi_year_frame(report)))
        # Add Comparative Analysis CSV
        comp_df = comparative_analysis_frame(reports, target_fy=target_fy)
        if not comp_df.empty:
            archive.writestr(f"comparative_analysis_{target_fy}.csv", _df_to_csv_bytes(comp_df))
        # Add Consolidated Multi-Year CSV
        all_df = consolidated_multi_year_frame(reports)
        if not all_df.empty:
            archive.writestr("all_companies_multi_year.csv", _df_to_csv_bytes(all_df))
        # Add Excel Workbook
        try:
            excel_bytes = export_excel_workbook(reports, target_fy=target_fy)
            archive.writestr("ESG_Analysis_Workbook.xlsx", excel_bytes)
        except Exception:
            pass
    return buf.getvalue()


def _coverage_counts(kpis: list[dict]) -> dict:
    cov = kpi_coverage(kpis or [])
    out = {
        "disclosed": f"{cov['disclosed']}/{cov['total']}" if kpis else f"0/{cov['total']}",
        "disclosed_n": cov["disclosed"] if kpis else 0,
        "derived": cov["derived"] if kpis else 0,
    }
    for cat in CATEGORIES:
        n = cov["by_category"][cat] if kpis else 0
        out[cat] = f"{n}/{cov['category_totals'][cat]}"
        out[f"{cat}_n"] = n
    return out


def _report_status(report: dict) -> str:
    if report.get("error"):
        return "Failed"
    if is_legacy_report(report):
        return "Needs re-extraction"
    if report_ready(report):
        return "Ready"
    return "No KPIs found"


def _kpi_cell(record: dict | None, year: str = "current") -> str:
    if not record or not is_disclosed(record):
        return "—"
    slot = record.get(year) or {}
    text = format_year(slot)
    if not text:
        return "—"
    fy = slot.get("fy")
    year_status = record.get("year_status", {}).get(year)
    flag = " *" if year_status == "derived" else ""
    if year_status == "reported_unit_mismatch" and slot.get("unit"):
        flag = f" {slot['unit']} (unit differs)"
    return f"{text}{flag}" + (f" ({fy})" if fy else "")


def _kpi_matrix_rows(reports: list[dict], year: str = "current") -> list[dict]:
    """One row per KPI ID × one column per company."""
    by_company = [(report["name"], {r["kpi_id"]: r for r in report["kpis"]}) for report in reports]
    rows = []
    for kpi in KPI_CATALOG:
        row = {"ID": kpi["id"], "KPI": kpi["name"], "Unit": kpi["required_unit"]}
        for company, found in by_company:
            row[company] = _kpi_cell(found.get(kpi["id"]), year)
        rows.append(row)
    return rows



def _merged_chat_context(reports: list[dict]):
    kpis = []
    topic_index = []
    page_map = {}
    texts = []
    next_page = 1
    for report in reports:
        local_map = load_report_page_map(report)
        remap = {}
        for page in sorted(local_map):
            remap[page] = next_page
            page_map[next_page] = f"[{report['name']}] {local_map[page]}"
            next_page += 1
        for entry in report.get("topic_index") or []:
            topic_index.append({
                **entry,
                "topic": f"{report['name']}: {entry.get('topic', '')}",
                "pages": [remap[page] for page in entry.get("pages", []) if page in remap],
            })
        kpis.extend(report.get("kpis") or [])
        rep_text = load_report_text(report)
        texts.append(f"\n\n===== {report['name']} =====\n\n{rep_text}")
    return kpis, topic_index, page_map, "\n".join(texts)


def _find_existing_report(reports: list[dict], company_name: str, source_file: str):
    for report in reports:
        if not report_ready(report):
            continue
        if source_file and report.get("source_file") == source_file:
            return report
        if company_name and report.get("name") == company_name:
            return report
    return None


def _start_or_resume_queue():
    pending = load_pending_job()
    items = (pending or {}).get("items") or []
    remaining = remaining_pending_items()
    if not remaining:
        return
    if is_streamlit_cloud():
        st.session_state["_inline_auto_continue"] = True
        st.rerun()
    else:
        _spawn_background_extraction(items)
        st.session_state["extract_notice"] = (
            "Resumed one-by-one background extraction. Completed reports remain available below."
        )
        st.rerun()


def render_resume_banner(scan_running: bool):
    if scan_running:
        return
    remaining = remaining_pending_items()
    if not remaining:
        return
    pending = load_pending_job() or {}
    total = len(pending.get("items") or [])
    done_count = total - len(remaining)
    next_index, next_item = remaining[0]
    next_company = next_item.get("company") or next_item.get("source_file") or f"PDF {next_index + 1}"
    remaining_names = ", ".join(
        item.get("company") or item.get("source_file") or f"PDF {idx + 1}"
        for idx, item in remaining
    )
    st.warning(
        f"**Unfinished batch detected ({done_count} of {total} processed).** "
        f"Saved reports are ready below. Remaining in queue: **{remaining_names}**."
    )
    col_resume, col_skip, col_discard = st.columns(3)
    with col_resume:
        if st.button(f"▶️ Resume from {next_company} ({len(remaining)} left)", type="primary", key="btn_resume_queue"):
            _start_or_resume_queue()
    with col_skip:
        if len(remaining) > 1:
            if st.button(f"⏭️ Skip {next_company} & continue", key="btn_skip_next"):
                mark_pending_item_done(next_index)
                _start_or_resume_queue()
    with col_discard:
        if st.button("🗑️ Discard remaining queue", key="btn_discard_queue"):
            clear_pending_job()
            save_job(status="completed", message="Queue cleared. Saved reports kept.", progress=1.0)
            st.rerun()


def render_upload_section():
    uploaded_files = st.file_uploader(
        "Upload one or more ESG / sustainability / BRSR PDFs",
        type=["pdf"],
        accept_multiple_files=True,
    )
    if not uploaded_files:
        st.caption(
            "Select one or more PDFs. PDFs are processed 1 at a time and saved immediately after each file."
        )
        return

    job = load_job()
    scan_running = _job_is_running(job)

    st.caption("Company names start from the file names. Change any that are wrong before extracting.")
    company_names = {}
    for uploaded in uploaded_files:
        company_names[uploaded.file_id] = st.text_input(
            f"Company name for {uploaded.name}",
            value=_display_name_from_filename(uploaded.name),
            key=f"company_name_{uploaded.file_id}",
        )

    if scan_running:
        st.warning("A scan is already running. Completed PDFs appear automatically as each one finishes.")
        return

    existing_reports = st.session_state.get("reports", [])
    already_done = []
    for uploaded in uploaded_files:
        cname = company_names.get(uploaded.file_id, "").strip()
        if _find_existing_report(existing_reports, cname, uploaded.name):
            already_done.append(cname or uploaded.name)

    reextract_existing = False
    if already_done:
        st.info(
            f"Already extracted and saved: **{', '.join(already_done)}**. "
            "By default, these will be kept and skipped so extraction continues from the remaining PDFs."
        )
        reextract_existing = st.checkbox(
            "Re-extract files that already have saved reports",
            value=False,
            key="reextract_existing_chk",
        )

    btn_label = (
        f"Extract remaining reports ({len(uploaded_files) - len(already_done)})"
        if already_done and not reextract_existing and len(uploaded_files) > len(already_done)
        else "Extract all reports (1 PDF at a time)"
    )
    if not st.button(btn_label, type="primary"):
        return

    if reextract_existing:
        _persist_reports([])
        existing_reports = []

    input_dir = job_input_dir()
    if os.path.isdir(input_dir):
        shutil.rmtree(input_dir, ignore_errors=True)
    os.makedirs(input_dir, exist_ok=True)

    used_names = {r.get("name", "") for r in existing_reports if r.get("name") and not reextract_existing}
    pending_items = []
    for index, uploaded in enumerate(uploaded_files):
        raw_name = company_names.get(uploaded.file_id, "").strip() or _display_name_from_filename(uploaded.name)
        matched = None if reextract_existing else _find_existing_report(existing_reports, raw_name, uploaded.name)
        if matched:
            pending_items.append({
                "company": matched["name"],
                "path": "",
                "source_file": uploaded.name,
                "done": True,
            })
            continue

        company_name = _unique_company_name(raw_name, used_names)
        filename = f"{index:03d}.pdf"
        pdf_path = os.path.join(input_dir, filename)
        uploaded.seek(0)
        with open(pdf_path, "wb") as handle:
            shutil.copyfileobj(uploaded, handle)
        pending_items.append({
            "company": company_name,
            "path": pdf_path,
            "source_file": uploaded.name,
            "done": False,
        })

    gc.collect()
    save_pending_job({"items": pending_items})

    to_process = [item for item in pending_items if not item.get("done")]
    if not to_process:
        clear_pending_job()
        st.session_state["extract_notice"] = "All selected PDFs already have saved reports."
        st.rerun()

    if is_streamlit_cloud():
        st.session_state["_inline_auto_continue"] = True
        st.rerun()

    _spawn_background_extraction(pending_items)
    st.session_state.pop("extract_notice", None)
    st.session_state["extract_notice"] = (
        "One-by-one background scan started. Each PDF is saved as soon as it finishes."
    )
    st.rerun()


def _run_next_inline_step(status_container=None):
    target = status_container if status_container is not None else st
    remaining = remaining_pending_items()
    if not remaining:
        st.session_state.pop("_inline_auto_continue", None)
        st.session_state.pop("_inline_step_active", None)
        clear_pending_job()
        reports = load_reports()
        _sync_reports()
        ready_count = sum(1 for report in reports if report_ready(report))
        notice = f"Finished. {ready_count} of {len(reports)} saved reports produced KPIs."
        save_job(
            status="completed",
            progress=1.0,
            message=notice,
            ready_count=ready_count,
            total_count=len(reports),
            current_index=len(reports),
        )
        st.session_state["extract_notice"] = notice
        return

    pending = load_pending_job() or {}
    items = pending.get("items") or []
    total = max(len(items), 1)
    index, item = remaining[0]
    company_name = item.get("company", f"PDF {index + 1}")

    st.session_state["_inline_step_active"] = True
    progress = target.progress(
        index / total,
        text=f"Processing 1 PDF at a time — {company_name} ({index + 1}/{total})...",
    )
    root = os.path.dirname(os.path.abspath(__file__))
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, "background_job.py"), "--single-index", str(index)],
        cwd=root,
    )
    while proc.poll() is None:
        time.sleep(0.5)
        job = load_job()
        progress.progress(
            float(job.get("progress") or (index / total)),
            text=job.get("message") or f"Processing {company_name} ({index + 1}/{total})...",
        )

    st.session_state.pop("_inline_step_active", None)
    _sync_reports()
    gc.collect()

    if proc.returncode != 0:
        st.session_state.pop("_inline_auto_continue", None)
        reports = st.session_state.get("reports", [])
        ready_count = sum(1 for r in reports if report_ready(r))
        save_job(
            status="failed",
            message=(
                f"Stopped while processing {company_name} (PDF {index + 1} of {total}). "
                f"{ready_count} completed report(s) are saved. Click Resume to continue."
            ),
            ready_count=ready_count,
            current_index=index + 1,
            total_count=total,
        )
        st.rerun()

    if not remaining_pending_items():
        st.session_state.pop("_inline_auto_continue", None)
        clear_pending_job()
        reports = st.session_state.get("reports", [])
        ready_count = sum(1 for r in reports if report_ready(r))
        notice = f"Finished. {ready_count} of {len(reports)} saved reports produced KPIs."
        save_job(
            status="completed",
            progress=1.0,
            message=notice,
            ready_count=ready_count,
            total_count=total,
            current_index=total,
        )
        st.session_state["extract_notice"] = notice
    st.rerun()


def render_results_overview(reports: list[dict]):
    notice = st.session_state.pop("extract_notice", None)
    if notice:
        st.success(notice)

    rows = []
    for report in reports:
        counts = _coverage_counts(report.get("kpis") or [])
        rows.append({
            "Company": report["name"],
            "File": report.get("source_file", ""),
            "KPIs disclosed": counts["disclosed"],
            "Derived": str(counts["derived"]),
            **{cat: counts[cat] for cat in CATEGORIES},
            "Status": _report_status(report),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    if any(is_legacy_report(report) for report in reports):
        st.info(
            "Reports marked **Needs re-extraction** were saved before the fixed-KPI format. "
            "Upload those PDFs again to extract the KPIs."
        )

    ready = [report for report in reports if report_ready(report)]
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        if ready:
            excel_bytes = export_excel_workbook(ready, target_fy="FY2026")
            st.download_button(
                "📥 Download Excel Workbook (.xlsx)",
                excel_bytes,
                file_name="ESG_Analysis_Workbook.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_excel_workbook_all",
            )
    with c2:
        if ready:
            st.download_button(
                "📥 Download Consolidated Multi-Year CSV",
                _combined_multi_year_csv(ready),
                file_name="ESG_All_Companies_MultiYear.csv",
                mime="text/csv",
                key="dl_combined_csv",
            )
    with c3:
        if ready:
            st.download_button(
                "📥 Download All Reports (ZIP)",
                _reports_csv_zip(ready),
                file_name="ESG_Reports_Package.zip",
                mime="application/zip",
                key="dl_csv_zip",
            )
    with c4:
        if st.button("🗑️ Clear reports"):
            clear_pending_job()
            clear_all_reports()
            _persist_reports([])
            save_job(status="idle", message="", progress=0.0, pid=None, ready_count=0, total_count=0, current_index=0, error=None)
            st.rerun()

    if ready:
        with st.expander("Download Multi-Year CSV per company"):
            for index, report in enumerate(ready):
                c_multi_df = company_multi_year_frame(report)
                st.download_button(
                    f"CSV — {report['name']} (Multi-Year)",
                    _df_to_csv_bytes(c_multi_df),
                    file_name=f"{_safe_download_stem(report['name'])}_multi_year.csv",
                    mime="text/csv",
                    key=f"dl_one_{index}_{_safe_download_stem(report['name'])}",
                )

    for report in reports:
        if report_ready(report) or is_legacy_report(report):
            continue
        with st.expander(f"{report['name']} — nothing extracted"):
            if report.get("error"):
                st.error(report["error"])
            for entry in report.get("debug_log") or []:
                st.write(f"{entry.get('status')}: {entry.get('detail')}")


# ---------------------------------------------------------------------
# Detail view: the KPIs by category + an AI summary
# ---------------------------------------------------------------------
DETAIL_COLUMNS = ["ID", "KPI", "Current FY", "Current", "Previous FY", "Previous", "YoY %", "Unit", "Status", "Pages"]


def render_detail_tab(name: str, data: dict):
    kpis = data["kpis"]
    df = kpi_frame(kpis)
    multi_df = company_multi_year_frame(data)
    cov = kpi_coverage(kpis)

    st.subheader(f"{name} — Multi-Year KPI Trend")
    st.caption(
        f"{cov['disclosed']}/{cov['total']} KPIs disclosed ({cov['derived']} derived). "
        "Side-by-side historical comparison across financial years."
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        st.download_button(
            "📥 Download Multi-Year CSV",
            _df_to_csv_bytes(multi_df),
            file_name=f"{_safe_download_stem(name)}_multi_year.csv",
            mime="text/csv",
            key=f"dl_multi_csv_{name}"
        )
    with col2:
        st.download_button(
            "📥 Download Standard CSV",
            _df_to_csv_bytes(df),
            file_name=f"{_safe_download_stem(name)}_kpis.csv",
            mime="text/csv",
            key=f"dl_csv_{name}"
        )
    with col3:
        st.download_button(
            "📥 Download JSON",
            json.dumps(kpis, indent=2),
            file_name=f"{_safe_download_stem(name)}_kpis.json",
            mime="application/json",
            key=f"dl_json_{name}"
        )

    # Show primary Multi-Year Table requested by the user
    st.dataframe(multi_df, width="stretch", hide_index=True)

    for cat in CATEGORIES:
        cat_multi_df = multi_df[multi_df["Env./Social/Gov."] == cat]
        n = cov["by_category"][cat]
        with st.expander(f"{cat} Breakdown — {n}/{cov['category_totals'][cat]} disclosed", expanded=False):
            st.dataframe(cat_multi_df, width="stretch", hide_index=True)

    derived_rows = df[df["Derivation"] != ""]
    if not derived_rows.empty:
        with st.expander(f"How derived values were computed ({len(derived_rows)})"):
            for _, row in derived_rows.iterrows():
                st.markdown(f"**{row['ID']} {row['KPI']}** — {row['Derivation']}")

    if cov["missing"]:
        st.warning(
            "Not disclosed: " + ", ".join(f"{r['kpi_id']} {r['kpi_name']}" for r in cov["missing"])
        )

    st.subheader("🤖 AI Summary")
    if st.button("Generate summary", key=f"summary_btn_{name}"):
        with st.spinner("Writing summary..."):
            data["summary"] = generate_summary(company_name=name, kpis=kpis)
        _persist_reports(st.session_state["reports"])
    if data.get("summary"):
        st.markdown(data["summary"])



# ---------------------------------------------------------------------
# Chat tab: tiered Q&A with visible progress + running history
# ---------------------------------------------------------------------
def render_chat_tab(name: str, data: dict):
    st.subheader(f"💬 Ask about {name}")
    st.caption(
        "Answers start from the extracted KPI table (fast). If that's "
        "not enough — e.g. asking about a program or policy, not just a "
        "number — it looks it up in the topic index built at upload time, "
        "and only does a full report scan as a last resort."
    )

    for turn in data["chat_history"]:
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            st.write(turn["answer"])
            st.caption(f"Answered from: {['KPI table', 'topic index lookup', 'full report scan'][turn['tier'] - 1]}")

    question = st.chat_input(f"Ask something about {name}...", key=f"chat_input_{name}")
    if question:
        with st.chat_message("user"):
            st.write(question)

        progress_bar = st.progress(0, text="Thinking...")

        def _chat_progress(current, total, status_text):
            progress_bar.progress(current / max(total, 1), text=status_text)

        rep_page_map = load_report_page_map(data)
        rep_full_text = load_report_text(data)
        result = answer_chat_question(
            question=question,
            company_name=name,
            kpis=data["kpis"],
            topic_index=data["topic_index"],
            page_map=rep_page_map,
            full_text=rep_full_text,
            chat_history=data["chat_history"],
            progress_callback=_chat_progress,
        )
        progress_bar.empty()

        with st.chat_message("assistant"):
            st.write(result["answer"])
            tier_label = ["KPI table", "topic index lookup", "full report scan"][result["tier"] - 1]
            st.caption(f"Answered from: {tier_label}")
            with st.expander("Debug: how this was answered"):
                for d in result["debug"]:
                    st.write(f"Tier {d['tier']} — {d['status']}: {d['detail']}")

        data["chat_history"].append({"question": question, "answer": result["answer"], "tier": result["tier"]})
        _persist_reports(st.session_state["reports"])
        st.rerun()


# ---------------------------------------------------------------------
# Report tab: selectable multi-page report (cover, one page per KPI
# category, AI summary, raw data appendix), styled with the muted palette.
# ---------------------------------------------------------------------
REPORT_PALETTE = {
    "environment": "#5B8C6E",
    "social": "#5B7C99",
    "governance": "#6B5B95",
    "financial": "#8C6D46",
    "disclosed": "#6B9E78",
    "partial": "#C9A15A",
    "missing": "#B56B6B",
    "neutral": "#9AA0A6",
    "bg": "#FAFAF8",
    "text": "#2B2B2B",
    "border": "#E0E0DD",
    "accent": "#2D6A4F",
}

STATUS_COLORS = {
    "Reported": REPORT_PALETTE["disclosed"],
    "Derived": REPORT_PALETTE["partial"],
    "Reported (unit differs)": REPORT_PALETTE["neutral"],
    "Not disclosed": REPORT_PALETTE["missing"],
}


def _status_counts(kpis: list[dict]) -> dict:
    counts = {label: 0 for label in STATUS_LABELS.values()}
    for r in kpis:
        counts[STATUS_LABELS.get(r.get("status"), "Not disclosed")] += 1
    return counts


def _yoy_rows(kpis: list[dict], category: str) -> list[dict]:
    """KPIs in this category with a current AND previous number, for the YoY chart."""
    rows = []
    for r in kpis:
        if r["category"] != category or not is_disclosed(r):
            continue
        change = yoy_change(r)
        if change is None:
            continue
        direction = KPI_BY_ID[r["kpi_id"]].get("direction")
        if direction == "higher":
            verdict = "better" if change > 0 else "worse" if change < 0 else "flat"
        elif direction == "lower":
            verdict = "better" if change < 0 else "worse" if change > 0 else "flat"
        else:
            verdict = "neutral"
        rows.append({"label": f"{r['kpi_id']} {r['kpi_name']}"[:45], "change": change, "verdict": verdict})
    return sorted(rows, key=lambda x: x["change"])


_VERDICT_COLORS = {
    "better": REPORT_PALETTE["disclosed"],
    "worse": REPORT_PALETTE["missing"],
    "flat": REPORT_PALETTE["neutral"],
    "neutral": REPORT_PALETTE["neutral"],
}


def _yoy_explanation(rows: list[dict], category: str) -> str:
    if not rows:
        return f"No {category.lower()} KPI has both a current and a previous-year number to compare."
    better = sum(1 for r in rows if r["verdict"] == "better")
    worse = sum(1 for r in rows if r["verdict"] == "worse")
    return (
        f"{len(rows)} {category.lower()} KPIs have both years. {better} moved in the better direction, "
        f"{worse} in the worse direction (green = better, red = worse, grey = no preferred direction)."
    )


def _plotly_yoy_chart(rows, category, key_suffix=""):
    if not rows or not PLOTLY_AVAILABLE:
        return
    fig = go.Figure(go.Bar(
        x=[r["change"] for r in rows], y=[r["label"] for r in rows], orientation="h",
        marker_color=[_VERDICT_COLORS[r["verdict"]] for r in rows],
        text=[f"{r['change']:+.1f}%" for r in rows], textposition="outside",
    ))
    fig.update_layout(
        plot_bgcolor=REPORT_PALETTE["bg"], paper_bgcolor=REPORT_PALETTE["bg"],
        font_color=REPORT_PALETTE["text"], margin=dict(l=10, r=40, t=10, b=10),
        height=max(240, 34 * len(rows)),
        xaxis=dict(gridcolor=REPORT_PALETTE["border"], title="Change vs previous year (%)"),
    )
    st.plotly_chart(fig, width='stretch', key=f"chart_yoy_{category}_{key_suffix}")


def _plotly_status_donut(kpis, key_suffix=""):
    if not PLOTLY_AVAILABLE:
        return
    counts = _status_counts(kpis)
    labels = [k for k, v in counts.items() if v]
    fig = go.Figure(data=[go.Pie(
        labels=labels, values=[counts[k] for k in labels], hole=0.55,
        marker=dict(colors=[STATUS_COLORS[k] for k in labels]), sort=False,
    )])
    fig.update_layout(
        paper_bgcolor=REPORT_PALETTE["bg"], font_color=REPORT_PALETTE["text"],
        margin=dict(l=10, r=10, t=10, b=10), height=300,
        annotations=[dict(text=f"{len(KPI_CATALOG)} KPIs", x=0.5, y=0.5, showarrow=False)],
    )
    st.plotly_chart(fig, width='stretch', key=f"chart_donut_{key_suffix}")


def _report_card(title: str, value: str, color: str, subtitle: str = ""):
    p = REPORT_PALETTE
    st.markdown(
        f"""
        <div style="background:{p['bg']}; border:1px solid {p['border']};
                    border-radius:6px; padding:18px 20px; margin-bottom:10px;">
            <div style="font-size:13px; color:{p['text']}; opacity:0.65;
                        text-transform:uppercase; letter-spacing:0.04em;">{title}</div>
            <div style="font-size:28px; font-weight:500; color:{color}; margin-top:4px;">{value}</div>
            {f'<div style="font-size:13px; color:{p["text"]}; opacity:0.6; margin-top:2px;">{subtitle}</div>' if subtitle else ''}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _report_page_cover(name: str, data: dict):
    p = REPORT_PALETTE
    kpis = data["kpis"]
    cov = kpi_coverage(kpis)

    st.markdown(
        f"""
        <div style="background:{p['bg']}; border:1px solid {p['border']}; border-radius:8px;
                    padding:32px; margin-bottom:20px;">
            <div style="font-size:14px; letter-spacing:0.08em; text-transform:uppercase;
                        color:{p['accent']}; opacity:0.85;">ESG KPI Snapshot</div>
            <div style="font-size:34px; font-weight:500; color:{p['text']}; margin-top:6px;">{_html_escape(name)}</div>
            <div style="font-size:14px; color:{p['text']}; opacity:0.6; margin-top:4px;">
                {cov['disclosed']}/{cov['total']} KPIs disclosed &nbsp;•&nbsp; {cov['derived']} derived
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cols = st.columns(len(CATEGORIES))
    for col, cat in zip(cols, CATEGORIES):
        with col:
            _report_card(cat, f"{cov['by_category'][cat]}/{cov['category_totals'][cat]}",
                         p[cat.lower()], "KPIs disclosed")

    if PLOTLY_AVAILABLE:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        _plotly_status_donut(kpis, key_suffix=name)

    if cov["missing"]:
        st.markdown("**Not disclosed**")
        st.markdown(", ".join(f"{r['kpi_id']} {r['kpi_name']}" for r in cov["missing"]))


def _report_page_category(name: str, data: dict, category: str):
    p = REPORT_PALETTE
    color = p[category.lower()]
    kpis = [r for r in data["kpis"] if r["category"] == category]
    disclosed = [r for r in kpis if is_disclosed(r)]

    st.markdown(
        f"""<div style="border-left:4px solid {color}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">{category}</span>
                <span style="font-size:14px; color:{p['text']}; opacity:0.6;"> — {len(disclosed)}/{len(kpis)} KPIs disclosed</span>
            </div>""",
        unsafe_allow_html=True,
    )

    df = kpi_frame(kpis)
    st.dataframe(df[DETAIL_COLUMNS], width="stretch", hide_index=True)

    rows = _yoy_rows(kpis, category)
    _plotly_yoy_chart(rows, category, key_suffix=name)
    st.caption(_yoy_explanation(rows, category))

    derived = [r for r in kpis if r.get("derivation")]
    if derived:
        with st.expander(f"Derived values ({len(derived)})"):
            for r in derived:
                st.markdown(f"**{r['kpi_id']} {r['kpi_name']}** — {r['derivation']}")


def _report_page_ai_summary(name: str, data: dict):
    p = REPORT_PALETTE
    st.markdown(
        f"""<div style="border-left:4px solid {p['accent']}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">AI Executive Summary</span>
            </div>""",
        unsafe_allow_html=True,
    )
    if st.button("Generate summary", key=f"report_summary_btn_{name}"):
        with st.spinner("Writing summary..."):
            data["summary"] = generate_summary(company_name=name, kpis=data["kpis"])
        _persist_reports(st.session_state["reports"])

    if data.get("summary"):
        st.markdown(data["summary"])
    else:
        st.caption("Click \"Generate summary\" to produce an AI-written executive summary for this report page.")


def _report_page_appendix(name: str, data: dict):
    p = REPORT_PALETTE
    kpis = data["kpis"]
    df = kpi_frame(kpis)
    st.markdown(
        f"""<div style="border-left:4px solid {p['text']}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">Raw Data Appendix</span>
            </div>""",
        unsafe_allow_html=True,
    )
    st.dataframe(df, width='stretch', hide_index=True)
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Download CSV", df.to_csv(index=False), file_name=f"{name}_kpis.csv", key=f"report_dl_csv_{name}")
    with col2:
        st.download_button("Download JSON", json.dumps(kpis, indent=2), file_name=f"{name}_kpis.json", key=f"report_dl_json_{name}")


def _safe_pdf_text(text) -> str:
    """fpdf2's built-in fonts only support Latin-1; swap anything else out
    rather than crash the whole PDF build over one unusual character."""
    return str(text).replace("—", "-").replace("·", "|").replace("×", "x").encode("latin-1", "replace").decode("latin-1")


def _mpl_png(fig):
    fig.patch.set_facecolor(REPORT_PALETTE["bg"])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _mpl_yoy_chart(rows):
    if not MATPLOTLIB_AVAILABLE or not rows:
        return None
    fig, ax = plt.subplots(figsize=(6.2, max(2, 0.4 * len(rows))))
    ax.barh([r["label"] for r in rows], [r["change"] for r in rows],
            color=[_VERDICT_COLORS[r["verdict"]] for r in rows])
    ax.axvline(0, color=REPORT_PALETTE["border"], linewidth=1)
    ax.set_xlabel("Change vs previous year (%)", color=REPORT_PALETTE["text"], fontsize=8)
    ax.set_facecolor(REPORT_PALETTE["bg"])
    ax.tick_params(colors=REPORT_PALETTE["text"], labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(REPORT_PALETTE["border"])
    return _mpl_png(fig)


def _mpl_status_donut(kpis):
    if not MATPLOTLIB_AVAILABLE:
        return None
    counts = _status_counts(kpis)
    labels = [k for k, v in counts.items() if v]
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.pie(
        [counts[k] for k in labels], labels=labels, colors=[STATUS_COLORS[k] for k in labels],
        wedgeprops=dict(width=0.45), textprops={"color": REPORT_PALETTE["text"], "fontsize": 8},
    )
    return _mpl_png(fig)


def build_report_pdf(name: str, data: dict, selected_pages: list) -> bytes | None:
    """Assembles the selected report sections into a downloadable PDF —
    same content and colors as the on-screen version, with static charts."""
    if not FPDF_AVAILABLE:
        return None

    kpis = data["kpis"]
    cov = kpi_coverage(kpis)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    tmpdir = tempfile.mkdtemp()

    def _para(w, h, text):
        # fpdf2 leaves the cursor at the right edge after multi_cell by
        # default, so the next one would have no width; return to the margin.
        pdf.multi_cell(w, h, text, new_x="LMARGIN", new_y="NEXT")

    def add_image(buf, w=170):
        if buf is None:
            return
        path = os.path.join(tmpdir, f"chart_{len(os.listdir(tmpdir))}.png")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        pdf.image(path, w=w)

    if "Cover / Snapshot" in selected_pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(0, 12, _safe_pdf_text(f"{name} - ESG KPI Report"), ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 8, _safe_pdf_text(f"{cov['disclosed']}/{cov['total']} KPIs disclosed | {cov['derived']} derived"), ln=True)
        for cat in CATEGORIES:
            pdf.cell(0, 6, _safe_pdf_text(f"{cat}: {cov['by_category'][cat]}/{cov['category_totals'][cat]}"), ln=True)
        pdf.ln(4)
        add_image(_mpl_status_donut(kpis), w=100)
        if cov["missing"]:
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 8, "Not disclosed", ln=True)
            pdf.set_font("Helvetica", "", 9)
            _para(0, 5, _safe_pdf_text(", ".join(f"{r['kpi_id']} {r['kpi_name']}" for r in cov["missing"])))

    for cat in CATEGORIES:
        if cat not in selected_pages:
            continue
        cat_kpis = [r for r in kpis if r["category"] == cat]
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _safe_pdf_text(cat), ln=True)
        for r in cat_kpis:
            cur, prev = r["current"], r["previous"]
            pdf.set_font("Helvetica", "B", 9)
            _para(0, 5, _safe_pdf_text(f"{r['kpi_id']} {r['kpi_name']}  [{STATUS_LABELS.get(r['status'], r['status'])}]"))
            pdf.set_font("Helvetica", "", 9)
            if is_disclosed(r):
                line = (
                    f"   {cur.get('fy') or 'Current'}: {format_year(cur) or '-'}   |   "
                    f"{prev.get('fy') or 'Previous'}: {format_year(prev) or '-'}   ({_kpi_unit(r)})"
                )
                _para(0, 5, _safe_pdf_text(line))
                if r.get("derivation"):
                    pdf.set_font("Helvetica", "I", 8)
                    _para(0, 4, _safe_pdf_text(f"   Derived: {r['derivation']}"))
        rows = _yoy_rows(cat_kpis, cat)
        if rows:
            pdf.ln(3)
            pdf.set_font("Helvetica", "", 10)
            _para(0, 6, _safe_pdf_text(_yoy_explanation(rows, cat)))
            add_image(_mpl_yoy_chart(rows))

    if "AI Summary" in selected_pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Executive Summary", ln=True)
        pdf.set_font("Helvetica", "", 10)
        summary = data.get("summary")
        _para(
            0, 6,
            _safe_pdf_text(summary) if summary else
            "No AI summary was generated for this report. Generate it in the app's "
            "AI Summary tab before exporting, and it will appear here.",
        )

    raw = pdf.output(dest="S")
    # fpdf2's return type has varied across versions (str vs bytearray) —
    # handle both so this doesn't break on a version bump.
    if isinstance(raw, str):
        raw = raw.encode("latin-1")
    return bytes(raw)


REPORT_PAGES = {
    "Cover / Snapshot": _report_page_cover,
    **{cat: (lambda c: (lambda nm, d: _report_page_category(nm, d, c)))(cat) for cat in CATEGORIES},
    "AI Summary": _report_page_ai_summary,
    "Raw Data Appendix": _report_page_appendix,
}


# ---------------------------------------------------------------------
# Styled HTML executive report — a separate download option alongside
# the chart-based PDF above. Open the file in a browser and use
# "Print -> Save as PDF" for an actual PDF.
# ---------------------------------------------------------------------
HTML_PALETTE = {
    "forest": "#0d3b2e",
    "sage": "#6b9080",
    "cream": "#f4f6f0",
    "card": "#ffffff",
    "border": "#e2e8f0",
    "win_bg": "#dcfce7", "win_text": "#15803d",
    "warn_bg": "#fef9c3", "warn_text": "#a16207",
    "bad_bg": "#fee2e2", "bad_text": "#b91c1c",
}

_HTML_STYLE = """
* {{ box-sizing: border-box; }}
body {{ font-family: 'Plus Jakarta Sans', -apple-system, sans-serif; color: {forest}; background: {cream}; font-size: 14px; line-height: 1.5; margin: 0; padding: 24px; }}
.header-card {{ background: linear-gradient(135deg, {forest} 0%, #1e4d3e 60%, {sage} 100%); color: #fff; padding: 24px; border-radius: 10px; margin-bottom: 20px; }}
.header-title {{ font-size: 24px; font-weight: 800; margin: 0 0 4px 0; }}
.header-meta {{ font-size: 12px; color: #e2e8f0; }}
.card {{ background: {card}; border: 1px solid {border}; border-radius: 10px; padding: 18px; margin-bottom: 18px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ background: {forest}; color: #fff; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; font-size: 13px; vertical-align: top; }}
td.sub {{ font-size: 11px; color: #64748b; }}
.pill {{ padding: 2px 10px; border-radius: 12px; font-weight: 700; font-size: 11px; white-space: nowrap; }}
.pill-win {{ background: {win_bg}; color: {win_text}; }}
.pill-warn {{ background: {warn_bg}; color: {warn_text}; }}
.pill-bad {{ background: {bad_bg}; color: {bad_text}; }}
h2 {{ font-size: 16px; margin-top: 0; }}
@media print {{ body {{ background: #fff; }} }}
"""


def _html_escape(text) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _status_pill(status: str) -> str:
    label = STATUS_LABELS.get(status, status or "")
    css = {"reported": "pill-win", "derived": "pill-warn"}.get(status, "pill-bad")
    return f'<span class="pill {css}">{_html_escape(label)}</span>'


def _html_year(slot: dict) -> str:
    text = format_year(slot)
    if not text:
        return "—"
    fy = slot.get("fy")
    return _html_escape(text) + (f'<br><span style="font-size:11px;color:#64748b;">{_html_escape(fy)}</span>' if fy else "")


def _build_executive_html(name: str, data: dict) -> str:
    """Single-company styled HTML KPI report."""
    kpis = data["kpis"]
    cov = kpi_coverage(kpis)
    p = HTML_PALETTE

    sections = ""
    for cat in CATEGORIES:
        rows_html = ""
        for r in [r for r in kpis if r["category"] == cat]:
            change = yoy_change(r)
            rows_html += f"""<tr>
                <td><strong>{_html_escape(r['kpi_id'])}</strong></td>
                <td>{_html_escape(r['kpi_name'])}<br><span style="font-size:11px;color:#64748b;">{_html_escape(_kpi_unit(r))}</span></td>
                <td>{_html_year(r['current'])}</td>
                <td>{_html_year(r['previous'])}</td>
                <td>{'—' if change is None else f'{change:+.1f}%'}</td>
                <td>{_status_pill(r['status'])}</td>
            </tr>"""
            if r.get("derivation"):
                rows_html += f'<tr><td></td><td class="sub" colspan="5">Derived: {_html_escape(r["derivation"])}</td></tr>'
        sections += f"""<div class="card">
  <h2>{cat} — {cov['by_category'][cat]}/{cov['category_totals'][cat]} disclosed</h2>
  <table><thead><tr><th>ID</th><th>KPI</th><th>Current</th><th>Previous</th><th>YoY</th><th>Status</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
</div>"""

    gap_rows = "".join(
        f"<li>{_html_escape(r['kpi_id'])} {_html_escape(r['kpi_name'])}</li>" for r in cov["missing"]
    ) or "<li>None — every KPI has a disclosed value.</li>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html_escape(name)} — ESG KPI Report</title>
<style>{_HTML_STYLE.format(**p)}</style></head>
<body>
<div class="header-card">
  <div style="font-size:11px; text-transform:uppercase; color:#a3e635; font-weight:700;">ESG KPI Report</div>
  <div class="header-title">{_html_escape(name)}</div>
  <div class="header-meta">{cov['disclosed']}/{cov['total']} KPIs disclosed · {cov['derived']} derived (unit-converted or computed from stated figures)</div>
</div>
{sections}
<div class="card">
  <h2>Not disclosed</h2>
  <ul>{gap_rows}</ul>
</div>
<p style="font-size:11px; color:#64748b;">Open this file in a browser and use Print → Save as PDF for a PDF copy.</p>
</body></html>"""


def _comparable(ra: dict, rb: dict):
    """Both current headline numbers in the same unit -> (a, b), else None."""
    if not (is_disclosed(ra) and is_disclosed(rb)):
        return None
    if "reported_unit_mismatch" in (ra.get("status"), rb.get("status")):
        return None
    va, vb = headline_value(ra["current"]), headline_value(rb["current"])
    if va is None or vb is None:
        return None
    if (ra["current"].get("unit") or "") != (rb["current"].get("unit") or ""):
        return None
    return va, vb


def _build_comparison_html(company_a: str, data_a: dict, company_b: str, data_b: dict) -> str:
    """Two-company styled HTML comparison across all KPIs."""
    p = HTML_PALETTE
    by_a = {r["kpi_id"]: r for r in data_a["kpis"]}
    by_b = {r["kpi_id"]: r for r in data_b["kpis"]}

    rows_html = ""
    for kpi in KPI_CATALOG:
        ra, rb = by_a.get(kpi["id"]), by_b.get(kpi["id"])
        if not ra or not rb:
            continue
        winner_html, delta_html = "—", "—"
        pair = _comparable(ra, rb)
        if pair:
            va, vb = pair
            if vb != 0:
                delta_html = f"{(va - vb) / abs(vb) * 100:+.1f}%"
            direction = kpi.get("direction")
            winner = None
            if direction == "lower":
                winner = company_a if va < vb else company_b if vb < va else None
            elif direction == "higher":
                winner = company_a if va > vb else company_b if vb > va else None
            if winner:
                winner_html = f'<span class="pill pill-win">{_html_escape(winner)}</span>'
        rows_html += f"""<tr>
            <td><strong>{_html_escape(kpi['id'])}</strong></td>
            <td>{_html_escape(kpi['name'])}<br><span style="font-size:11px;color:#64748b;">{_html_escape(kpi['required_unit'])}</span></td>
            <td>{_html_year(ra['current']) if is_disclosed(ra) else '—'}</td>
            <td>{_html_year(rb['current']) if is_disclosed(rb) else '—'}</td>
            <td>{delta_html}</td>
            <td>{winner_html}</td>
        </tr>"""

    cov_a, cov_b = kpi_coverage(data_a["kpis"]), kpi_coverage(data_b["kpis"])
    cov_rows = "".join(
        f"<tr><td>{cat}</td><td>{cov_a['by_category'][cat]}/{cov_a['category_totals'][cat]}</td>"
        f"<td>{cov_b['by_category'][cat]}/{cov_b['category_totals'][cat]}</td></tr>"
        for cat in CATEGORIES
    )
    cov_rows += f"<tr><td><strong>Total</strong></td><td><strong>{cov_a['disclosed']}/{cov_a['total']}</strong></td><td><strong>{cov_b['disclosed']}/{cov_b['total']}</strong></td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html_escape(company_a)} vs {_html_escape(company_b)} — ESG KPI Comparison</title>
<style>{_HTML_STYLE.format(**p)}</style></head>
<body>
<div class="header-card">
  <div style="font-size:11px; text-transform:uppercase; color:#a3e635; font-weight:700;">Executive Benchmarking Brief</div>
  <div class="header-title">{_html_escape(company_a)} vs. {_html_escape(company_b)}</div>
  <div class="header-meta">{len(KPI_CATALOG)} KPIs, current year. Delta and leader only where both values are in the same unit; leader uses each KPI's better direction (e.g. lower emission intensity, higher renewable share).</div>
</div>
<div class="card">
  <h2>Head-to-Head KPIs</h2>
  <table><thead><tr><th>ID</th><th>KPI</th><th>{_html_escape(company_a)}</th><th>{_html_escape(company_b)}</th><th>Delta</th><th>Leader</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
</div>
<div class="card">
  <h2>Disclosure coverage</h2>
  <table><thead><tr><th>Category</th><th>{_html_escape(company_a)}</th><th>{_html_escape(company_b)}</th></tr></thead>
  <tbody>{cov_rows}</tbody></table>
</div>
<p style="font-size:11px; color:#64748b;">Open this file in a browser and use Print → Save as PDF for a PDF copy.</p>
</body></html>"""


def render_report_tab(name: str, data: dict):
    st.caption("Pick any section below — build only the pages you need, or use a quick preset.")

    presets = {
        "Full report": list(REPORT_PAGES.keys()),
        "Environment only": ["Cover / Snapshot", "Environment"],
        "Social only": ["Cover / Snapshot", "Social"],
        "Governance & financial": ["Cover / Snapshot", "Governance", "Financial"],
    }
    preset_cols = st.columns(len(presets))
    for col, (label, pages) in zip(preset_cols, presets.items()):
        with col:
            if st.button(label, key=f"preset_{label}_{name}", width='stretch'):
                st.session_state[f"report_sections_{name}"] = pages
                st.rerun()

    page_names = list(REPORT_PAGES.keys())
    # A selection saved under the old page names would make the multiselect raise.
    saved = st.session_state.get(f"report_sections_{name}")
    if saved is not None and any(page not in page_names for page in saved):
        st.session_state[f"report_sections_{name}"] = [page for page in saved if page in page_names] or page_names
    selected_pages = st.multiselect(
        "Sections to include",
        page_names,
        default=page_names,
        key=f"report_sections_{name}",
    )
    if not selected_pages:
        st.info("Select at least one section above to view the report.")
        return

    page_tabs = st.tabs(selected_pages)
    for tab, page_name in zip(page_tabs, selected_pages):
        with tab:
            REPORT_PAGES[page_name](name, data)

    st.markdown("---")
    st.subheader("Export")
    if not FPDF_AVAILABLE or not MATPLOTLIB_AVAILABLE:
        st.info(
            "PDF export needs two extra packages. In your venv, run:\n\n"
            "`pip install fpdf2 matplotlib`\n\nthen restart the app."
        )
    else:
        if st.button("📥 Build PDF report", key=f"prep_pdf_{name}"):
            with st.spinner("Building PDF..."):
                pdf_bytes = build_report_pdf(name, data, selected_pages)
            if pdf_bytes:
                st.session_state[f"report_pdf_bytes_{name}"] = pdf_bytes
            else:
                st.error("Couldn't build the PDF — check that fpdf2 and matplotlib are installed correctly.")

        if st.session_state.get(f"report_pdf_bytes_{name}"):
            st.download_button(
                "Download report as PDF",
                st.session_state[f"report_pdf_bytes_{name}"],
                file_name=f"{name}_ESG_KPI_Report.pdf",
                mime="application/pdf",
                key=f"dl_pdf_{name}",
            )

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.download_button(
        "🎨 Download styled HTML report (Forest Pine design)",
        _build_executive_html(name, data),
        file_name=f"{name}_ESG_KPI_Report.html",
        mime="text/html",
        key=f"dl_html_{name}",
        help="No extra installs needed. Open the downloaded file in any browser, then use Print → Save as PDF for a PDF copy.",
    )


def render_compare_tab():
    reports = [report for report in st.session_state["reports"] if report_ready(report)]
    if len(reports) < 2:
        st.info("Extract at least two reports to compare them. A single report can still be downloaded as CSV from All companies.")
        return

    names = [report["name"] for report in reports]
    st.caption("Comparing " + ", ".join(names) + ".")

    summary_rows = []
    chart_rows = []
    for report in reports:
        counts = _coverage_counts(report["kpis"])
        summary_rows.append({
            "Company": report["name"],
            "KPIs disclosed": counts["disclosed"],
            "Derived": str(counts["derived"]),
            **{cat: counts[cat] for cat in CATEGORIES},
        })
        chart_rows.append({"Company": report["name"], **{cat: counts[f"{cat}_n"] for cat in CATEGORIES}})
    st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True)
    st.bar_chart(pd.DataFrame(chart_rows).set_index("Company")[list(CATEGORIES)])

    st.subheader("📊 Comparative Analysis Across Companies")
    found_years = set(["FY2024", "FY2025", "FY2026"])
    for report in reports:
        dom_c, dom_p = extract_report_dominant_years(report)
        if dom_c:
            found_years.add(dom_c)
        if dom_p:
            found_years.add(dom_p)
        for r in report.get("kpis") or []:
            for slot in (r.get("current"), r.get("previous")):
                if slot and slot.get("fy"):
                    s = standardize_fy(slot.get("fy"))
                    if s:
                        found_years.add(s)
            for y_k in (r.get("years") or {}).keys():
                s = standardize_fy(y_k)
                if s:
                    found_years.add(s)
    available_years = sorted(found_years)
    default_idx = available_years.index("FY2026") if "FY2026" in available_years else len(available_years) - 1

    col_fy, col_dl1, col_dl2 = st.columns([2, 2, 2])
    with col_fy:
        selected_fy = st.selectbox("Select Financial Year for Comparison", available_years, index=default_idx, key="compare_fy_select")

    comp_df = comparative_analysis_frame(reports, target_fy=selected_fy)
    with col_dl1:
        st.download_button(
            f"📥 Download CSV ({selected_fy})",
            _df_to_csv_bytes(comp_df),
            file_name=f"comparative_analysis_{selected_fy}.csv",
            mime="text/csv",
            key="dl_comp_csv_btn",
        )
    with col_dl2:
        excel_bytes = export_excel_workbook(reports, target_fy=selected_fy)
        st.download_button(
            "📥 Download Excel Workbook (.xlsx)",
            excel_bytes,
            file_name=f"ESG_Comparative_Analysis_{selected_fy}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="dl_comp_excel_btn",
        )

    st.dataframe(comp_df, width="stretch", hide_index=True)
    st.caption(f"Showing KPIs across all companies for {selected_fy}. Columns: S. No. | KPI Name | Unit | [Companies]. '—' indicates not disclosed for that financial year.")

    st.subheader("Look up one KPI")
    options = [f"{k['id']} — {k['name']}" for k in KPI_CATALOG]
    chosen = st.selectbox("KPI", options, key="compare_metric_select")
    chosen_id = chosen.split(" — ", 1)[0]
    rows = []
    for report in reports:
        match = next((r for r in report["kpis"] if r["kpi_id"] == chosen_id), None)
        if match and is_disclosed(match):
            rows.append({
                "Company": report["name"],
                "Current FY": match["current"].get("fy") or "",
                "Current": format_year(match["current"]),
                "Previous FY": match["previous"].get("fy") or "",
                "Previous": format_year(match["previous"]),
                "Unit": _kpi_unit(match),
                "Status": STATUS_LABELS.get(match["status"], match["status"]),
                "Derivation": match.get("derivation") or "",
            })
        else:
            rows.append({"Company": report["name"], "Current FY": "", "Current": "Not disclosed", "Previous FY": "",
                         "Previous": "", "Unit": "", "Status": "Not disclosed", "Derivation": ""})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.subheader("Styled two-company report")
    if len(names) == 2:
        left_name, right_name = names
    else:
        pick_left, pick_right = st.columns(2)
        with pick_left:
            left_name = st.selectbox("First company", names, key="cmp_left")
        with pick_right:
            right_name = st.selectbox("Second company", names, index=1, key="cmp_right")
    if left_name == right_name:
        st.caption("Pick two different companies for the styled report.")
    else:
        data_left = next(report for report in reports if report["name"] == left_name)
        data_right = next(report for report in reports if report["name"] == right_name)
        st.download_button(
            "Download styled HTML comparison",
            _build_comparison_html(left_name, data_left, right_name, data_right),
            file_name=f"{_safe_download_stem(left_name)}_vs_{_safe_download_stem(right_name)}_KPI_Comparison.html",
            mime="text/html",
            key="dl_compare_html",
            help="Open the file in a browser, then use Print → Save as PDF.",
        )

    st.subheader("Ask a comparison question")
    st.caption("Grounded in every extracted report.")
    combined_kpis, topic_index, page_map, full_text = _merged_chat_context(reports)
    compare_question = st.chat_input("e.g. Which company has the lower emission intensity?", key="compare_chat_input")
    if compare_question:
        with st.chat_message("user"):
            st.write(compare_question)
        progress_bar = st.progress(0, text="Thinking...")

        def _cmp_progress(current, total, status_text):
            progress_bar.progress(current / max(total, 1), text=status_text)

        result = answer_chat_question(
            question=compare_question,
            company_name=" vs ".join(names),
            kpis=combined_kpis,
            topic_index=topic_index,
            page_map=page_map,
            full_text=full_text,
            chat_history=[],
            progress_callback=_cmp_progress,
        )
        progress_bar.empty()
        with st.chat_message("assistant"):
            st.write(result["answer"])



def render_company_tab():
    ready = [report for report in st.session_state["reports"] if report_ready(report)]
    if not ready:
        st.info("No report produced KPIs to open.")
        return

    selected = st.selectbox("Company", [report["name"] for report in ready], key="open_company")
    data = next(report for report in ready if report["name"] == selected)
    detail_tab, chat_tab, report_tab = st.tabs(["Detail", "Chat", "Report"])
    with detail_tab:
        render_detail_tab(data["name"], data)
    with chat_tab:
        render_chat_tab(data["name"], data)
    with report_tab:
        render_report_tab(data["name"], data)


_poll_background_job()

reports = st.session_state["reports"]
job = load_job()
scan_running = _job_is_running(job)
if job.get("status") == "completed":
    notice = job.get("message")
    if notice and st.session_state.get("_last_completed_job_message") != notice:
        st.session_state["_last_completed_job_message"] = notice
        st.session_state["extract_notice"] = notice
elif job.get("status") == "failed" and job.get("message"):
    st.error(job.get("message"))

render_resume_banner(scan_running)
inline_status_box = st.container()

if not reports and not scan_running and not st.session_state.get("_inline_auto_continue"):
    st.header("Upload reports")
    render_upload_section()
elif (scan_running or st.session_state.get("_inline_auto_continue")) and not reports:
    st.header("Scan in progress")
    render_upload_section()
else:
    ready_count = sum(1 for report in reports if report_ready(report))
    st.caption(f"{ready_count} of {len(reports)} reports have extracted KPIs.")
    with st.expander("Add or extract more reports", expanded=False):
        render_upload_section()
        st.caption("Already-extracted reports are preserved automatically. Use Clear reports to remove everything.")

    all_tab, compare_tab, company_tab = st.tabs(["All companies", "Compare", "One company"])
    with all_tab:
        render_results_overview(reports)
    with compare_tab:
        render_compare_tab()
    with company_tab:
        render_company_tab()

if is_streamlit_cloud() and st.session_state.get("_inline_auto_continue"):
    _run_next_inline_step(inline_status_box)
