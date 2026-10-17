"""
ESG Report Extractor — Streamlit Demo
---------------------------------------
Upload a company sustainability/ESG/BRSR report PDF and get:
  - Detail tab: metrics grouped by BRSR principle + an AI summary
  - Chat tab: ask follow-up questions, grounded in the extracted data,
    escalating to the real report text only when it needs to
  - Compare tab: line two (or more) companies up side by side

Run locally with:  streamlit run app.py
"""

import json
import os
import io
import tempfile

import pandas as pd
import streamlit as st

try:
    import plotly.express as px
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

from extractor import (
    extract_text_from_pdf,
    extract_esg_metrics,
    brsr_coverage_report,
    identify_red_flags,
    find_brsr_start_page,
    answer_chat_question,
    generate_summary,
    BRSR_PRINCIPLES,
)

st.set_page_config(page_title="ESG Report Extractor", layout="wide")

if not os.environ.get("GEMINI_API_KEY"):
    st.error(
        "⚠️ GEMINI_API_KEY is not set in this environment. Extraction and "
        "chat will fail until you set it. In your terminal, before running "
        "Streamlit:\n\n"
        "  macOS/Linux: `export GEMINI_API_KEY=your_key_here`\n\n"
        "  Windows (PowerShell): `$env:GEMINI_API_KEY='your_key_here'`\n\n"
        "Then restart `streamlit run app.py`. Get a key at "
        "https://aistudio.google.com/apikey"
    )

st.title("📊 AI-Powered ESG Extraction & Comparative Benchmarking Platform")
st.caption(
    "Upload a sustainability / ESG / BRSR report PDF. The tool reads it, "
    "pulls out every quantifiable ESG metric, maps it to GRI codes and "
    "BRSR Core principles, builds a topic index for fast follow-up "
    "questions, and lets you compare companies side by side."
)

st.session_state.setdefault("slot_a", None)  # {"name", "metrics", "topic_index", "page_map", "full_text", "chat_history"}
st.session_state.setdefault("slot_b", None)


# ---------------------------------------------------------------------
# Shared helper: runs the full upload -> detect -> extract pipeline for
# one PDF, storing the result into a fixed slot ("slot_a" or "slot_b")
# rather than an open-ended list — Tab 1 always owns slot_a, Tab 2 always
# owns slot_b, and Compare just reads both directly. No separate upload
# path in Compare anymore, since that was the confusing duplicate.
# ---------------------------------------------------------------------
def run_extraction_ui(key_prefix: str, slot_key: str):
    company_name = st.text_input("Company name", value="", key=f"{key_prefix}_company")

    scan_whole_doc = st.checkbox(
        "Scan the entire document instead of auto-detecting a section",
        value=False,
        key=f"{key_prefix}_scan_whole",
        help=(
            "More reliable when heading detection can't find the right "
            "section (different wording, scanned pages, GRI-only reports) "
            "— just slower and uses more API calls on long reports."
        ),
    )
    start_page = st.number_input(
        "Start page (skip to BRSR/ESG section)",
        min_value=1,
        value=st.session_state.get(f"{key_prefix}_detected_start_page", 1),
        step=1,
        key=f"{key_prefix}_start_page_input",
        disabled=scan_whole_doc,
    )
    max_pages = st.slider(
        "Pages to process from start page", 5, 400, 150,
        key=f"{key_prefix}_max_pages", disabled=scan_whole_doc,
    )

    uploaded_file = st.file_uploader(
        "Upload ESG/Sustainability/BRSR report (PDF)", type=["pdf"], key=f"{key_prefix}_uploader"
    )

    if not uploaded_file:
        return

    detect_col, extract_col = st.columns([1, 1])

    with detect_col:
        if st.button("🔍 Auto-Find BRSR Section", key=f"{key_prefix}_autofind", disabled=scan_whole_doc):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(uploaded_file.getvalue())
                tmp_path = tmp.name

            progress_bar = st.progress(0, text="Scanning report for the BRSR section...")

            def _update_progress(current, total):
                progress_bar.progress(
                    current / total,
                    text=f"Scanning report for the BRSR section... (page {current}/{total})",
                )

            try:
                detected = find_brsr_start_page(tmp_path, progress_callback=_update_progress)
            except ValueError as e:
                progress_bar.empty()
                st.error(f"⚠️ {e}")
                detected = None
            else:
                progress_bar.empty()

            if detected:
                st.session_state[f"{key_prefix}_detected_start_page"] = detected
                st.success(f"Found it — BRSR section appears to start around page {detected}.")
                st.rerun()
            else:
                st.warning(
                    "Couldn't confidently detect a BRSR section heading. "
                    "Try setting the start page manually, or check "
                    "'Scan the entire document instead' above."
                )

    with extract_col:
        run_extraction = st.button("Extract Metrics", type="primary", key=f"{key_prefix}_extract_btn")

    if not run_extraction:
        return

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(uploaded_file.getvalue())
        tmp_path = tmp.name

    read_progress = st.progress(0, text="Reading PDF...")

    def _read_progress(current, total):
        read_progress.progress(current / total, text=f"Reading PDF... (page {current}/{total})")

    if scan_whole_doc:
        text = extract_text_from_pdf(tmp_path, max_pages=100_000, start_page=1, progress_callback=_read_progress)
    else:
        text = extract_text_from_pdf(tmp_path, max_pages=max_pages, start_page=start_page, progress_callback=_read_progress)
    read_progress.empty()
    st.info(f"Extracted {len(text):,} characters from the report.")

    extract_progress = st.progress(0, text="Starting AI extraction...")

    def _extract_progress(current, total, status_text):
        extract_progress.progress(current / max(total, 1), text=status_text)

    metrics, debug_log, topic_index, page_map = extract_esg_metrics(
        text, company_name=company_name or "Unknown", progress_callback=_extract_progress
    )
    extract_progress.empty()

    with st.expander("🔧 Extraction debug log (open this if results look wrong)", expanded=not metrics):
        for entry in debug_log:
            status = entry["status"]
            icon = {"ok": "✅", "empty_response": "⚠️", "parse_error": "❌", "api_error": "🛑", "error": "🛑"}.get(status, "•")
            st.write(f"{icon} Chunk {entry['chunk']} — **{status}**: {entry['detail']}")

    if not metrics:
        st.warning(
            "No structured metrics were found. Check the debug log above — "
            "it will tell you whether this is a page-range problem, an "
            "API/key problem, or genuinely nothing numeric in this text."
        )
        return

    final_name = company_name or "Unknown"
    st.session_state[slot_key] = {
        "name": final_name,
        "metrics": metrics,
        "topic_index": topic_index,
        "page_map": page_map,
        "full_text": text,
        "chat_history": [],
    }
    st.toast(f"✅ Extracted {len(metrics)} metrics and indexed {len(topic_index)} topics for '{final_name}'.", icon="✅")
    st.rerun()


# ---------------------------------------------------------------------
# Detail view: grouped-by-principle table + AI summary
# ---------------------------------------------------------------------
def render_detail_tab(name: str, data: dict):
    metrics = data["metrics"]
    df = _safe_metrics_df(metrics)

    st.subheader(f"{name} — {len(df)} metrics extracted")

    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Download CSV", df.to_csv(index=False), file_name=f"{name}_metrics.csv", key=f"dl_csv_{name}")
    with col2:
        st.download_button("Download JSON", json.dumps(metrics, indent=2), file_name=f"{name}_metrics.json", key=f"dl_json_{name}")

    st.subheader("BRSR Core — 9 Principle Coverage")
    coverage = brsr_coverage_report(metrics)
    cov_df = pd.DataFrame([
        {"Principle": num, "Description": info["principle"], "Disclosed?": "✅ Yes" if info["covered"] else "❌ Gap"}
        for num, info in coverage.items()
    ])
    st.dataframe(cov_df, width='stretch', hide_index=True)

    gaps = [c for c in coverage.values() if not c["covered"]]
    if gaps:
        st.warning(f"⚠️ {len(gaps)} of 9 BRSR Core principles have no disclosed metrics — potential compliance gaps.")
    else:
        st.success("✅ All 9 BRSR Core principles have at least one disclosed metric.")

    if "category" in df.columns:
        st.subheader("Metrics by Category")
        cat_counts = df["category"].value_counts()
        if PLOTLY_AVAILABLE:
            # Using Plotly here instead of st.bar_chart on purpose: this
            # chart renders once per company, and both companies' Detail
            # tabs run in the same Streamlit pass (tabs don't actually
            # skip execution for hidden tabs). st.bar_chart in this
            # Streamlit version has no "key" argument to disambiguate two
            # identical-looking calls, which is exactly what caused the
            # earlier plotly_chart crash — Plotly's chart supports "key"
            # here, so it's the safer choice for anything rendered twice.
            fig = px.bar(
                x=cat_counts.index, y=cat_counts.values,
                labels={"x": "", "y": "Count"},
                color=cat_counts.index,
                color_discrete_map={"Environmental": REPORT_PALETTE["environmental"], "Social": REPORT_PALETTE["social"], "Governance": REPORT_PALETTE["governance"]},
            )
            fig.update_layout(showlegend=False, margin=dict(l=10, r=10, t=10, b=10), height=280)
            st.plotly_chart(fig, width='stretch', key=f"detail_bar_{name}")
        else:
            st.bar_chart(cat_counts)

    st.subheader("Metrics grouped by BRSR Principle")
    for num, principle_name in BRSR_PRINCIPLES.items():
        principle_metrics = [m for m in metrics if m.get("brsr_principle") == num]
        if not principle_metrics:
            continue
        with st.expander(f"Principle {num} — {principle_name} ({len(principle_metrics)} metrics)"):
            for m in principle_metrics:
                st.markdown(
                    f"**{m.get('metric_name', '?')}**: {m.get('value', '?')} {m.get('unit', '')} "
                    f"({m.get('year', 'n/a')}) — *{m.get('gri_code') or 'no GRI code'}* — "
                    f"{m.get('source_context', '')}"
                )

    st.subheader("🤖 AI Summary")
    if st.button("Generate summary", key=f"summary_btn_{name}"):
        with st.spinner("Writing summary..."):
            summary_text = generate_summary(company_name=name, metrics=metrics)
        st.session_state[f"detail_summary_{name}"] = summary_text
    if st.session_state.get(f"detail_summary_{name}"):
        st.markdown(st.session_state[f"detail_summary_{name}"])


# ---------------------------------------------------------------------
# Chat tab: tiered Q&A with visible progress + running history
# ---------------------------------------------------------------------
def render_chat_tab(name: str, data: dict):
    st.subheader(f"💬 Ask about {name}")
    st.caption(
        "Answers start from the extracted metrics table (fast). If that's "
        "not enough — e.g. asking about a program or policy, not just a "
        "number — it looks it up in the topic index built at upload time, "
        "and only does a full report scan as a last resort."
    )

    for turn in data["chat_history"]:
        with st.chat_message("user"):
            st.write(turn["question"])
        with st.chat_message("assistant"):
            st.write(turn["answer"])
            st.caption(f"Answered from: {['metrics table', 'topic index lookup', 'full report scan'][turn['tier'] - 1]}")

    question = st.chat_input(f"Ask something about {name}...", key=f"chat_input_{name}")
    if question:
        with st.chat_message("user"):
            st.write(question)

        progress_bar = st.progress(0, text="Thinking...")

        def _chat_progress(current, total, status_text):
            progress_bar.progress(current / max(total, 1), text=status_text)

        result = answer_chat_question(
            question=question,
            company_name=name,
            metrics=data["metrics"],
            topic_index=data["topic_index"],
            page_map=data["page_map"],
            full_text=data["full_text"],
            chat_history=data["chat_history"],
            progress_callback=_chat_progress,
        )
        progress_bar.empty()

        with st.chat_message("assistant"):
            st.write(result["answer"])
            tier_label = ["metrics table", "topic index lookup", "full report scan"][result["tier"] - 1]
            st.caption(f"Answered from: {tier_label}")
            with st.expander("Debug: how this was answered"):
                for d in result["debug"]:
                    st.write(f"Tier {d['tier']} — {d['status']}: {d['detail']}")

        data["chat_history"].append({"question": question, "answer": result["answer"], "tier": result["tier"]})
        st.rerun()


# ---------------------------------------------------------------------
# Report tab: selectable multi-page report (cover, E, S, G+gaps, AI
# summary, raw data appendix), styled with the muted ESG palette.
# ---------------------------------------------------------------------
REPORT_PALETTE = {
    "environmental": "#5B8C6E",
    "social": "#5B7C99",
    "governance": "#6B5B95",
    "disclosed": "#6B9E78",
    "partial": "#C9A15A",
    "missing": "#B56B6B",
    "bg": "#FAFAF8",
    "text": "#2B2B2B",
    "border": "#E0E0DD",
    "accent": "#2D6A4F",
}


def _safe_metrics_df(metrics: list[dict]) -> pd.DataFrame:
    """
    pd.DataFrame(metrics) directly can crash with a pyarrow ArrowInvalid
    error if the "year" column mixes types — e.g. some metrics have a plain
    int year (2024) and others have a fiscal-year string ("2025-26"), which
    is normal for Indian BRSR reports. extractor.py now normalizes year to
    a string at extraction time, but this is a defensive second layer for
    any already-loaded session data from before that fix, or any other
    insertion path — always coerce year to string right before display.
    """
    df = pd.DataFrame(metrics)
    if "year" in df.columns:
        df["year"] = df["year"].astype(str).replace("None", "")
    return df


def _top_numeric_metrics(metrics, category, n=10):
    subset = [
        m for m in metrics
        if m.get("category") == category and isinstance(m.get("value"), (int, float))
    ]
    return sorted(subset, key=lambda m: m["value"], reverse=True)[:n]


def _chart_explanation(top_metrics, category):
    """A short, deterministic (non-AI) one-liner under each chart — instant, free, no API call."""
    if not top_metrics:
        return f"No numeric {category.lower()} metrics were available to chart."
    best = top_metrics[0]
    year = f" ({best['year']})" if best.get("year") else ""
    return (
        f"The largest disclosed {category.lower()} figure is **{best.get('metric_name')}** "
        f"at **{best.get('value')} {best.get('unit', '')}**{year}. "
        f"{len(top_metrics)} numeric {category.lower()} metrics are charted here."
    )


def _plotly_category_chart(metrics, category, key_suffix=""):
    """Interactive on-screen bar chart. Returns the top metrics used, for the explanation line."""
    top = _top_numeric_metrics(metrics, category, n=10)
    if not top or not PLOTLY_AVAILABLE:
        return top
    df = _safe_metrics_df(top).sort_values("value")
    fig = px.bar(
        df, x="value", y="metric_name", orientation="h",
        color_discrete_sequence=[REPORT_PALETTE[category.lower()]],
        labels={"value": "Value", "metric_name": ""}, text="unit",
    )
    fig.update_layout(
        plot_bgcolor=REPORT_PALETTE["bg"], paper_bgcolor=REPORT_PALETTE["bg"],
        font_color=REPORT_PALETTE["text"], margin=dict(l=10, r=10, t=10, b=10),
        height=max(280, 32 * len(df)),
        xaxis=dict(gridcolor=REPORT_PALETTE["border"]),
    )
    st.plotly_chart(fig, width='stretch', key=f"chart_cat_{category}_{key_suffix}")
    return top


def _plotly_cover_donut(cat_counts, key_suffix=""):
    if not PLOTLY_AVAILABLE:
        return
    cats = ["Environmental", "Social", "Governance"]
    vals = [int(cat_counts.get(c, 0)) for c in cats]
    fig = go.Figure(data=[go.Pie(
        labels=cats, values=vals, hole=0.55,
        marker=dict(colors=[REPORT_PALETTE[c.lower()] for c in cats]),
    )])
    fig.update_layout(
        paper_bgcolor=REPORT_PALETTE["bg"], font_color=REPORT_PALETTE["text"],
        margin=dict(l=10, r=10, t=10, b=10), height=300,
        annotations=[dict(text="E / S / G", x=0.5, y=0.5, showarrow=False)],
    )
    st.plotly_chart(fig, width='stretch', key=f"chart_donut_{key_suffix}")


def _plotly_gap_chart(coverage, key_suffix=""):
    if not PLOTLY_AVAILABLE:
        return
    disclosed = sum(1 for c in coverage.values() if c["covered"])
    missing = 9 - disclosed
    fig = go.Figure(go.Bar(
        x=["Disclosed", "Gap"], y=[disclosed, missing],
        marker_color=[REPORT_PALETTE["disclosed"], REPORT_PALETTE["missing"]],
    ))
    fig.update_layout(
        plot_bgcolor=REPORT_PALETTE["bg"], paper_bgcolor=REPORT_PALETTE["bg"],
        font_color=REPORT_PALETTE["text"], margin=dict(l=10, r=10, t=10, b=10), height=280,
    )
    st.plotly_chart(fig, width='stretch', key=f"chart_gap_{key_suffix}")


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
    metrics = data["metrics"]
    df = _safe_metrics_df(metrics)
    coverage = brsr_coverage_report(metrics)
    covered_count = sum(1 for c in coverage.values() if c["covered"])
    cat_counts = df["category"].value_counts() if "category" in df.columns else pd.Series(dtype=int)

    st.markdown(
        f"""
        <div style="background:{p['bg']}; border:1px solid {p['border']}; border-radius:8px;
                    padding:32px; margin-bottom:20px;">
            <div style="font-size:14px; letter-spacing:0.08em; text-transform:uppercase;
                        color:{p['accent']}; opacity:0.85;">ESG / BRSR Report Snapshot</div>
            <div style="font-size:34px; font-weight:500; color:{p['text']}; margin-top:6px;">{name}</div>
            <div style="font-size:14px; color:{p['text']}; opacity:0.6; margin-top:4px;">
                {len(df)} metrics extracted &nbsp;•&nbsp; {covered_count}/9 BRSR Core principles disclosed
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        _report_card("Environmental", str(int(cat_counts.get("Environmental", 0))), p["environmental"], "metrics")
    with c2:
        _report_card("Social", str(int(cat_counts.get("Social", 0))), p["social"], "metrics")
    with c3:
        _report_card("Governance", str(int(cat_counts.get("Governance", 0))), p["governance"], "metrics")
    with c4:
        _report_card("BRSR Coverage", f"{covered_count}/9", p["accent"], "principles disclosed")

    if PLOTLY_AVAILABLE:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        _plotly_cover_donut(cat_counts, key_suffix=name)
        st.caption(
            f"{len(df)} total metrics span the three ESG pillars — Environmental, Social, and "
            f"Governance — with {covered_count} of 9 BRSR Core principles disclosed."
        )


def _report_page_category(name: str, data: dict, category: str, color: str):
    p = REPORT_PALETTE
    metrics = [m for m in data["metrics"] if m.get("category") == category]

    st.markdown(
        f"""<div style="border-left:4px solid {color}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">{category}</span>
                <span style="font-size:14px; color:{p['text']}; opacity:0.6;"> — {len(metrics)} metrics</span>
            </div>""",
        unsafe_allow_html=True,
    )

    if not metrics:
        st.info(f"No {category.lower()} metrics were extracted for {name}.")
        return

    top = _plotly_category_chart(metrics, category, key_suffix=name)
    if top:
        st.caption(_chart_explanation(top, category))

    principles_here = sorted({m.get("brsr_principle") for m in metrics if m.get("brsr_principle")})
    for num in principles_here:
        principle_name = BRSR_PRINCIPLES.get(num, f"Principle {num}")
        group = [m for m in metrics if m.get("brsr_principle") == num]
        with st.expander(f"Principle {num} — {principle_name} ({len(group)} metrics)", expanded=False):
            for m in group:
                st.markdown(
                    f"**{m.get('metric_name', '?')}**: {m.get('value', '?')} {m.get('unit', '')} "
                    f"({m.get('year', 'n/a')}) — *{m.get('gri_code') or 'no GRI code'}*"
                )

    uncategorized = [m for m in metrics if not m.get("brsr_principle")]
    if uncategorized:
        with st.expander(f"Not mapped to a principle ({len(uncategorized)} metrics)", expanded=False):
            for m in uncategorized:
                st.markdown(f"**{m.get('metric_name', '?')}**: {m.get('value', '?')} {m.get('unit', '')}")


def _report_page_governance_gaps(name: str, data: dict):
    p = REPORT_PALETTE
    metrics = data["metrics"]
    coverage = brsr_coverage_report(metrics)

    st.markdown(
        f"""<div style="border-left:4px solid {p['governance']}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">Governance & BRSR Coverage Gaps</span>
            </div>""",
        unsafe_allow_html=True,
    )

    gov_metrics = [m for m in metrics if m.get("category") == "Governance"]
    st.caption(f"{len(gov_metrics)} governance metrics extracted.")
    for m in gov_metrics:
        st.markdown(
            f"**{m.get('metric_name', '?')}**: {m.get('value', '?')} {m.get('unit', '')} ({m.get('year', 'n/a')})"
        )

    if PLOTLY_AVAILABLE:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        _plotly_gap_chart(coverage, key_suffix=name)
        disclosed_n = sum(1 for c in coverage.values() if c["covered"])
        st.caption(f"{disclosed_n} of 9 BRSR Core principles have at least one disclosed metric; {9 - disclosed_n} show no disclosure at all.")

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.markdown(
        f"""<div style="font-size:16px; font-weight:500; color:{p['text']}; margin-bottom:8px;">
                BRSR Core — 9 Principle Coverage
            </div>""",
        unsafe_allow_html=True,
    )
    for num, info in coverage.items():
        status_color = p["disclosed"] if info["covered"] else p["missing"]
        status_label = "✅ Disclosed" if info["covered"] else "❌ Gap"
        st.markdown(
            f"""
            <div style="display:flex; justify-content:space-between; align-items:center;
                        border:1px solid {p['border']}; border-radius:6px; padding:10px 16px; margin-bottom:6px;">
                <span style="color:{p['text']};">Principle {num} — {info['principle']}</span>
                <span style="color:{status_color}; font-weight:500;">{status_label}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    gaps = [c for c in coverage.values() if not c["covered"]]
    if gaps:
        st.warning(f"⚠️ {len(gaps)} of 9 BRSR Core principles have no disclosed metrics.")
    else:
        st.success("✅ All 9 BRSR Core principles have at least one disclosed metric.")

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.markdown(
        f"""<div style="font-size:16px; font-weight:500; color:{p['text']}; margin-bottom:4px;">
                🚩 Red Flags — commonly-mandatory disclosures found missing
            </div>""",
        unsafe_allow_html=True,
    )
    st.caption(
        "A short, deliberately conservative checklist (not the full official Essential-indicator "
        "list — that's a separate, dedicated project) — flags a small set of disclosures almost "
        "every listed company is expected to report, checked against both the extracted metrics "
        "AND the topic index, so something only discussed in prose still counts as present."
    )
    red_flags = identify_red_flags(metrics, data["topic_index"])
    if red_flags:
        for flag in red_flags:
            st.markdown(
                f"""<div style="border:1px solid {p['missing']}; background:#fdf2f2; border-radius:6px;
                            padding:10px 16px; margin-bottom:6px;">
                        <span style="color:{p['missing']}; font-weight:600;">Missing:</span>
                        <span style="color:{p['text']};"> {_html_escape(flag['indicator'])} (Principle {flag['principle']} — {_html_escape(flag['principle_name'])})</span>
                    </div>""",
                unsafe_allow_html=True,
            )
    else:
        st.success("✅ No red flags — every commonly-mandatory indicator on this checklist was found somewhere in the report.")


def _report_page_ai_summary(name: str, data: dict):
    p = REPORT_PALETTE
    st.markdown(
        f"""<div style="border-left:4px solid {p['accent']}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">AI Executive Summary</span>
            </div>""",
        unsafe_allow_html=True,
    )
    cache_key = f"report_summary_{name}"
    if st.button("Generate summary", key=f"report_summary_btn_{name}"):
        with st.spinner("Writing summary..."):
            st.session_state[cache_key] = generate_summary(company_name=name, metrics=data["metrics"])

    if st.session_state.get(cache_key):
        st.markdown(st.session_state[cache_key])
    else:
        st.caption("Click \"Generate summary\" to produce an AI-written executive summary for this report page.")


def _report_page_appendix(name: str, data: dict):
    p = REPORT_PALETTE
    metrics = data["metrics"]
    df = _safe_metrics_df(metrics)
    st.markdown(
        f"""<div style="border-left:4px solid {p['text']}; padding-left:14px; margin-bottom:16px;">
                <span style="font-size:22px; font-weight:500; color:{p['text']};">Raw Data Appendix</span>
            </div>""",
        unsafe_allow_html=True,
    )
    st.dataframe(df, width='stretch', hide_index=True)
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Download CSV", df.to_csv(index=False), file_name=f"{name}_report.csv", key=f"report_dl_csv_{name}")
    with col2:
        st.download_button("Download JSON", json.dumps(metrics, indent=2), file_name=f"{name}_report.json", key=f"report_dl_json_{name}")


def _safe_pdf_text(text) -> str:
    """fpdf2's built-in fonts only support Latin-1; swap anything else out
    rather than crash the whole PDF build over one unusual character."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _mpl_bar_chart(top_metrics, category):
    if not MATPLOTLIB_AVAILABLE or not top_metrics:
        return None
    ordered = list(reversed(top_metrics))
    names = [str(m.get("metric_name", "?"))[:40] for m in ordered]
    values = [m["value"] for m in ordered]
    fig, ax = plt.subplots(figsize=(6.2, max(2, 0.4 * len(names))))
    ax.barh(names, values, color=REPORT_PALETTE[category.lower()])
    fig.patch.set_facecolor(REPORT_PALETTE["bg"])
    ax.set_facecolor(REPORT_PALETTE["bg"])
    ax.tick_params(colors=REPORT_PALETTE["text"], labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(REPORT_PALETTE["border"])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _mpl_donut(cat_counts):
    if not MATPLOTLIB_AVAILABLE:
        return None
    cats = ["Environmental", "Social", "Governance"]
    vals = [int(cat_counts.get(c, 0)) for c in cats]
    fig, ax = plt.subplots(figsize=(4, 4))
    ax.pie(
        vals, labels=cats, colors=[REPORT_PALETTE[c.lower()] for c in cats],
        wedgeprops=dict(width=0.45), textprops={"color": REPORT_PALETTE["text"]},
    )
    fig.patch.set_facecolor(REPORT_PALETTE["bg"])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _mpl_gap_bar(coverage):
    if not MATPLOTLIB_AVAILABLE:
        return None
    disclosed = sum(1 for c in coverage.values() if c["covered"])
    missing = 9 - disclosed
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.bar(["Disclosed", "Gap"], [disclosed, missing], color=[REPORT_PALETTE["disclosed"], REPORT_PALETTE["missing"]])
    fig.patch.set_facecolor(REPORT_PALETTE["bg"])
    ax.set_facecolor(REPORT_PALETTE["bg"])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def build_report_pdf(name: str, data: dict, selected_pages: list) -> bytes | None:
    """Assembles the selected report sections into a downloadable PDF —
    same charts and colors as the on-screen version, rendered statically
    via matplotlib since the PDF can't be interactive."""
    if not FPDF_AVAILABLE:
        return None

    metrics = data["metrics"]
    df = _safe_metrics_df(metrics)
    coverage = brsr_coverage_report(metrics)
    covered_count = sum(1 for c in coverage.values() if c["covered"])
    cat_counts = df["category"].value_counts() if "category" in df.columns else pd.Series(dtype=int)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    tmpdir = tempfile.mkdtemp()

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
        pdf.cell(0, 12, _safe_pdf_text(f"{name} — ESG / BRSR Report"), ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.cell(0, 8, _safe_pdf_text(f"{len(df)} metrics extracted | {covered_count}/9 BRSR Core principles disclosed"), ln=True)
        pdf.ln(4)
        add_image(_mpl_donut(cat_counts), w=100)

    for category in ["Environmental", "Social"]:
        if category not in selected_pages:
            continue
        top = _top_numeric_metrics(metrics, category, n=10)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _safe_pdf_text(category), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _safe_pdf_text(_chart_explanation(top, category)))
        pdf.ln(2)
        add_image(_mpl_bar_chart(top, category))

    if "Governance & Gaps" in selected_pages:
        top = _top_numeric_metrics(metrics, "Governance", n=10)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Governance", ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _safe_pdf_text(_chart_explanation(top, "Governance")))
        pdf.ln(2)
        add_image(_mpl_bar_chart(top, "Governance"))

        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "BRSR Core Gap Analysis", ln=True)
        add_image(_mpl_gap_bar(coverage), w=100)
        pdf.ln(2)
        pdf.set_font("Helvetica", "", 10)
        for num, info in coverage.items():
            status = "Disclosed" if info["covered"] else "GAP — not disclosed"
            pdf.multi_cell(0, 6, _safe_pdf_text(f"Principle {num} — {info['principle']}: {status}"))

    if "AI Summary" in selected_pages:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Executive Summary", ln=True)
        pdf.set_font("Helvetica", "", 10)
        summary = st.session_state.get(f"report_summary_{name}")
        pdf.multi_cell(
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
    "Environmental": lambda nm, d: _report_page_category(nm, d, "Environmental", REPORT_PALETTE["environmental"]),
    "Social": lambda nm, d: _report_page_category(nm, d, "Social", REPORT_PALETTE["social"]),
    "Governance & Gaps": _report_page_governance_gaps,
    "AI Summary": _report_page_ai_summary,
    "Raw Data Appendix": _report_page_appendix,
}


# ---------------------------------------------------------------------
# Styled HTML executive report — a separate download option alongside
# the chart-based PDF above. Real .html file, no new dependencies, no
# PDF-rendering library needed. Open the file in a browser and use
# "Print -> Save as PDF" for an actual PDF, using software already on
# any machine — this avoids installing weasyprint/wkhtmltopdf, which
# are known to be genuinely painful on Windows.
# ---------------------------------------------------------------------
HTML_PALETTE = {
    "forest": "#0d3b2e",
    "sage": "#6b9080",
    "cream": "#f4f6f0",   # corrected from the sample's invalid 7-char hex
    "card": "#ffffff",
    "border": "#e2e8f0",
    "win_bg": "#dcfce7", "win_text": "#15803d",
    "warn_bg": "#fef9c3", "warn_text": "#a16207",
    "bad_bg": "#fee2e2", "bad_text": "#b91c1c",
}

# Common ESG metric-name keywords -> whether a HIGHER or LOWER value is
# better. This is a deliberately simple, transparent lookup — not an AI
# judgment call — so "who wins" is always explainable in an interview,
# not a black box. Metrics that don't match any keyword are marked
# direction-unknown rather than guessed.
_LOWER_IS_BETTER_KEYWORDS = ["emission", "waste", "water consum", "water withdraw", "energy consum", "incident", "complaint", "turnover", "grievance"]
_HIGHER_IS_BETTER_KEYWORDS = ["renewable", "recycl", "diversity", "women", "independent", "training", "wellbeing", "well-being", "coverage", "disclosed"]


def _metric_direction(metric_name: str):
    name_lower = (metric_name or "").lower()
    if any(k in name_lower for k in _LOWER_IS_BETTER_KEYWORDS):
        return "lower"
    if any(k in name_lower for k in _HIGHER_IS_BETTER_KEYWORDS):
        return "higher"
    return None  # direction genuinely unclear for this metric name


def _html_escape(text) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _build_executive_html(name: str, data: dict) -> str:
    """Single-company styled HTML executive report."""
    metrics = data["metrics"]
    coverage = brsr_coverage_report(metrics)
    covered_count = sum(1 for c in coverage.values() if c["covered"])
    p = HTML_PALETTE

    rows_html = ""
    for m in metrics[:60]:  # keep the file a reasonable size
        assurance = m.get("assurance_level")
        assurance_pill = (
            f'<span class="pill pill-win">{_html_escape(assurance)}</span>' if assurance == "Reasonable Assurance"
            else f'<span class="pill pill-warn">{_html_escape(assurance)}</span>' if assurance == "Limited Assurance"
            else f'<span class="pill pill-bad">Unassured</span>' if assurance == "Unassured"
            else "—"
        )
        rows_html += f"""<tr>
            <td><strong>{_html_escape(m.get('metric_name', '?'))}</strong></td>
            <td>{_html_escape(m.get('value', 'Not Disclosed'))} {_html_escape(m.get('unit', ''))}</td>
            <td>{_html_escape(m.get('year', ''))}</td>
            <td>{_html_escape(m.get('gri_code') or '—')}</td>
            <td>{_html_escape(m.get('brsr_principle') or '—')}</td>
            <td>{assurance_pill}</td>
        </tr>"""

    gap_rows = "".join(
        f'<li>Principle {num}: {_html_escape(info["principle"])}</li>'
        for num, info in coverage.items() if not info["covered"]
    ) or "<li>No gaps — all 9 principles have at least one disclosed metric.</li>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html_escape(name)} — ESG Executive Report</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: 'Plus Jakarta Sans', -apple-system, sans-serif; color: {p['forest']}; background: {p['cream']}; font-size: 14px; line-height: 1.5; margin: 0; padding: 24px; }}
.header-card {{ background: linear-gradient(135deg, {p['forest']} 0%, #1e4d3e 60%, {p['sage']} 100%); color: #fff; padding: 24px; border-radius: 10px; margin-bottom: 20px; }}
.header-title {{ font-size: 24px; font-weight: 800; margin: 0 0 4px 0; }}
.header-meta {{ font-size: 12px; color: #e2e8f0; }}
.card {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 18px; margin-bottom: 18px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ background: {p['forest']}; color: #fff; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; font-size: 13px; }}
.pill {{ padding: 2px 10px; border-radius: 12px; font-weight: 700; font-size: 11px; }}
.pill-win {{ background: {p['win_bg']}; color: {p['win_text']}; }}
.pill-warn {{ background: {p['warn_bg']}; color: {p['warn_text']}; }}
.pill-bad {{ background: {p['bad_bg']}; color: {p['bad_text']}; }}
h2 {{ font-size: 16px; margin-top: 0; }}
@media print {{ body {{ background: #fff; }} }}
</style></head>
<body>
<div class="header-card">
  <div style="font-size:11px; text-transform:uppercase; color:#a3e635; font-weight:700;">ESG Executive Report</div>
  <div class="header-title">{_html_escape(name)}</div>
  <div class="header-meta">{len(metrics)} metrics extracted · {covered_count}/9 BRSR Core principles disclosed · Framework: SEBI BRSR / GRI</div>
</div>
<div class="card">
  <h2>BRSR Core Gaps</h2>
  <ul>{gap_rows}</ul>
</div>
<div class="card">
  <h2>Extracted Metrics</h2>
  <table><thead><tr><th>Metric</th><th>Value</th><th>Year</th><th>GRI Code</th><th>BRSR Principle</th><th>Assurance</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
</div>
<p style="font-size:11px; color:#64748b;">Open this file in a browser and use Print → Save as PDF for a PDF copy.</p>
</body></html>"""


def _build_comparison_html(company_a: str, data_a: dict, company_b: str, data_b: dict) -> str:
    """Two-company styled HTML comparison report with delta/winner callouts."""
    p = HTML_PALETTE
    names_a = {m.get("metric_name"): m for m in data_a["metrics"] if m.get("metric_name")}
    names_b = {m.get("metric_name"): m for m in data_b["metrics"] if m.get("metric_name")}
    shared = sorted(set(names_a) & set(names_b))

    rows_html = ""
    for metric_name in shared:
        ma, mb = names_a[metric_name], names_b[metric_name]
        va, vb = ma.get("value"), mb.get("value")
        winner_html, delta_html = "—", "—"
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and vb != 0:
            delta_pct = ((va - vb) / abs(vb)) * 100
            delta_html = f"{delta_pct:+.1f}%"
            direction = _metric_direction(metric_name)
            if direction == "lower":
                winner = company_a if va < vb else company_b if vb < va else None
            elif direction == "higher":
                winner = company_a if va > vb else company_b if vb > va else None
            else:
                winner = None
            winner_html = f'<span class="pill pill-win">{_html_escape(winner)}</span>' if winner else "—"
        rows_html += f"""<tr>
            <td><strong>{_html_escape(metric_name)}</strong></td>
            <td>{_html_escape(va)} {_html_escape(ma.get('unit',''))}</td>
            <td>{_html_escape(vb)} {_html_escape(mb.get('unit',''))}</td>
            <td>{delta_html}</td>
            <td>{winner_html}</td>
        </tr>"""

    if not shared:
        rows_html = '<tr><td colspan="5">No identically-named metrics were extracted for both companies to compare directly.</td></tr>'

    cov_a, cov_b = brsr_coverage_report(data_a["metrics"]), brsr_coverage_report(data_b["metrics"])
    brsr_rows = ""
    for num, info in cov_a.items():
        status_a = "Disclosed" if info["covered"] else "Gap"
        status_b = "Disclosed" if cov_b[num]["covered"] else "Gap"
        brsr_rows += f"<tr><td>P{num}: {_html_escape(info['principle'])}</td><td>{status_a}</td><td>{status_b}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>{_html_escape(company_a)} vs {_html_escape(company_b)} — ESG Comparison</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: 'Plus Jakarta Sans', -apple-system, sans-serif; color: {p['forest']}; background: {p['cream']}; font-size: 14px; line-height: 1.5; margin: 0; padding: 24px; }}
.header-card {{ background: linear-gradient(135deg, {p['forest']} 0%, #1e4d3e 60%, {p['sage']} 100%); color: #fff; padding: 24px; border-radius: 10px; margin-bottom: 20px; }}
.header-title {{ font-size: 22px; font-weight: 800; margin: 0 0 4px 0; }}
.header-meta {{ font-size: 12px; color: #e2e8f0; }}
.card {{ background: {p['card']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 18px; margin-bottom: 18px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ background: {p['forest']}; color: #fff; font-size: 11px; text-transform: uppercase; padding: 8px 10px; text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #f1f5f9; font-size: 13px; }}
.pill {{ padding: 2px 10px; border-radius: 12px; font-weight: 700; font-size: 11px; }}
.pill-win {{ background: {p['win_bg']}; color: {p['win_text']}; }}
h2 {{ font-size: 16px; margin-top: 0; }}
@media print {{ body {{ background: #fff; }} }}
</style></head>
<body>
<div class="header-card">
  <div style="font-size:11px; text-transform:uppercase; color:#a3e635; font-weight:700;">Executive Benchmarking Brief</div>
  <div class="header-title">{_html_escape(company_a)} vs. {_html_escape(company_b)}</div>
  <div class="header-meta">Framework: SEBI BRSR Core / GRI · Winner direction inferred from common ESG conventions (e.g. lower emissions, higher renewable share) — verify manually for unusual metrics.</div>
</div>
<div class="card">
  <h2>Head-to-Head Metrics</h2>
  <table><thead><tr><th>Metric</th><th>{_html_escape(company_a)}</th><th>{_html_escape(company_b)}</th><th>Delta</th><th>Leader</th></tr></thead>
  <tbody>{rows_html}</tbody></table>
</div>
<div class="card">
  <h2>BRSR Core Principle Coverage</h2>
  <table><thead><tr><th>Principle</th><th>{_html_escape(company_a)}</th><th>{_html_escape(company_b)}</th></tr></thead>
  <tbody>{brsr_rows}</tbody></table>
</div>
<p style="font-size:11px; color:#64748b;">Open this file in a browser and use Print → Save as PDF for a PDF copy.</p>
</body></html>"""


def render_report_tab(name: str, data: dict):
    st.caption("Pick any section below — build only the pages you need, or use a quick preset.")

    presets = {
        "Full report": list(REPORT_PAGES.keys()),
        "Environmental only": ["Cover / Snapshot", "Environmental"],
        "Social only": ["Cover / Snapshot", "Social"],
        "Governance & compliance only": ["Cover / Snapshot", "Governance & Gaps"],
    }
    preset_cols = st.columns(len(presets))
    for col, (label, pages) in zip(preset_cols, presets.items()):
        with col:
            if st.button(label, key=f"preset_{label}_{name}", width='stretch'):
                st.session_state[f"report_sections_{name}"] = pages
                st.rerun()

    page_names = list(REPORT_PAGES.keys())
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
                file_name=f"{name}_ESG_Report.pdf",
                mime="application/pdf",
                key=f"dl_pdf_{name}",
            )

    st.markdown("&nbsp;", unsafe_allow_html=True)
    st.download_button(
        "🎨 Download styled HTML report (Forest Pine design)",
        _build_executive_html(name, data),
        file_name=f"{name}_ESG_Executive_Report.html",
        mime="text/html",
        key=f"dl_html_{name}",
        help="No extra installs needed. Open the downloaded file in any browser, then use Print → Save as PDF for a PDF copy.",
    )


# ---------------------------------------------------------------------
# Compare tab: directly compares slot_a vs slot_b — no upload box of its
# own, no dropdown picking. Whatever's in Tab 1 vs whatever's in Tab 2.
# ---------------------------------------------------------------------
def render_compare_tab():
    slot_a, slot_b = st.session_state["slot_a"], st.session_state["slot_b"]

    if not slot_a or not slot_b:
        missing = []
        if not slot_a:
            missing.append("Tab 1")
        if not slot_b:
            missing.append("Tab 2 (Add another report)")
        st.info(f"Upload a report in {' and '.join(missing)} first — Compare shows those two side by side once both are ready.")
        return

    company_a, company_b = slot_a["name"], slot_b["name"]
    st.caption(f"Comparing **{company_a}** (Tab 1) vs **{company_b}** (Tab 2).")

    summary_rows = []
    for nm, d in [(company_a, slot_a), (company_b, slot_b)]:
        cov = brsr_coverage_report(d["metrics"])
        covered_count = sum(1 for c in cov.values() if c["covered"])
        cat_counts = pd.Series([x.get("category") for x in d["metrics"]]).value_counts()
        summary_rows.append({
            "Company": nm,
            "Metrics extracted": len(d["metrics"]),
            "BRSR principles covered": f"{covered_count}/9",
            "Environmental": int(cat_counts.get("Environmental", 0)),
            "Social": int(cat_counts.get("Social", 0)),
            "Governance": int(cat_counts.get("Governance", 0)),
        })
    summary_df = pd.DataFrame(summary_rows)
    st.dataframe(summary_df, width='stretch', hide_index=True)
    st.bar_chart(summary_df.set_index("Company")[["Environmental", "Social", "Governance"]])

    st.subheader("Compare a specific metric")
    all_metric_names = sorted({
        m.get("metric_name") for d in (slot_a, slot_b) for m in d["metrics"] if m.get("metric_name")
    })
    if all_metric_names:
        chosen_metric = st.selectbox("Metric", all_metric_names, key="compare_metric_select")
        rows = []
        for nm, d in [(company_a, slot_a), (company_b, slot_b)]:
            match = next((x for x in d["metrics"] if x.get("metric_name") == chosen_metric), None)
            if match:
                # Value is forced to string here — this column will always
                # mix real numbers with the text "Not extracted" below, and
                # mixed number/text columns crash Streamlit's table
                # renderer (pyarrow) the same way the "year" column did
                # earlier. Same fix, same reason: this is a display column,
                # not something we do math on, so text is fine either way.
                rows.append({"Company": nm, "Value": str(match.get("value")), "Unit": match.get("unit"), "Year": str(match.get("year")) if match.get("year") is not None else ""})
            else:
                # Always show both companies, even when one didn't extract
                # this metric — explicit "Not extracted" instead of just
                # silently omitting the row, which looked like a bug.
                rows.append({"Company": nm, "Value": "Not extracted", "Unit": "—", "Year": "—"})
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    combined = pd.concat([_safe_metrics_df(slot_a["metrics"]), _safe_metrics_df(slot_b["metrics"])], ignore_index=True)
    st.download_button(
        "Download combined comparison CSV",
        combined.to_csv(index=False),
        file_name="esg_comparison.csv",
    )
    st.download_button(
        "🎨 Download styled HTML comparison report (Forest Pine design)",
        _build_comparison_html(company_a, slot_a, company_b, slot_b),
        file_name=f"{company_a}_vs_{company_b}_Comparison.html",
        mime="text/html",
        key="dl_compare_html",
        help="No extra installs needed. Open the downloaded file in any browser, then use Print → Save as PDF for a PDF copy.",
    )

    st.subheader("💬 Ask a comparison question")
    st.caption(f"Grounded in both {company_a}'s and {company_b}'s extracted data.")
    combined_metrics = [dict(m, _company=company_a) for m in slot_a["metrics"]] + [dict(m, _company=company_b) for m in slot_b["metrics"]]
    compare_question = st.chat_input("e.g. Which company has better emissions performance?", key="compare_chat_input")
    if compare_question:
        with st.chat_message("user"):
            st.write(compare_question)
        progress_bar = st.progress(0, text="Thinking...")

        def _cmp_progress(current, total, status_text):
            progress_bar.progress(current / max(total, 1), text=status_text)

        result = answer_chat_question(
            question=compare_question,
            company_name=f"{company_a} vs {company_b}",
            metrics=combined_metrics,
            topic_index=slot_a["topic_index"] + slot_b["topic_index"],
            page_map={**slot_a["page_map"], **slot_b["page_map"]},
            full_text=slot_a["full_text"] + "\n\n" + slot_b["full_text"],
            chat_history=[],
            progress_callback=_cmp_progress,
        )
        progress_bar.empty()
        with st.chat_message("assistant"):
            st.write(result["answer"])


# ---------------------------------------------------------------------
# Report slot: shared rendering for Tab 1 and Tab 2 — upload form until
# a report's extracted, then the Detail / Chat / Report sub-tabs.
# ---------------------------------------------------------------------
def render_report_slot(slot_key: str, key_prefix: str, empty_heading: str):
    data = st.session_state[slot_key]

    if not data:
        st.header(empty_heading)
        run_extraction_ui(key_prefix=key_prefix, slot_key=slot_key)
        return

    col_title, col_reset = st.columns([5, 1])
    with col_title:
        st.subheader(f"📄 {data['name']}")
    with col_reset:
        if st.button("🔄 Replace", key=f"{key_prefix}_reset", help="Upload a different report into this slot"):
            st.session_state[slot_key] = None
            st.rerun()

    detail_tab, chat_tab, report_tab = st.tabs(["Detail", "Chat", "Report"])
    with detail_tab:
        render_detail_tab(data["name"], data)
    with chat_tab:
        render_chat_tab(data["name"], data)
    with report_tab:
        render_report_tab(data["name"], data)


# ---------------------------------------------------------------------
# Main layout — three fixed tabs: Tab 1 (slot A), Tab 2 / Add another
# report (slot B), Compare (reads both slots directly).
# ---------------------------------------------------------------------
tab1_label = f"📄 {st.session_state['slot_a']['name']}" if st.session_state["slot_a"] else "⬆️ Upload Your First Report"
top_tabs = st.tabs([tab1_label, "➕ Add another report", "📈 Compare"])

with top_tabs[0]:
    render_report_slot("slot_a", key_prefix="a", empty_heading="Upload a report to get started")

with top_tabs[1]:
    render_report_slot("slot_b", key_prefix="b", empty_heading="Upload a second report to compare")

with top_tabs[2]:
    render_compare_tab()
