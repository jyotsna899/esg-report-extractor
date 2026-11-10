"""
ESG Report Extractor (Gemini version)
---------------------------------------
Takes a company sustainability/ESG/BRSR report (PDF) and uses Gemini to:
  1. Extract a fixed set of ESG + financial KPIs (see kpis.py), each
     with the current and previous financial year when the report shows
     both; unit conversions and ratios are computed in Python and flagged
  2. Build a lightweight topic index (topic -> page numbers) as a free
     byproduct of the same extraction pass, so later chat questions can
     jump straight to the right page instead of rescanning the report
  3. Answer follow-up chat questions using a tiered lookup: KPI table
     first (cheap), then the topic index (targeted), then a full report
     scan only as a last resort

Usage:
    from extractor import extract_text_from_pdf, extract_kpis, answer_chat_question

    text = extract_text_from_pdf("company_report.pdf")
    kpis, debug_log, topic_index, page_map = extract_kpis(text, company_name="Acme Corp")
    result = answer_chat_question("Tell me about their rainwater harvesting", "Acme Corp", kpis, topic_index, page_map, text)
"""

import gc
import json
import os
import re
import time
import pdfplumber
from google import genai

from kpis import (
    KPI_COUNT,
    build_kpi_prompt_block,
    compact_for_prompt,
    coverage,
    merge_chunk_kpis,
    normalize_kpis,
)


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
# asking it to extract the KPIs AND build the full topic index for a
# huge chunk in one response risks hitting that ceiling and getting cut
# off mid-JSON. Smaller chunks keep each response comfortably inside the
# limit. (Previously 400,000 — large enough that a full-document "scan
# whole doc" run could collapse into a single oversized call.)

MODEL_NAME = "gemini-3.5-flash-lite"
MAX_OUTPUT_TOKENS = 65_536
# temperature=0 makes the model always pick its most confident output
# instead of sampling somewhat randomly. It won't guarantee byte-identical
# output on every run, but it removes most of the run-to-run drift that
# was causing the same PDF to produce different KPI values each time.
TEMPERATURE = 0


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
                import random
                sleep_sec = delay + random.uniform(1.0, 3.0)
                if progress_callback:
                    progress_callback(
                        0, 1,
                        f"Model busy — waiting {sleep_sec:.1f}s before retry ({attempt + 1}/{max_retries})...",
                    )
                time.sleep(sleep_sec)
                delay = min(delay * 2, 45)
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


def _get_page_text_fast(pdf_path: str):
    """Yields (page_index_1_based, text) using pypdfium2 if available, else pdfplumber."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        total = len(pdf)
        for i in range(total):
            page = pdf[i]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            yield i + 1, total, (text or "").strip()
        pdf.close()
        gc.collect()
        return
    except Exception:
        pass

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            _flush_pdf_page(page)
            yield i + 1, total, text.strip()
            if (i + 1) % 50 == 0:
                gc.collect()
    gc.collect()


def find_brsr_start_page(pdf_path: str, progress_callback=None) -> int | None:
    """
    Scans the PDF for the page where the ESG/sustainability/BRSR section
    actually begins. Uses fast text extraction to prevent memory bloat.
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

    candidates = []
    blank_pages = 0
    total_pages = 0
    for page_num, total, text in _get_page_text_fast(pdf_path):
        total_pages = total
        if progress_callback:
            progress_callback(page_num, total)
        if not text:
            blank_pages += 1
            continue
        lower = " ".join(text.lower().split())
        for phrase_rank, phrase in enumerate(heading_phrases):
            if phrase in lower:
                position = lower.find(phrase)
                is_early_on_page = position < 300
                is_short_page = len(text) < 2000
                candidates.append((page_num, is_early_on_page, len(text), phrase_rank))
                if is_early_on_page and is_short_page and phrase_rank <= HIGH_CONFIDENCE_RANK:
                    return page_num
                break

    if not candidates:
        if blank_pages == total_pages and total_pages > 0:
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


def extract_text_from_pdf(pdf_path: str, max_pages: int = 100_000, start_page: int = 1, progress_callback=None) -> str:
    """
    Ultra-fast, low-memory text extractor using pypdfium2 with automatic fallback
    to pdfplumber. Keeps RAM bounded under 50 MB even on 500+ page PDFs.
    """
    full_text = []
    # Fast path: pypdfium2
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        total_in_pdf = len(pdf)
        start_idx = max(0, start_page - 1)
        end_idx = min(total_in_pdf, start_idx + max_pages)
        total = max(1, end_idx - start_idx)
        for i, page_idx in enumerate(range(start_idx, end_idx)):
            page = pdf[page_idx]
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            textpage.close()
            page.close()
            if text and text.strip():
                full_text.append(f"\n--- Page {page_idx + 1} ---\n{text.strip()}")
            if progress_callback:
                progress_callback(i + 1, total)
        pdf.close()
        gc.collect()
        return "\n".join(full_text)
    except Exception:
        pass

    # Fallback path: pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        total_in_pdf = len(pdf.pages)
        start_idx = max(0, start_page - 1)
        end_idx = min(total_in_pdf, start_idx + max_pages)
        total = max(1, end_idx - start_idx)
        for i, page_idx in enumerate(range(start_idx, end_idx)):
            page = pdf.pages[page_idx]
            text = page.extract_text()
            _flush_pdf_page(page)
            if text and text.strip():
                full_text.append(f"\n--- Page {page_idx + 1} ---\n{text.strip()}")
            if progress_callback:
                progress_callback(i + 1, total)
            if (i + 1) % 50 == 0:
                gc.collect()
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
# Step 3: Ask the LLM for the fixed KPI list (current + previous year) and
# build a topic index in the same pass (no extra API cost)
# ---------------------------------------------------------------------
EXTRACTION_PROMPT = """You are an ESG data analyst. Read the report text below, which includes \
"--- Page N ---" markers. Extract ONLY the {kpi_count} KPIs listed below — nothing else.

GROUNDING RULES (follow strictly):
- Every value must be LITERALLY STATED in the text below. Never invent, estimate, or use \
outside knowledge. Do NOT do any arithmetic or unit conversion yourself — copy the number and \
the unit exactly as printed. Conversions and ratios are computed later in code.
- Reports often show the current year AND previous years side by side (e.g. "FY 2023-24", "FY 2024-25", \
"FY 2025-26" columns). Capture all reported years: set "current" to the most recent financial year, \
"previous" to the year before it, and also include all reported years under "years": \
{{"<FY label e.g. FY2024>": {{...}}, ...}}.
- If a KPI is not printed in its required unit, still report what IS printed (with its own \
unit), and also fill "inputs" with the raw figures listed for that KPI so it can be computed \
(e.g. energy intensity printed "per rupee of turnover" -> give that value and unit as-is; \
water recycling rate not printed -> give water_recycled and water_consumption in "inputs").
- Only include a KPI if this text actually discloses something for it. Skip it otherwise.

KPI LIST (ID | Name | Required unit/format | components / inputs):
{kpi_block}

Return ONLY a valid JSON object with exactly two keys, "kpis" and "topics". No preamble, no \
markdown fences.

1. "kpis": a list of objects, one per KPI found:
   {{
     "kpi_id": "E2",
     "current":  {{"fy": "FY 2024-25", "value": <number or null>, "unit": "<unit as printed>",
                   "components": {{<component name>: <number or text>}},
                   "text": "<short text for Yes/No / qualitative KPIs, else null>",
                   "inputs": {{<input name>: {{"value": <number>, "unit": "<unit as printed>"}}}}}},
     "previous": {{ same shape, or null if the previous year is not shown }},
     "source_context": "<short VERBATIM snippet containing the figure>",
     "pages": [<page numbers from the --- Page N --- markers>]
   }}
   - Use "components" for KPIs that list components (e.g. S10 filed/resolved/pending, F1 \
revenue/ebitda/pat, S12 budgeted/spent, G3 count/pct, E10 status/target_year/scope).
   - For F1, use revenue from operations (standalone if both standalone and consolidated are \
shown, and say which in "text"). Put the printed money unit (e.g. "Rs. crore", "Rs. lakh", \
"Rs. million") in "unit".
   - For Yes/No KPIs (E10, G5, G7, G8, G9, G10) put "Yes"/"No" (or the assurance level for \
G8) in components.status and names/frameworks/years/providers in components.detail or the \
listed components.

2. "topics": a navigation index of every distinct subject discussed in this text, including \
prose-only subjects (e.g. "Rainwater harvesting", "Board diversity policy"). For each: \
{{"topic": short phrase (3-6 words), "pages": [page numbers]}}.

If nothing is found for a key, return [] for it.

REPORT TEXT:
{chunk}
"""


def extract_kpis(
    text: str,
    company_name: str = "Unknown",
    progress_callback=None,
) -> tuple[list[dict], list[dict], list[dict], dict[int, str]]:
    """
    Runs the KPI extraction prompt over the report text, chunk by chunk,
    with a short delay between calls to stay under rate limits.

    progress_callback, if given, is called as
    progress_callback(current_chunk, total_chunks, status_text).

    Returns (kpis, debug_log, topic_index, page_map):
      - kpis: one normalized record per catalog KPI (see kpis.py), with
        not-disclosed ones included so every report has the same rows
      - debug_log: one entry per chunk describing what happened
      - topic_index: list of {"topic": ..., "pages": [...], "chunk": N}
      - page_map: {page_number: page_text}, built locally from the text
    """
    page_map = build_page_map(text)

    if not text.strip():
        return (
            normalize_kpis({}, company_name),
            [{"chunk": 0, "status": "error", "detail": "No text was extracted from the PDF for this page range — nothing was sent to the model."}],
            [],
            page_map,
        )

    client = genai.Client()  # picks up GEMINI_API_KEY from env automatically
    chunks = chunk_text(text)
    total = len(chunks)
    per_chunk_kpis = []
    topic_index = []
    debug_log = []
    kpi_block = build_kpi_prompt_block()

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
                EXTRACTION_PROMPT.format(chunk=chunk, kpi_block=kpi_block, kpi_count=KPI_COUNT),
                {"response_mime_type": "application/json", "max_output_tokens": MAX_OUTPUT_TOKENS},
                progress_callback=progress_callback,
            )
            # Reading .text can itself raise when a response is cut off, so
            # it stays inside the try and becomes a per-chunk failure.
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
                raise ValueError(f"Expected a JSON object with 'kpis'/'topics' keys, got {type(parsed).__name__}")

            chunk_kpis = parsed.get("kpis", []) or []
            chunk_topics = parsed.get("topics", []) or []
            per_chunk_kpis.append(chunk_kpis)

            for t in chunk_topics:
                t["chunk"] = idx + 1
            topic_index.extend(chunk_topics)

            found_ids = sorted({str(k.get("kpi_id")) for k in chunk_kpis if isinstance(k, dict)})
            debug_log.append({
                "chunk": idx + 1,
                "status": "ok",
                "detail": f"Found {len(found_ids)} KPIs ({', '.join(found_ids) or 'none'}), {len(chunk_topics)} topics.",
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

    kpis = normalize_kpis(merge_chunk_kpis(per_chunk_kpis), company_name)
    gc.collect()
    return kpis, debug_log, topic_index, page_map

# ---------------------------------------------------------------------
# Step 4: Tiered chat Q&A
#   Tier 1 - answer from the KPI table alone (cheap, fast)
#   Tier 2 - if insufficient, look up the pre-built topic index and
#            answer from those specific real pages (targeted, no rescan)
#   Tier 3 - last resort: chunk-scan the full report (rare; only when
#            the topic index genuinely missed something)
# ---------------------------------------------------------------------
CHAT_TIER1_PROMPT = """You are an ESG analyst assistant helping a user work with a company's \
extracted KPI table (a fixed set of ESG and financial KPIs, each with a current and a \
previous financial year). Rows with status "derived" were computed from stated figures — their \
"derivation" field shows how. Rows with status "not_disclosed" were not found in the report. Use \
the KPI table below (JSON) and the conversation history to respond to the user's latest message.

Two kinds of requests both count as fully answerable from the KPI table alone — mark them \
"sufficient": true and give a complete response, not a thin one:
1. Factual questions about a KPI value, unit, year, year-over-year change, or how a derived \
   value was computed.
2. Any request to reformulate, summarize, analyze, or creatively present the KPIs that are \
   already in the table — e.g. "write an executive summary," "turn the governance KPIs into \
   a short story for investors," "make this sound more compelling." These don't need any new \
   facts beyond what's already extracted, so they are always sufficient — write the full \
   requested piece (summary, story, pitch, whatever was asked), don't just describe what you \
   could write.

Only mark "sufficient": false when the user is asking about something that genuinely isn't in \
the KPI table — e.g. asking about a program, policy, or explanation not captured as a bare \
number (like "how does their rainwater harvesting work"). In that case, don't guess or pad a \
thin answer; say so and suggest search terms instead.

Return ONLY a valid JSON object with exactly these keys:
- "answer": your full response in plain text
- "sufficient": true if the KPI table gave you enough (see both cases above), false if you need the fuller report text
- "search_terms": if sufficient is false, 2-5 short keywords/phrases to search a topic index for (e.g. ["rainwater harvesting", "water conservation"])

COMPANY: {company_name}

KPI TABLE (JSON):
{kpis_json}

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
of {company_name}'s ESG performance, based entirely on the KPI table below. Cover: notable \
strengths and standout figures, year-over-year movement (current vs previous year) for the \
KPIs that have both, any concerning numbers, and disclosure gaps — name the KPIs that were not \
disclosed. Mention when a figure is derived rather than printed. Write it as plain prose, no \
JSON, no markdown headers, just the summary text itself.

KPI TABLE (JSON):
{kpis_json}

COVERAGE: {disclosed} of {total} KPIs disclosed. Not disclosed: {missing}
"""


def generate_summary(company_name: str, kpis: list[dict]) -> str:
    """
    Writes an executive summary directly from the KPI table in a single
    call. Deliberately NOT routed through answer_chat_question's tiered
    Q&A pipeline — that pipeline is built for factual lookups (does this
    specific fact exist in the KPI table / topic index / full report?), and
    a summarization request isn't a fact-lookup, so its "insufficient ->
    escalate -> found?" logic could incorrectly conclude there's "nothing"
    to summarize. This is a plain, single-shot synthesis call instead.
    """
    if not kpis:
        return "No KPIs have been extracted yet — nothing to summarize."

    client = genai.Client()
    cov = coverage(kpis)
    try:
        resp = _call_gemini_with_retry(
            client,
            SUMMARY_PROMPT.format(
                company_name=company_name,
                kpis_json=json.dumps(compact_for_prompt(kpis))[:150_000],
                disclosed=cov["disclosed"],
                total=cov["total"],
                missing=", ".join(f'{r["kpi_id"]} {r["kpi_name"]}' for r in cov["missing"]) or "none",
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
    kpis: list[dict],
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

    # --- Tier 1: KPI table only ---
    if progress_callback:
        progress_callback(1, 3, "Checking extracted KPIs...")
    kpis_json = json.dumps(compact_for_prompt(kpis))[:150_000]  # a few dozen rows per report; just a safety cap
    try:
        resp = _call_gemini_with_retry(
            client,
            CHAT_TIER1_PROMPT.format(
                company_name=company_name, kpis_json=kpis_json,
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
            "I couldn't find anything about this in the extracted KPIs, the topic index, "
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

    kpis, debug_log, topic_index, page_map = extract_kpis(text, company_name=company)
    for entry in debug_log:
        print(f"chunk {entry['chunk']}: {entry['status']} — {entry['detail']}")

    from kpis import format_year
    print(f"\n{'ID':<4} {'KPI':<38} {'Status':<22} {'Current':<40} Previous")
    for r in kpis:
        cur = f"{r['current'].get('fy') or ''} {format_year(r['current'])}".strip()
        prev = f"{r['previous'].get('fy') or ''} {format_year(r['previous'])}".strip()
        print(f"{r['kpi_id']:<4} {r['kpi_name'][:38]:<38} {r['status']:<22} {cur[:40]:<40} {prev[:60]}")
    cov = coverage(kpis)
    print(f"\n{cov['disclosed']}/{cov['total']} KPIs disclosed ({cov['derived']} derived), {len(topic_index)} topic entries.")
