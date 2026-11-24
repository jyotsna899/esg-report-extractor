"""
Checks that run after extraction. Nothing is silently dropped: problems are
attached as review flags so an analyst can look at exactly those cells.

1. Grounding  - the printed figure must actually appear in the page text.
                If it appears on another selected page, the page is corrected.
2. Sanity     - percentages within 0-100, parts not larger than totals,
                renewable + non-renewable ~ total, suspicious year-on-year jumps.
"""

import re

_NUM_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


def _numbers_on_page(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        s = m.group(0).strip("()").replace(",", "")
        try:
            out.append(abs(float(s)))
        except ValueError:
            continue
    return out


def _matches(target: float, candidates: list[float]) -> bool:
    t = abs(target)
    tol = max(1e-9, 0.0005 * t)
    return any(abs(c - t) <= tol for c in candidates)


def ground_value(value, page, page_texts: dict[int, str], search_pages: list[int]):
    """
    Returns (page, flag). flag is None when the number was found on the stated page,
    'page_corrected' when found on a different page, 'not_found_in_text' when
    nowhere, or 'unverified_no_text' when the page has no extractable text (scanned).
    Small integers (0-31) are too common to verify meaningfully and pass.
    """
    if value is None:
        return page, None
    if float(value).is_integer() and 0 <= abs(value) <= 31:
        return page, None
    if page and page in page_texts:
        text = page_texts.get(page) or ""
        if not text.strip():
            return page, "unverified_no_text"
        if _matches(value, _numbers_on_page(text)):
            return page, None
    for p in search_pages:
        if p != page and _matches(value, _numbers_on_page(page_texts.get(p, ""))):
            return p, "page_corrected"
    if page and not (page_texts.get(page) or "").strip():
        return page, "unverified_no_text"
    return page, "not_found_in_text"


def _v(dps, dp_id, year):
    d = dps.get(dp_id)
    if not d:
        return None
    return (d["years"].get(year) or {}).get("norm_value")


def _flag(dps, dp_id, msg):
    if dp_id in dps:
        dps[dp_id]["flags"].append(msg)


def sanity_checks(dps: dict) -> None:
    """Adds flags in place to datapoint results."""
    for dp_id, d in dps.items():
        kind = d.get("kind")
        for year, slot in (d.get("years") or {}).items():
            v = (slot or {}).get("norm_value")
            if v is None:
                continue
            if kind == "pct" and not (0 <= v <= 100):
                _flag(dps, dp_id, f"{year}: % outside 0-100 ({v:g})")
            if kind in ("count", "energy", "emission", "water", "waste_mass", "air_mass") and v < 0:
                _flag(dps, dp_id, f"{year}: negative value")
        cur, prev = _v(dps, dp_id, "current"), _v(dps, dp_id, "previous")
        if cur and prev and kind not in ("pct", "text") and cur > 0 and prev > 0:
            ratio = cur / prev
            if ratio > 10 or ratio < 0.1:
                _flag(dps, dp_id, f"current vs previous differs {ratio:.1f}x - check units/column")

    pairs = [  # (part, whole, label)
        ("renewable_energy", "total_energy", "renewable > total energy"),
        ("non_renewable_energy", "total_energy", "non-renewable > total energy"),
        ("female_employees", "employees_total", "female > total employees"),
        ("male_employees", "employees_total", "male > total employees"),
        ("female_workers", "workers_total", "female > total workers"),
        ("women_directors", "board_size", "women directors > board size"),
        ("independent_directors", "board_size", "independent directors > board size"),
        ("women_kmp", "total_kmp", "women KMP > total KMP"),
        ("waste_recovered", "total_waste", "waste recovered > total waste"),
    ]
    for year in ("current", "previous"):
        for part, whole, label in pairs:
            a, b = _v(dps, part, year), _v(dps, whole, year)
            if a is not None and b is not None and a > b * 1.02:
                _flag(dps, part, f"{year}: {label}")
        r, n, t = (_v(dps, k, year) for k in ("renewable_energy", "non_renewable_energy", "total_energy"))
        if None not in (r, n, t) and t > 0 and abs((r + n) - t) / t > 0.03:
            _flag(dps, "total_energy", f"{year}: renewable + non-renewable != total ({(r + n):,.0f} vs {t:,.0f})")
        f, m, e = (_v(dps, k, year) for k in ("female_employees", "male_employees", "employees_total"))
        if None not in (f, m, e) and e > 0 and abs((f + m) - e) / e > 0.02:
            _flag(dps, "employees_total", f"{year}: male + female != total employees")
        spent, oblig = _v(dps, "csr_spent", year), _v(dps, "csr_obligation", year)
        if spent and oblig and not (0.5 <= spent / oblig <= 3):
            _flag(dps, "csr_spent", f"{year}: CSR spent/obligation = {spent / oblig:.2f} - check units")
