"""
Finds the few pages that matter for each datapoint group, so the LLM reads
10-16 focused pages instead of a 300-page annual report.

Scoring is deliberately simple and transparent: each datapoint keyword
found on a page adds 1, each group heading adds 2, and a page that looks
like a table of numbers gets a small boost. Tables often run onto the next
page, so the page after each top hit is added too.
"""

import re

from .config import GROUP_HEADINGS, Datapoint

MAX_PAGES_PER_GROUP = 16
MIN_SCORE = 3


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def score_pages(page_texts: dict[int, str], datapoints: list[Datapoint], group: str) -> dict[int, float]:
    keywords = sorted({k for dp in datapoints for k in dp.keywords})
    headings = GROUP_HEADINGS.get(group, [])
    scores = {}
    for page, text in page_texts.items():
        if not text:
            continue
        t = _norm(text)
        s = sum(1 for k in keywords if k in t)
        s += sum(2 for h in headings if h in t)
        if s:
            digits = len(re.findall(r"\d[\d,]*\.?\d*", t))
            s += min(digits / 80.0, 2.0)  # numeric-heavy pages are likely tables
        if s:
            scores[page] = s
    return scores


def select_pages(page_texts: dict[int, str], datapoints: list[Datapoint], group: str,
                 max_pages: int = MAX_PAGES_PER_GROUP) -> list[int]:
    scores = score_pages(page_texts, datapoints, group)
    ranked = [p for p, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s >= MIN_SCORE]
    if not ranked:  # weak signal: still take the best few rather than nothing
        ranked = [p for p, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:4]]

    last_page = max(page_texts) if page_texts else 0
    spill = 4  # room kept for "table continues on next page"
    chosen: list[int] = ranked[: max(1, max_pages - spill)]
    for p in ranked[:spill]:
        nxt = p + 1
        if nxt <= last_page and nxt not in chosen and len(chosen) < max_pages:
            chosen.append(nxt)
    return sorted(chosen)
