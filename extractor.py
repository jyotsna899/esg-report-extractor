"""
ESG Report Extractor (Gemini version)
---------------------------------------
Takes a company sustainability/ESG/BRSR report (PDF) and uses Gemini to:
  1. Extract structured ESG metrics mapped to GRI codes and BRSR categories
  2. Build a lightweight topic index (topic -> page numbers) as a free
     byproduct of the same extraction pass, so later chat questions can
     jump straight to the right page instead of rescanning the report
  3. Answer follow-up chat questions using a tiered lookup: metrics table
     first (cheap), then the topic index (targeted), then a full report
     scan only as a last resort

Usage:
    from extractor import extract_text_from_pdf, extract_esg_metrics, answer_chat_question

    text = extract_text_from_pdf("company_report.pdf")
    metrics, debug_log, topic_index, page_map = extract_esg_metrics(text, company_name="Acme Corp")
    result = answer_chat_question("Tell me about their rainwater harvesting", "Acme Corp", metrics, topic_index, page_map, text)
"""

import gc
import json
import os
import re
import time
import pdfplumber
from google import genai


def _load_env_file():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_env_file()


# Roughly 1 token ≈ 4 characters. Free-tier Gemini rate limits cap input
# tokens per minute, so we keep each call comfortably under that rather
# than maxing out the full 1M context in one shot.
SINGLE_CALL_CHAR_LIMIT = 150_000
# Gemini 3.6 Flash caps output at 65,536 tokens regardless of input size —
# asking it to extract every metric AND build the full topic index for a
# huge chunk in one response risks hitting that ceiling and getting cut
# off mid-JSON. Smaller chunks keep each response comfortably inside the
# limit. (Previously 400,000 — large enough that a full-document "scan
# whole doc" run could collapse into a single oversized call.)

MODEL_NAME = "gemini-3.5-flash-lite"
MAX_OUTPUT_TOKENS = 65_536
# temperature=0 makes the model always pick its most confident output
# instead of sampling somewhat randomly. It won't guarantee byte-identical
# output on every run, but it removes most of the run-to-run drift that
# was causing the same PDF to show a different BRSR gap count each time.
TEMPERATURE = 0

# The real content of each NGRBC principle underlying BRSR, per SEBI's
# framework — used to ground brsr_principle classification in what each
# principle actually covers, instead of leaving it to guesswork. This is
# principle-level context, not the full 98-indicator Essential checklist
# (that level of granularity is deliberately Project 2's job — the
# dedicated BRSR Compliance Gap Analyzer — to keep these two portfolio
# projects distinct rather than duplicating each other).
BRSR_PRINCIPLE_SYLLABUS = """
P1 (Ethics, Transparency & Accountability): conducting business with integrity, transparency, and accountability — anti-corruption, anti-bribery, whistleblower mechanisms, conflicts of interest, penalties/fines for non-compliance.
P2 (Sustainable & Safe Products/Services): goods and services provided safely and sustainably throughout their lifecycle — sustainable sourcing, product safety, R&D/capex on sustainability, circular design, life-cycle assessments.
P3 (Employee Wellbeing): wellbeing of all employees AND value-chain workers — health & safety (LTIFR, fatalities), wages, benefits, training, retention/attrition, diversity, grievance mechanisms, union representation.
P4 (Stakeholder Engagement): responsiveness to all stakeholders, especially disadvantaged/vulnerable/marginalised groups — stakeholder identification, engagement processes, grievance redressal.
P5 (Human Rights): respect for and promotion of human rights — human rights policy, due diligence, child/forced labor safeguards, supplier human-rights assessments, remediation of violations.
P6 (Environment): protection and restoration of the environment — GHG emissions (Scope 1/2/3), energy consumption and renewable share, water withdrawal/consumption/discharge, waste generation and management, biodiversity impact.
P7 (Public Policy Advocacy): responsible and transparent public policy engagement — trade association memberships, policy advocacy positions, lobbying transparency.
P8 (Inclusive Growth): inclusive growth and equitable development — CSR spend vs. target, sourcing from small/local/marginalized producers, employment/impact generated in aspirational districts or smaller towns.
P9 (Consumer Value): responsible value delivery to consumers — product labeling/information, data privacy and cybersecurity, advertising practices, consumer complaint volumes and resolution, product recalls.
""".strip()


# ---------------------------------------------------------------------
# Retry helper — shared by every Gemini call in this file. Free-tier
# limits are opaque (Google no longer publishes a fixed table; it's
# assigned per project), so instead of guessing a safe number we just
# back off and retry whenever we actually hit a 429.
# ---------------------------------------------------------------------
def _call_gemini_with_retry(client, contents, config, max_retries=4, progress_callback=None):
    # temperature=0 by default on every call in this file, applied once
    # here rather than at each call site — every caller passes its own
    # config dict (response_mime_type, max_output_tokens, etc.), so we
    # merge temperature in without needing to touch every call site.
    config = {"temperature": TEMPERATURE, **config}
    delay = 5
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return client.models.generate_content(model=MODEL_NAME, contents=contents, config=config)
        except Exception as e:
            last_error = e
            msg = str(e)
            retryable = (
                "429" in msg
                or "503" in msg
                or "RESOURCE_EXHAUSTED" in msg
                or "UNAVAILABLE" in msg
                or "rate limit" in msg.lower()
                or "high demand" in msg.lower()
            )
            if retryable and attempt < max_retries:
                if progress_callback:
                    progress_callback(
                        0, 1,
                        f"Model busy — waiting {delay}s before retry ({attempt + 1}/{max_retries})...",
                    )
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise
    raise last_error


# ---------------------------------------------------------------------
# Step 1: Pull text out of the PDF
# ---------------------------------------------------------------------
def _flush_pdf_page(page):
    try:
        page.flush_cache()
    except Exception:
        pass
    try:
        if hasattr(page, "get_textmap") and hasattr(page.get_textmap, "cache_clear"):
            page.get_textmap.cache_clear()
    except Exception:
        pass


def find_brsr_start_page(pdf_path: str, progress_callback=None) -> int | None:
    """
    Scans the PDF for the page where the ESG/sustainability/BRSR section
    actually begins, rather than just any page that mentions it in passing.
    Checks for BRSR-specific headings first (India-specific), then falls
    back to general ESG/GRI/sustainability report headings so this also
    works for reports that don't use the BRSR framework at all.

    Stops as soon as it finds a high-confidence match (an early-page, short,
    BRSR-specific heading) instead of always reading the whole document.

    progress_callback, if given, is called as progress_callback(current_page,
    total_pages) after every page so a caller can show a progress bar.

    Returns the 1-indexed page number where the section appears to start,
    or None if it can't be confidently detected. Raises ValueError if the
    PDF has no extractable text at all (likely scanned/image-only).
    """
    heading_phrases = [
        "business responsibility and sustainability report",
        "business responsibility & sustainability report",
        "brsr core",
        "sustainability report",
        "esg report",
        "environmental, social and governance report",
        "environmental, social, and governance report",
        "gri content index",
        "gri standards",
        "corporate sustainability report",
    ]
    HIGH_CONFIDENCE_RANK = 2

    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        candidates = []
        blank_pages = 0
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            _flush_pdf_page(page)
            if progress_callback:
                progress_callback(i + 1, total_pages)
            if not text:
                blank_pages += 1
                continue
            # Collapse whitespace (including line breaks a wrapped heading
            # gets split across) so multi-line headings still match.
            lower = " ".join(text.lower().split())
            for phrase_rank, phrase in enumerate(heading_phrases):
                if phrase in lower:
                    position = lower.find(phrase)
                    is_early_on_page = position < 300
                    is_short_page = len(text) < 2000
                    candidates.append((i + 1, is_early_on_page, len(text), phrase_rank))
                    if is_early_on_page and is_short_page and phrase_rank <= HIGH_CONFIDENCE_RANK:
                        return i + 1
                    break

        if not candidates:
            if blank_pages == total_pages:
                raise ValueError(
                    f"No extractable text found on any of {total_pages} pages. "
                    "This PDF is likely scanned/image-based rather than "
                    "text-based — it needs OCR before this tool can read it."
                )
            return None

        title_like = sorted(
            [c for c in candidates if c[1] and c[2] < 2000],
            key=lambda c: c[3],
        )
        if title_like:
            return title_like[0][0]

        candidates.sort(key=lambda c: c[3])
        return candidates[0][0]


def extract_text_from_pdf(pdf_path: str, max_pages: int = 200, start_page: int = 1, progress_callback=None) -> str:
    """
    Extract text from a PDF report, starting at start_page (1-indexed) and
    reading up to max_pages pages from there.

    progress_callback, if given, is called as progress_callback(current, total)
    after each page so a caller can render a progress bar.
    """
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        start_idx = max(0, start_page - 1)
        end_idx = start_idx + max_pages
        pages = pdf.pages[start_idx:end_idx]
        total = len(pages)
        for i, page in enumerate(pages):
            text = page.extract_text()
            _flush_pdf_page(page)
            if text:
                full_text.append(f"\n--- Page {start_idx + i + 1} ---\n{text}")
            if progress_callback:
                progress_callback(i + 1, total)
    gc.collect()
    return "\n".join(full_text)


def build_page_map(text: str) -> dict[int, str]:
    """
    Splits page-marked text (as produced by extract_text_from_pdf, which
    inserts "--- Page N ---" markers) into {page_number: page_text}.
    This is what lets chat answers jump straight to a specific page's real
    text instead of rescanning the whole document.
    """
    pages = {}
    matches = list(re.finditer(r"--- Page (\d+) ---\n", text))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        pages[int(m.group(1))] = text[start:end].strip()
    return pages


# ---------------------------------------------------------------------
# Step 2: Chunk the text — needed for reports too large for one call,
# and reused as the last-resort fallback during chat.
# ---------------------------------------------------------------------
def chunk_text(text: str, chunk_size_chars: int = SINGLE_CALL_CHAR_LIMIT) -> list[str]:
    """Character-based chunking, splitting on page breaks where possible."""
    if len(text) <= chunk_size_chars:
        return [text]

    pages = text.split("--- Page")
    chunks, current = [], ""
    for p in pages:
        if len(current) + len(p) > chunk_size_chars and current:
            chunks.append(current)
            current = p
        else:
            current += "--- Page" + p if current else p
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------
# Step 3: Ask the LLM to extract structured ESG metrics + build a topic
# index in the same pass (no extra API cost — same calls, richer output)
# ---------------------------------------------------------------------
EXTRACTION_PROMPT = """You are an ESG data analyst assistant. Read the report text below, which \
includes "--- Page N ---" markers, and extract two things.

GROUNDING RULES (follow these strictly to avoid hallucination):
- Every metric value must be a number that is LITERALLY STATED in the text below. Never invent, \
estimate, round, or infer a value — including "typical" values you might know for similar \
companies from your own training. If it's not written in this text, it doesn't go in "metrics".
- The one deliberate exception is "gri_code" below, where you SHOULD use your own knowledge of \
the GRI Standards framework — that is the only field where outside knowledge is allowed. Every \
other field must come strictly from what's written in the text.

BRSR PRINCIPLE REFERENCE — use this to classify "brsr_principle" accurately, matching each \
metric to what the principle actually covers, not just a guess:
{brsr_syllabus}

1. "metrics": every quantifiable ESG metric you can find. For each, an object with:
   - "metric_name": short descriptive name (e.g. "Scope 1 GHG Emissions")
   - "value": the numeric value (as a number, not a string, if possible)
   - "unit": unit of measurement (e.g. "tCO2e", "%", "GJ", "million liters")
   - "year": reporting year if stated, else null
   - "category": one of ["Environmental", "Social", "Governance"]
   - "gri_code": the matching GRI Standard code, using your own knowledge of the GRI \
Standards framework to infer it from the metric type — not only when the report itself \
cites one. For example: Scope 1 GHG emissions -> "GRI 305-1", water withdrawal -> "GRI \
303-3", employee turnover -> "GRI 401-1", training hours per employee -> "GRI 404-1", \
board independence -> "GRI 405-1". Only use null if the metric genuinely doesn't map to \
any GRI Standard topic — don't leave it null just because the report didn't print the code.
   - "brsr_principle": matching BRSR Principle number (1-9), using the reference above to \
classify based on subject matter — else null if it genuinely doesn't fit any principle.
   - "source_context": the number and its immediate surrounding text AS LITERALLY WRITTEN in \
the source (a short verbatim snippet, not a paraphrase), plus the page number. This must be \
checkable against the source text — don't summarize or reword it.
   - "assurance_level": if the report states this metric (or its section) was third-party \
verified, one of "Reasonable Assurance", "Limited Assurance", or "Unassured" — else null. \
Don't guess; only set this when the report explicitly discusses assurance/verification.
   - "assurance_provider": the named auditor/verifier if stated (e.g. "KPMG", "DNV"), else null
   - "indicator_type": your best judgment of "Essential" (baseline, typically-mandatory \
disclosure — e.g. Scope 1/2 emissions, total workforce, safety incidents, board independence) \
or "Leadership" (a more advanced/voluntary-tier disclosure — e.g. value-chain assessments, \
life-cycle analysis, biodiversity impact studies). This is a judgment call, not an exact \
lookup against the official ~98-item checklist — treat it as directional, not authoritative.

   Only extract metrics with an actual stated numeric value. Do not invent or estimate values.

2. "topics": a navigation index of every distinct subject discussed in this text, INCLUDING \
subjects that were only discussed in prose with no number attached (e.g. "Rainwater harvesting", \
"Employee grievance redressal process", "Board diversity policy", "Supply chain audits"). Be \
generous here — it's much better to list a topic than to miss it, since this index is how a \
reader will later jump straight to detailed prose about something that never became a metric. \
For each: {{"topic": short phrase (3-6 words), "pages": [page numbers, from the "--- Page N ---" \
markers, where this is discussed]}}.

Return ONLY a valid JSON object with exactly these two keys: "metrics" and "topics". No \
preamble, no markdown fences, no explanation text. If nothing is found for a key, return [] for it.

REPORT TEXT:
{chunk}
"""


def extract_esg_metrics(
    text: str,
    company_name: str = "Unknown",
    progress_callback=None,
) -> tuple[list[dict], list[dict], list[dict], dict[int, str]]:
    """
    Runs the extraction prompt over the report text, chunk by chunk, with a
    short delay between calls to stay under rate limits.

    progress_callback, if given, is called as
    progress_callback(current_chunk, total_chunks, status_text).

    Returns (metrics, debug_log, topic_index, page_map):
      - metrics: list of extracted metric dicts
      - debug_log: one entry per chunk describing what happened, so a
        caller can surface real failures instead of a silent "0 results"
      - topic_index: list of {"topic": ..., "pages": [...], "chunk": N},
        built for free as part of the same calls — this is what powers
        fast, targeted chat lookups later without rescanning the report
      - page_map: {page_number: page_text}, built locally (no API cost)
        from the original text, so chat can pull real page content for
        whatever the topic index points it at
    """
    page_map = build_page_map(text)

    if not text.strip():
        return [], [{"chunk": 0, "status": "error", "detail": "No text was extracted from the PDF for this page range — nothing was sent to the model."}], [], page_map

    client = genai.Client()  # picks up GEMINI_API_KEY from env automatically
    chunks = chunk_text(text)
    total = len(chunks)
    all_metrics = []
    topic_index = []
    debug_log = []

    for idx, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(idx + 1, total, f"Sending chunk {idx + 1}/{total} to Gemini...")

        if idx > 0:
            if progress_callback:
                progress_callback(idx + 1, total, f"Waiting to stay under rate limits (chunk {idx + 1}/{total})...")
            time.sleep(2)

        try:
            response = _call_gemini_with_retry(
                client,
                EXTRACTION_PROMPT.format(chunk=chunk, brsr_syllabus=BRSR_PRINCIPLE_SYLLABUS),
                {"response_mime_type": "application/json", "max_output_tokens": MAX_OUTPUT_TOKENS},
                progress_callback=progress_callback,
            )
            # Reading .text (and finish_reason) can itself raise — e.g. when
            # a response gets cut off partway through a very long request.
            # This used to sit outside any try/except, so a truncated
            # response could crash the whole extraction run silently
            # instead of being logged as a recoverable per-chunk failure.
            raw = (response.text or "").strip()
        except Exception as e:
            finish_reason = None
            try:
                finish_reason = response.candidates[0].finish_reason
            except Exception:
                pass
            debug_log.append({
                "chunk": idx + 1, "status": "api_error",
                "detail": f"{type(e).__name__}: {e} (finish_reason={finish_reason})",
            })
            continue

        if not raw:
            finish_reason = None
            try:
                finish_reason = response.candidates[0].finish_reason
            except Exception:
                pass
            debug_log.append({
                "chunk": idx + 1,
                "status": "empty_response",
                "detail": f"Model returned no text. finish_reason={finish_reason}",
            })
            continue

        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(f"Expected a JSON object with 'metrics'/'topics' keys, got {type(parsed).__name__}")

            chunk_metrics = parsed.get("metrics", []) or []
            chunk_topics = parsed.get("topics", []) or []

            for m in chunk_metrics:
                m["company"] = company_name
                m["source_chunk"] = idx + 1
                # Indian BRSR reports commonly state figures as fiscal years
                # like "2025-26" rather than a plain number, while other
                # metrics in the SAME table get a plain int year like 2024.
                # A mixed int/string column crashes pyarrow (the library
                # Streamlit's dataframe display relies on) with an
                # ArrowInvalid error the instant it tries to render. Forcing
                # every year to a string keeps the column type consistent —
                # year is only ever displayed, never used for arithmetic.
                if m.get("year") is not None:
                    m["year"] = str(m["year"])
            all_metrics.extend(chunk_metrics)

            for t in chunk_topics:
                t["chunk"] = idx + 1
            topic_index.extend(chunk_topics)

            debug_log.append({
                "chunk": idx + 1,
                "status": "ok",
                "detail": f"Parsed {len(chunk_metrics)} metrics, {len(chunk_topics)} topics.",
            })
        except (json.JSONDecodeError, ValueError) as e:
            finish_reason = None
            try:
                finish_reason = response.candidates[0].finish_reason
            except Exception:
                pass
            truncation_note = (
                " — this looks like the response got CUT OFF by Gemini's output limit "
                "(65,536 tokens per call) rather than a genuine parsing bug. Try a smaller "
                "page range per extraction."
                if str(finish_reason) in ("2", "MAX_TOKENS", "FinishReason.MAX_TOKENS") else ""
            )
            debug_log.append({
                "chunk": idx + 1,
                "status": "parse_error",
                "detail": f"{e}. finish_reason={finish_reason}{truncation_note} Raw response (first 500 chars): {raw[:500]!r}",
            })

    gc.collect()
    return all_metrics, debug_log, topic_index, page_map


# ---------------------------------------------------------------------
# Step 4: Simple gap check against BRSR Core's 9 principles
# ---------------------------------------------------------------------
BRSR_PRINCIPLES = {
    1: "Ethics, Transparency & Accountability",
    2: "Sustainable & Safe Products/Services",
    3: "Employee Wellbeing",
    4: "Stakeholder Responsiveness",
    5: "Human Rights",
    6: "Environment Protection",
    7: "Public Policy Advocacy",
    8: "Inclusive Growth",
    9: "Consumer Value & Responsibility",
}


def brsr_coverage_report(metrics: list[dict]) -> dict:
    """Returns which of the 9 BRSR principles have at least one disclosed metric."""
    covered = {m.get("brsr_principle") for m in metrics if m.get("brsr_principle")}
    return {
        num: {"principle": name, "covered": num in covered}
        for num, name in BRSR_PRINCIPLES.items()
    }


# Commonly-mandatory disclosures whose complete ABSENCE (not just a low
# principle score) is worth calling out specifically — these are checked
# deterministically in Python (keyword match against extracted metric
# names AND the topic index), no extra API call. This deliberately isn't
# the full official Essential-indicator checklist (that's Project 2's
# job) — it's a short, defensible "commonly expected" list that makes an
# obvious, glaring omission visible rather than burying it inside a
# generic "gap" count.
RED_FLAG_INDICATORS = [
    (1, ["whistleblower"], "Whistleblower mechanism"),
    (1, ["anti-corruption", "anti corruption", "bribery"], "Anti-corruption / anti-bribery policy"),
    (3, ["lost time injury", "ltifr", "fatalit"], "Workplace safety incident rate (LTIFR/fatalities)"),
    (3, ["contractual worker", "value chain worker", "contract worker"], "Contractual/value-chain worker safety data"),
    (5, ["human rights due diligence", "child labor", "child labour", "forced labor", "forced labour"], "Human rights due diligence / child & forced labor safeguards"),
    (6, ["scope 3"], "Scope 3 GHG emissions"),
    (6, ["water discharge", "water withdrawal", "water consumption"], "Water withdrawal/discharge disclosure"),
    (9, ["data privacy", "cybersecurity", "data breach"], "Data privacy / cybersecurity incident disclosure"),
    (9, ["product recall"], "Product recall disclosure"),
]


def identify_red_flags(metrics: list[dict], topic_index: list[dict]) -> list[dict]:
    """
    Checks each commonly-mandatory indicator above against both the
    extracted metric names and the topic index (so something discussed
    only in prose, with no number attached, still counts as present).
    Returns entries only for what's genuinely missing.
    """
    haystack = " ".join(
        [str(m.get("metric_name", "")).lower() for m in metrics]
        + [str(t.get("topic", "")).lower() for t in topic_index]
    )
    flags = []
    for principle_num, keywords, label in RED_FLAG_INDICATORS:
        if not any(kw in haystack for kw in keywords):
            flags.append({"principle": principle_num, "principle_name": BRSR_PRINCIPLES[principle_num], "indicator": label})
    return flags


# ---------------------------------------------------------------------
# Step 5: Tiered chat Q&A
#   Tier 1 - answer from the metrics table alone (cheap, fast)
#   Tier 2 - if insufficient, look up the pre-built topic index and
#            answer from those specific real pages (targeted, no rescan)
#   Tier 3 - last resort: chunk-scan the full report (rare; only when
#            the topic index genuinely missed something)
# ---------------------------------------------------------------------
CHAT_TIER1_PROMPT = """You are an ESG analyst assistant helping a user work with a company's \
extracted ESG/BRSR/GRI metrics. Use the metrics table below (JSON) and the conversation history \
to respond to the user's latest message.

Two kinds of requests both count as fully answerable from the metrics table alone — mark them \
"sufficient": true and give a complete response, not a thin one:
1. Factual questions about a number, unit, year, GRI code, or BRSR principle that's actually in \
   the table.
2. Any request to reformulate, summarize, analyze, or creatively present the metrics that are \
   already in the table — e.g. "write an executive summary," "turn the governance metrics into \
   a short story for investors," "make this sound more compelling." These don't need any new \
   facts beyond what's already extracted, so they are always sufficient — write the full \
   requested piece (summary, story, pitch, whatever was asked), don't just describe what you \
   could write.

Only mark "sufficient": false when the user is asking about something that genuinely isn't in \
the metrics table — e.g. asking about a program, policy, or explanation not captured as a bare \
number (like "how does their rainwater harvesting work"). In that case, don't guess or pad a \
thin answer; say so and suggest search terms instead.

Return ONLY a valid JSON object with exactly these keys:
- "answer": your full response in plain text
- "sufficient": true if the metrics table gave you enough (see both cases above), false if you need the fuller report text
- "search_terms": if sufficient is false, 2-5 short keywords/phrases to search a topic index for (e.g. ["rainwater harvesting", "water conservation"])

COMPANY: {company_name}

METRICS TABLE (JSON):
{metrics_json}

CONVERSATION SO FAR:
{history_text}

LATEST QUESTION: {question}
"""

CHAT_TIER2_PROMPT = """You are an ESG analyst assistant. Answer the user's question using the \
actual report excerpts below. Be specific and detailed — surface relevant facts, figures, and \
program details in your own words. If the excerpts don't actually contain the answer, say so \
honestly instead of guessing.

COMPANY: {company_name}

REPORT EXCERPTS:
{excerpts}

CONVERSATION SO FAR:
{history_text}

LATEST QUESTION: {question}

Return ONLY a valid JSON object with exactly these keys: "answer" (string) and "found" (true/false, \
whether these excerpts actually answered the question).
"""


SUMMARY_PROMPT = """You are an ESG analyst assistant. Write a 3-4 paragraph executive summary \
of {company_name}'s ESG performance, based entirely on the metrics table below. Cover: \
notable strengths and standout figures, any concerning numbers, and gaps — specifically call \
out any of the 9 BRSR Core principles with no disclosed metrics. Write it as plain prose, no \
JSON, no markdown headers, just the summary text itself.

METRICS TABLE (JSON):
{metrics_json}

BRSR COVERAGE (principle -> disclosed?):
{coverage_json}
"""


def generate_summary(company_name: str, metrics: list[dict]) -> str:
    """
    Writes an executive summary directly from the metrics table in a single
    call. Deliberately NOT routed through answer_chat_question's tiered
    Q&A pipeline — that pipeline is built for factual lookups (does this
    specific fact exist in the metrics / topic index / full report?), and
    a summarization request isn't a fact-lookup, so its "insufficient ->
    escalate -> found?" logic could incorrectly conclude there's "nothing"
    to summarize. This is a plain, single-shot synthesis call instead.
    """
    if not metrics:
        return "No metrics have been extracted yet — nothing to summarize."

    client = genai.Client()
    coverage = brsr_coverage_report(metrics)
    try:
        resp = _call_gemini_with_retry(
            client,
            SUMMARY_PROMPT.format(
                company_name=company_name,
                metrics_json=json.dumps(metrics)[:150_000],
                coverage_json=json.dumps({num: info["covered"] for num, info in coverage.items()}),
            ),
            {},  # plain text response, not JSON
        )
        return (resp.text or "").strip() or "The model returned an empty response — try again."
    except Exception as e:
        # This used to be unguarded — any failure here (rate limit exhausted,
        # blocked response, network issue) crashed the entire page with a
        # raw traceback instead of a readable message.
        return f"⚠️ Couldn't generate the summary right now: {type(e).__name__}: {e}"


def answer_chat_question(
    question: str,
    company_name: str,
    metrics: list[dict],
    topic_index: list[dict],
    page_map: dict[int, str],
    full_text: str,
    chat_history: list[dict] | None = None,
    progress_callback=None,
) -> dict:
    """
    Answers a follow-up chat question using the tiered lookup described
    above. progress_callback, if given, is called as
    progress_callback(current_step, total_steps, status_text).

    Returns {"answer": str, "tier": 1|2|3, "debug": [list of what was tried]}
    """
    chat_history = chat_history or []
    history_text = "\n".join(
        f"Q: {h['question']}\nA: {h['answer']}" for h in chat_history[-5:]
    ) or "(none yet)"
    client = genai.Client()
    debug = []

    # --- Tier 1: metrics table only ---
    if progress_callback:
        progress_callback(1, 3, "Checking extracted metrics...")
    metrics_json = json.dumps(metrics)[:150_000]  # metrics tables are small; this is just a safety cap
    try:
        resp = _call_gemini_with_retry(
            client,
            CHAT_TIER1_PROMPT.format(
                company_name=company_name, metrics_json=metrics_json,
                history_text=history_text, question=question,
            ),
            {"response_mime_type": "application/json"},
            progress_callback=progress_callback,
        )
        parsed = json.loads((resp.text or "").strip())
        debug.append({"tier": 1, "status": "ok", "detail": f"sufficient={parsed.get('sufficient')}"})
        if parsed.get("sufficient", True) and parsed.get("answer"):
            return {"answer": parsed["answer"], "tier": 1, "debug": debug}
        search_terms = parsed.get("search_terms", [])
    except Exception as e:
        debug.append({"tier": 1, "status": "error", "detail": str(e)})
        search_terms = []

    # --- Tier 2: pre-built topic index -> real page text ---
    if progress_callback:
        progress_callback(2, 3, "Looking up relevant report sections in the topic index...")
    terms = [t.lower() for t in search_terms] or [question.lower()]
    matched_pages = set()
    for entry in topic_index:
        topic_lower = (entry.get("topic") or "").lower()
        if any(term in topic_lower or topic_lower in term for term in terms if term):
            matched_pages.update(entry.get("pages", []))

    if matched_pages:
        excerpts = "\n\n".join(
            f"--- Page {p} ---\n{page_map.get(p, '(page text unavailable)')}"
            for p in sorted(matched_pages)[:6]
        )
        try:
            resp = _call_gemini_with_retry(
                client,
                CHAT_TIER2_PROMPT.format(
                    company_name=company_name, excerpts=excerpts,
                    history_text=history_text, question=question,
                ),
                {"response_mime_type": "application/json"},
                progress_callback=progress_callback,
            )
            parsed = json.loads((resp.text or "").strip())
            debug.append({
                "tier": 2, "status": "ok",
                "detail": f"matched pages={sorted(matched_pages)}, found={parsed.get('found')}",
            })
            if parsed.get("found", True) and parsed.get("answer"):
                return {"answer": parsed["answer"], "tier": 2, "debug": debug}
        except Exception as e:
            debug.append({"tier": 2, "status": "error", "detail": str(e)})
    else:
        debug.append({"tier": 2, "status": "no_match", "detail": f"No topic index entries matched: {terms}"})

    # --- Tier 3: last resort, chunk-scan the full report ---
    if progress_callback:
        progress_callback(0, 1, "Not in the topic index — scanning the full report as a last resort...")
    chunks = chunk_text(full_text)
    for idx, chunk in enumerate(chunks):
        if progress_callback:
            progress_callback(idx + 1, len(chunks), f"Scanning full report... chunk {idx + 1}/{len(chunks)}")
        try:
            resp = _call_gemini_with_retry(
                client,
                CHAT_TIER2_PROMPT.format(
                    company_name=company_name, excerpts=chunk,
                    history_text=history_text, question=question,
                ),
                {"response_mime_type": "application/json"},
                progress_callback=progress_callback,
            )
            parsed = json.loads((resp.text or "").strip())
            if parsed.get("found") and parsed.get("answer"):
                debug.append({"tier": 3, "status": "ok", "detail": f"found in chunk {idx + 1}"})
                return {"answer": parsed["answer"], "tier": 3, "debug": debug}
        except Exception as e:
            debug.append({"tier": 3, "status": "error", "detail": f"chunk {idx + 1}: {e}"})
        if idx < len(chunks) - 1:
            time.sleep(2)

    debug.append({"tier": 3, "status": "not_found", "detail": "Scanned the full report, no answer found."})
    return {
        "answer": (
            "I couldn't find anything about this in the extracted metrics, the topic index, "
            "or a full scan of the report. It may genuinely not be covered in this document."
        ),
        "tier": 3,
        "debug": debug,
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python extractor.py <path_to_report.pdf> [company_name]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    company = sys.argv[2] if len(sys.argv) > 2 else "Unknown"

    print(f"Extracting text from {pdf_path}...")
    text = extract_text_from_pdf(pdf_path)
    print(f"Extracted {len(text)} characters. Running LLM extraction...")

    metrics, debug_log, topic_index, page_map = extract_esg_metrics(text, company_name=company)
    print(f"\nFound {len(metrics)} metrics, {len(topic_index)} topic entries.\n")
    print(json.dumps(metrics, indent=2))

    coverage = brsr_coverage_report(metrics)
    print("\n--- BRSR Core Coverage ---")
    for num, info in coverage.items():
        status = "✅" if info["covered"] else "❌"
        print(f"{status} Principle {num}: {info['principle']}")
