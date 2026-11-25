"""
Number parsing and unit conversion — done in Python, never by the LLM.

Canonical units by datapoint kind:
    energy      GJ
    emission    tCO2e
    air_mass    kg
    waste_mass  tonnes
    water       kL
    money       Rs. crore
    pct         %
    count       number
    ratio       as printed (no conversion)
    intensity   kept as printed (used only for cross-checks)
    text        n/a
"""

import re

CANONICAL_UNIT = {
    "energy": "GJ",
    "emission": "tCO2e",
    "air_mass": "kg",
    "waste_mass": "tonnes",
    "water": "kL",
    "money": "Rs. crore",
    "pct": "%",
    "count": "number",
    "ratio": "",
}

_MULTIPLIER_WORDS = [
    (r"\bbillions?\b|\bbn\b", 1e9),
    (r"\bcrores?\b|\bcr\b", 1e7),
    (r"\bmillions?\b|\bmn\b|\bmio\b|\bmillion\b", 1e6),
    (r"\blakhs?\b|\blacs?\b|\blac\b", 1e5),
    (r"\bthousands?\b|'000|’000|\b000s\b|\bin 000\b", 1e3),
]

_NULL_WORDS = {"", "-", "–", "—", "na", "n.a.", "n/a", "nan", "none", "not applicable", "not available",
               "not reported", "not disclosed", "null"}
_ZERO_WORDS = {"nil", "zero", "0", "no"}


def parse_number(raw):
    """'1,23,456.78' -> 123456.78; '(12)' -> -12; 'Nil' -> 0; '245:1' -> 245; '-' -> None."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().lower()
    if s in _NULL_WORDS:
        return None
    if s in _ZERO_WORDS:
        return 0.0
    s = s.replace("₹", "").replace("rs.", "").replace("inr", "")
    if ":" in s:
        s = s.split(":")[0]
    negative = s.startswith("(") and s.rstrip(" %").endswith(")")
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+", s)
    if not m:
        return None
    num = float(m.group(0).replace(",", ""))
    if negative and num > 0:
        num = -num
    # number words written next to the figure, e.g. "1.2 lakh"
    rest = s[m.end():]
    for pattern, factor in _MULTIPLIER_WORDS:
        if re.match(r"\s*(?:" + pattern + r")", rest):
            num *= factor
            break
    return num


def _split_multiplier(unit: str) -> tuple[float, str]:
    u = f" {unit.lower()} "
    for pattern, factor in _MULTIPLIER_WORDS:
        if re.search(pattern, u):
            return factor, re.sub(pattern, " ", u).strip()
    return 1.0, u.strip()


def _energy_factor(u: str):
    # order matters: check longer prefixes first
    table = [
        (r"\bpj\b|petajoule", 1e6), (r"\btj\b|terajoule", 1e3), (r"\bgj\b|gigajoule", 1.0),
        (r"\bmj\b|megajoule", 1e-3), (r"\bkj\b|kilojoule", 1e-6),
        (r"\bgwh\b", 3600.0), (r"\bmwh\b", 3.6), (r"\bkwh\b|\bunits\b", 0.0036),
        (r"\btoe\b|tonnes? of oil equivalent", 41.868),
    ]
    for pattern, f in table:
        if re.search(pattern, u):
            return f
    return None


def _tonne_factor(u: str, original: str = ""):
    """Factor to tonnes for a mass unit string. In Indian reports 'MT' means
    metric tonnes; only a case-sensitive 'Mt' is read as million tonnes
    ('million' as a word is handled by the multiplier step)."""
    if re.search(r"\bMt\b|\bMtCO2", original):
        return 1e6
    if re.search(r"\bkt\b|kilo\s*tonnes?|ktco2", u):
        return 1e3
    if re.search(r"\bkg\b|kgs\b|kilogram|kgco2", u):
        return 1e-3
    if re.search(r"(?<![a-z])g\b|gram", u) and "kg" not in u:
        return 1e-6
    if re.search(r"\bt\b|tonne|\bton|\bmt\b|tco2|metric", u):
        return 1.0
    return None


def _water_factor(u: str):
    if re.search(r"\bml\b|megalit|mega lit", u):
        return 1e3
    if re.search(r"\bkl\b|kilolit|kilo lit|m3|m³|cubic met|cum\b", u):
        return 1.0
    if re.search(r"\bl\b|lit(re|er)s?\b|ltrs?\b", u):
        return 1e-3
    return None


def _money_factor(u: str):
    if re.search(r"rs|inr|₹|rupee|\b(cr|crore|lakh|lac|million|mn|bn)\b", u) or u == "":
        return 1e-7  # rupees -> crore
    if re.search(r"usd|\$", u):
        return None  # no FX conversion
    return None


def normalize(value, unit, kind):
    """
    Returns (normalized_value, canonical_unit, problem). problem is None when
    the conversion is safe; otherwise a short reason and normalized_value is
    None (the raw value is still kept by the caller).
    """
    if value is None:
        return None, CANONICAL_UNIT.get(kind, unit or ""), None
    unit = (unit or "").strip()
    if kind in ("pct", "count", "ratio"):
        return float(value), CANONICAL_UNIT[kind], None
    if kind in ("intensity", "text"):
        return float(value), unit, None

    mult, base = _split_multiplier(unit)
    if kind == "energy":
        f = _energy_factor(base)
    elif kind == "emission":
        f = _tonne_factor(base, unit)
    elif kind == "air_mass":
        t = _tonne_factor(base, unit)
        f = None if t is None else t * 1000.0
    elif kind == "waste_mass":
        f = _tonne_factor(base, unit)
    elif kind == "water":
        f = _water_factor(base)
    elif kind == "money":
        f = _money_factor(base)
        if f is not None and mult == 1.0 and not unit:
            return None, CANONICAL_UNIT[kind], "money value without a unit"
    else:
        return float(value), unit, None

    if f is None:
        return None, CANONICAL_UNIT[kind], f"cannot convert unit '{unit}' for {kind}"
    return float(value) * mult * f, CANONICAL_UNIT[kind], None


def intensity_to_per_crore(value, unit, numerator_kind):
    """
    Converts a reported intensity such as '0.0000123 GJ per rupee of turnover'
    into canonical-numerator per Rs. crore (e.g. GJ per Rs. crore).
    Returns None when the denominator is not a plain rupee turnover
    (e.g. PPP-adjusted, per tonne of product).
    """
    if value is None or not unit:
        return None
    u = unit.lower()
    if "ppp" in u or "purchasing power" in u:
        return None
    parts = re.split(r"/|\bper\b", u, maxsplit=1)
    if len(parts) != 2:
        return None
    num, den = parts
    if not re.search(r"rupee|rs|inr|₹|revenue|turnover|crore|lakh|million", den):
        return None
    den_mult, _ = _split_multiplier(den)  # crore -> 1e7 rupees, lakh -> 1e5, plain rupee -> 1
    rupees = den_mult
    n_val, _, problem = normalize(1.0, num.strip(), numerator_kind)
    if problem or n_val is None:
        return None
    return float(value) * n_val / rupees * 1e7
