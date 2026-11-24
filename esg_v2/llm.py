"""
Gemini call for one datapoint group.

The selected pages are sent as a real PDF (not extracted text), so the model
sees table rows and columns as laid out. The model is asked ONLY to copy raw
figures and units exactly as printed — every conversion and formula runs in
Python afterwards. Output is constrained with a JSON schema.
"""

import json
import os
import random
import time

from .config import Datapoint

DEFAULT_MODEL = "gemini-3.5-flash"


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


PROMPT = """You are a meticulous ESG data analyst extracting figures from an Indian listed company's \
annual report / Business Responsibility and Sustainability Report (BRSR).

The attached PDF contains ONLY selected pages of the report. Page mapping \
(attachment page -> ORIGINAL report page): {page_map}

Extract each datapoint listed below.

RULES (follow strictly):
1. Copy every figure EXACTLY as printed in "*_raw" (keep commas, decimals, brackets, "Nil"). \
Do NOT calculate, sum, convert, round or re-scale anything. Put the same figure as a plain number \
in "*_value" (commas removed). If a datapoint instruction explicitly asks you to COUNT items, you may count.
2. Units: give the unit exactly as printed for that figure. Many tables print the unit only in the \
header or caption (e.g. "(Rs. in crore)", "(in GJ)", "in Kilolitres (kL)") - use that. If no unit is \
printed anywhere for the figure, return an empty string.
3. Years: "current" = the most recent financial year column, "previous" = the year immediately before. \
Report the FY labels exactly as printed in fy_current / fy_previous. Never swap the columns.
4. Read the right ROW and COLUMN: e.g. "Total employees (D+E)" is not "Permanent employees"; \
"Total" column is not "Male"/"Female"; employees are not workers; consumption is not withdrawal.
5. If a datapoint is not present on these pages, set found=false and leave the values null. \
NEVER guess or carry a number from another row.
6. "page" = the ORIGINAL report page number (use the mapping above) where the figure is printed.
7. "quote" = a short verbatim snippet from that row (row label plus the figure), max 25 words.
8. For text datapoints, write a concise answer in "text" (e.g. "Yes - CSR & ESG Committee") \
and leave the numeric fields null.
9. Use "note" for anything an analyst should know (e.g. "only consolidated figure given", \
"location-based Scope 2", "health & safety training only").

DATAPOINTS (id | name | where it usually is | what exactly to extract):
{datapoints}
"""


def build_prompt(datapoints: list[Datapoint], page_numbers: list[int]) -> str:
    mapping = ", ".join(f"{i + 1}->{p}" for i, p in enumerate(page_numbers))
    lines = "\n".join(
        f"- {dp.id} | {dp.name} | {dp.brsr_reference} | {dp.instruction}"
        + ("" if dp.kind != "text" else " [TEXT answer]")
        for dp in datapoints
    )
    return PROMPT.format(page_map=mapping, datapoints=lines)


def response_schema(datapoints: list[Datapoint]) -> dict:
    nullable_num = {"type": ["number", "null"]}
    nullable_str = {"type": ["string", "null"]}
    return {
        "type": "object",
        "properties": {
            "fy_current": {"type": "string"},
            "fy_previous": {"type": "string"},
            "datapoints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": [dp.id for dp in datapoints]},
                        "found": {"type": "boolean"},
                        "current_raw": nullable_str,
                        "current_value": nullable_num,
                        "current_unit": nullable_str,
                        "previous_raw": nullable_str,
                        "previous_value": nullable_num,
                        "previous_unit": nullable_str,
                        "text": nullable_str,
                        "page": {"type": ["integer", "null"]},
                        "quote": nullable_str,
                        "note": nullable_str,
                    },
                    "required": ["id", "found"],
                },
            },
        },
        "required": ["fy_current", "datapoints"],
    }


_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set (put it in .env or Streamlit secrets).")
        _client = genai.Client(api_key=api_key)
    return _client


def _retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("429", "500", "503", "504", "resource_exhausted", "unavailable",
                                  "rate limit", "high demand", "deadline", "timeout", "overloaded"))


def gemini_extract(pdf_bytes: bytes, prompt: str, schema: dict, max_retries: int = 5, on_wait=None) -> dict:
    """Calls Gemini with the page-subset PDF + prompt and returns parsed JSON."""
    from google.genai import types

    client = _get_client()
    contents = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), prompt]
    config_kwargs = dict(temperature=0, response_mime_type="application/json", max_output_tokens=32_768)
    try:
        config = types.GenerateContentConfig(response_json_schema=schema, **config_kwargs)
    except Exception:  # older SDKs: plain JSON mode, the prompt still defines the shape
        config = types.GenerateContentConfig(**config_kwargs)

    delay = 5.0
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(model=model_name(), contents=contents, config=config)
            raw = (resp.text or "").strip()
            if raw.startswith("```"):
                raw = raw.strip("`").split("\n", 1)[-1]
            return json.loads(raw)
        except json.JSONDecodeError:
            if attempt >= max_retries:
                raise
        except Exception as exc:
            if not _retryable(exc) or attempt >= max_retries:
                raise
        wait = delay + random.uniform(0.5, 2.5)
        if on_wait:
            on_wait(wait, attempt + 1)
        time.sleep(wait)
        delay = min(delay * 2, 60)
    raise RuntimeError("unreachable")
