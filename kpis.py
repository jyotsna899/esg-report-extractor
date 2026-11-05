"""
The fixed KPI catalog (39 KPIs: E1–E11, S1–S16, G1–G10, F1–F2) and everything that turns raw LLM output into
one clean record per KPI.

The LLM only reads the report and writes down what it literally says
(values, units, and the raw inputs behind a ratio). Every unit conversion
and every computed ratio happens here, in plain Python, so derived numbers
are reproducible and each one carries the formula that produced it.

Record shape (one per KPI, every catalog KPI present in every report):
    {
      "kpi_id", "kpi_name", "category", "required_unit",
      "current":  {"fy", "value", "unit", "components", "text"},
      "previous": {"fy", "value", "unit", "components", "text"},
      "status": "reported" | "derived" | "reported_unit_mismatch" | "not_disclosed",
      "derivation": str | None,
      "source_context": str | None,
      "pages": [int, ...],
      "company": str,
    }
"""

import re

YEARS = ("current", "previous")
CATEGORIES = ("Environment", "Social", "Governance", "Financial")

# unit_kind drives normalization:
#   pct, count, rate_pmh, ratio, money, text, emission_abs,
#   energy_int, emission_int, air_int, water_int, waste_int
KPI_CATALOG = [
    # ---------------- ENVIRONMENT ----------------
    {"id": "E1", "name": "Renewable Energy Share", "category": "Environment",
     "required_unit": "% of total energy consumption", "unit_kind": "pct", "direction": "higher",
     "inputs": ["renewable_energy", "total_energy"]},
    {"id": "E2", "name": "Energy Intensity", "category": "Environment",
     "required_unit": "MJ per Rs. crore of revenue", "unit_kind": "energy_int", "direction": "lower",
     "inputs": ["total_energy"]},
    {"id": "E3", "name": "Scope 1 & Scope 2 Emission Intensity", "category": "Environment",
     "required_unit": "tCO2e per Rs. crore of revenue", "unit_kind": "emission_int", "direction": "lower",
     "inputs": ["scope1", "scope2"]},
    {"id": "E4", "name": "Scope 3 Emissions", "category": "Environment",
     "required_unit": "tCO2e", "unit_kind": "emission_abs", "direction": "lower", "inputs": []},
    {"id": "E5", "name": "Air Emission Intensity (SOx + NOx)", "category": "Environment",
     "required_unit": "kg per Rs. crore of revenue", "unit_kind": "air_int", "direction": "lower",
     "inputs": ["sox", "nox"]},
    {"id": "E6", "name": "Water Consumption Intensity", "category": "Environment",
     "required_unit": "kL per Rs. crore of revenue", "unit_kind": "water_int", "direction": "lower",
     "inputs": ["water_consumption"]},
    {"id": "E7", "name": "Water Recycling Rate", "category": "Environment",
     "required_unit": "% (recycled water / total water consumption)", "unit_kind": "pct", "direction": "higher",
     "inputs": ["water_recycled", "water_consumption"]},
    {"id": "E8", "name": "Waste Generation Intensity", "category": "Environment",
     "required_unit": "metric tonnes per Rs. crore of revenue", "unit_kind": "waste_int", "direction": "lower",
     "inputs": ["total_waste"]},
    {"id": "E9", "name": "Waste Recycling Rate", "category": "Environment",
     "required_unit": "% (recycled/recovered/reused waste / total waste)", "unit_kind": "pct", "direction": "higher",
     "inputs": ["waste_recovered", "total_waste"]},
    {"id": "E10", "name": "Net Zero Target Status", "category": "Environment",
     "required_unit": "Yes/No + target year + scope covered", "unit_kind": "text", "direction": None,
     "components": ["status", "target_year", "scope"]},
    {"id": "E11", "name": "International Targets / Commitments", "category": "Environment",
     "required_unit": "Text (e.g., SBTi validated, RE100)", "unit_kind": "text", "direction": None},
    # ---------------- SOCIAL ----------------
    {"id": "S1", "name": "Total Workforce Headcount", "category": "Social",
     "required_unit": "number (employees + workers)", "unit_kind": "count", "direction": None,
     "inputs": ["employees", "workers"]},
    {"id": "S2", "name": "Workplace Fatalities", "category": "Social",
     "required_unit": "number (employees and workers separately)", "unit_kind": "count", "direction": "lower",
     "components": ["employees", "workers"]},
    {"id": "S3", "name": "LTIFR", "category": "Social",
     "required_unit": "rate per million hours worked", "unit_kind": "rate_pmh", "direction": "lower"},
    {"id": "S4", "name": "Employee Well-being Investment", "category": "Social",
     "required_unit": "% of total payroll/revenue as reported", "unit_kind": "pct", "direction": "higher"},
    {"id": "S5", "name": "Attrition Rate", "category": "Social",
     "required_unit": "%", "unit_kind": "pct", "direction": "lower"},
    {"id": "S6", "name": "Gender Diversity Ratio", "category": "Social",
     "required_unit": "% female, % male", "unit_kind": "pct", "direction": None,
     "components": ["female_pct", "male_pct"], "inputs": ["female_count", "male_count"]},
    {"id": "S7", "name": "Income Inequality Ratio", "category": "Social",
     "required_unit": "highest paid salary / median remuneration", "unit_kind": "ratio", "direction": "lower",
     "inputs": ["highest_paid", "median_remuneration"]},
    {"id": "S8", "name": "Female to Male Wage Ratio", "category": "Social",
     "required_unit": "median female pay / median male pay", "unit_kind": "ratio", "direction": "higher",
     "inputs": ["median_female_pay", "median_male_pay"]},
    {"id": "S9", "name": "Training Coverage", "category": "Social",
     "required_unit": "% of employees trained", "unit_kind": "pct", "direction": "higher",
     "inputs": ["employees_trained", "total_employees"]},
    {"id": "S10", "name": "POSH Complaints", "category": "Social",
     "required_unit": "filed, resolved, pending", "unit_kind": "count", "direction": None,
     "components": ["filed", "resolved", "pending"]},
    {"id": "S11", "name": "Customer Complaints", "category": "Social",
     "required_unit": "received, resolved (pending if given)", "unit_kind": "count", "direction": None,
     "components": ["received", "resolved", "pending"]},
    {"id": "S12", "name": "CSR Budget", "category": "Social",
     "required_unit": "budgeted amount and amount spent (Rs. crore)", "unit_kind": "money", "direction": None,
     "components": ["budgeted", "spent"]},
    {"id": "S13", "name": "CSR Allocation Efficiency", "category": "Social",
     "required_unit": "% (spent / budgeted)", "unit_kind": "pct", "direction": "higher"},
    {"id": "S14", "name": "Rural & Semi-Urban Job Creation", "category": "Social",
     "required_unit": "headcount or % of total", "unit_kind": "text", "direction": "higher"},
    {"id": "S15", "name": "Investment in Aspirational Districts", "category": "Social",
     "required_unit": "Rs. crore", "unit_kind": "money", "direction": "higher"},
    {"id": "S16", "name": "Social Targets & Commitments", "category": "Social",
     "required_unit": "Text", "unit_kind": "text", "direction": None},
    # ---------------- GOVERNANCE ----------------
    {"id": "G1", "name": "Board Gender Diversity", "category": "Governance",
     "required_unit": "% women on board", "unit_kind": "pct", "direction": "higher",
     "inputs": ["women_directors", "board_size"]},
    {"id": "G2", "name": "KMP Gender Diversity", "category": "Governance",
     "required_unit": "% women in KMP", "unit_kind": "pct", "direction": "higher",
     "inputs": ["women_kmp", "total_kmp"]},
    {"id": "G3", "name": "Independent Directors", "category": "Governance",
     "required_unit": "number and % of board", "unit_kind": "count", "direction": "higher",
     "components": ["count", "pct"], "inputs": ["board_size"]},
    {"id": "G4", "name": "Board Committees", "category": "Governance",
     "required_unit": "number of active committees", "unit_kind": "count", "direction": None},
    {"id": "G5", "name": "Dedicated ESG Committee", "category": "Governance",
     "required_unit": "Yes/No (+ committee name)", "unit_kind": "text", "direction": None,
     "components": ["status", "detail"]},
    {"id": "G6", "name": "Board Attendance Rate", "category": "Governance",
     "required_unit": "average % for the year", "unit_kind": "pct", "direction": "higher"},
    {"id": "G7", "name": "Sustainability Report Released", "category": "Governance",
     "required_unit": "Yes/No + framework (BRSR, GRI, etc.)", "unit_kind": "text", "direction": None,
     "components": ["status", "detail"]},
    {"id": "G8", "name": "Assurance Status", "category": "Governance",
     "required_unit": "Reasonable / Limited / None (+ assurance provider)", "unit_kind": "text", "direction": None,
     "components": ["status", "detail"]},
    {"id": "G9", "name": "Climate Risk Framework", "category": "Governance",
     "required_unit": "Yes/No + TCFD alignment", "unit_kind": "text", "direction": None,
     "components": ["status", "detail"]},
    {"id": "G10", "name": "Green Bond Framework", "category": "Governance",
     "required_unit": "Yes/No + issuance details", "unit_kind": "text", "direction": None,
     "components": ["status", "detail"]},
    # ---------------- FINANCIAL ----------------
    {"id": "F1", "name": "Revenue, EBITDA, PAT", "category": "Financial",
     "required_unit": "Rs. crore (three separate values)", "unit_kind": "money", "direction": None,
     "components": ["revenue", "ebitda", "pat"]},
    {"id": "F2", "name": "Green Capex and Total Capex", "category": "Financial",
     "required_unit": "Rs. crore + green capex as % of total", "unit_kind": "money", "direction": "higher",
     "components": ["green_capex", "total_capex", "green_pct"]},
]

KPI_BY_ID = {k["id"]: k for k in KPI_CATALOG}
KPI_IDS = [k["id"] for k in KPI_CATALOG]
KPI_COUNT = len(KPI_CATALOG)
assert len(set(KPI_IDS)) == KPI_COUNT, "duplicate KPI id in KPI_CATALOG"

# Short guidance for the LLM on the inputs it should report whenever the KPI
# itself is not printed in the required unit. Keys must match "inputs" above.
INPUT_HINTS = {
    "renewable_energy": "energy from renewable sources (value + unit)",
    "total_energy": "total energy consumption (value + unit, e.g. GJ)",
    "scope1": "Scope 1 emissions (value + unit)",
    "scope2": "Scope 2 emissions (value + unit)",
    "sox": "SOx emissions (value + unit)",
    "nox": "NOx emissions (value + unit)",
    "water_consumption": "total water consumption (value + unit)",
    "water_recycled": "water recycled/reused (value + unit)",
    "total_waste": "total waste generated (value + unit)",
    "waste_recovered": "waste recycled + re-used + otherwise recovered (value + unit)",
    "employees": "number of permanent + other employees",
    "workers": "number of permanent + other workers",
    "female_count": "number of female employees + workers",
    "male_count": "number of male employees + workers",
    "highest_paid": "remuneration of the highest-paid person",
    "median_remuneration": "median remuneration of employees",
    "median_female_pay": "median remuneration of female employees",
    "median_male_pay": "median remuneration of male employees",
    "employees_trained": "number of employees who received training",
    "total_employees": "total employees used as the training denominator",
    "women_directors": "number of women on the board",
    "board_size": "total number of directors on the board",
    "women_kmp": "number of women KMPs",
    "total_kmp": "total number of KMPs",
}


def build_kpi_prompt_block() -> str:
    """Renders the catalog as prompt text so the KPI list lives in one place."""
    lines = []
    for category in CATEGORIES:
        lines.append(category.upper())
        for k in KPI_CATALOG:
            if k["category"] != category:
                continue
            line = f'{k["id"]} | {k["name"]} | {k["required_unit"]}'
            if k.get("components"):
                line += f' | components: {", ".join(k["components"])}'
            if k.get("inputs"):
                line += " | inputs if not printed in required unit: " + "; ".join(
                    f"{i} = {INPUT_HINTS.get(i, i)}" for i in k["inputs"]
                )
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------
def to_number(value):
    """Best-effort numeric coercion: 1,23,456.7 / '12.5%' / '(3.2)' -> float. None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("₹", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


def _fmt(n) -> str:
    if n is None:
        return "—"
    if isinstance(n, float):
        if n == int(n) and abs(n) < 1e15:
            return f"{int(n):,}"
        if abs(n) >= 100:
            return f"{n:,.2f}"
        return f"{n:.4g}"
    return str(n)


def _clean_unit(unit) -> str:
    u = (unit or "").lower().replace("₹", "rs").replace("inr", "rs").replace("rupees", "rupee")
    u = u.replace("co₂", "co2").replace("co 2", "co2")
    return " ".join(u.split())


def _split_ratio_unit(unit: str):
    """'GJ per Rs crore' / 'GJ/Rs. crore' -> ('gj', 'rs crore'). No denominator -> (unit, '')."""
    u = _clean_unit(unit)
    m = re.split(r"\s+per\s+|\s*/\s*", u, maxsplit=1)
    if len(m) == 2:
        return m[0].strip(), m[1].strip()
    return u.strip(), ""


# Revenue-denominator factor to reach "per Rs. crore". None = unknown/not rupees.
def _per_crore_factor(den: str):
    if not den:
        return None
    if any(t in den for t in ("ppp", "usd", "$", "dollar", "eur", "€")):
        return None
    if "crore" in den or re.search(r"\bcr\b", den):
        return 1.0
    if "lakh" in den or "lac" in den:
        return 100.0
    if "billion" in den or re.search(r"\bbn\b", den):
        return 0.1
    if "million" in den or re.search(r"\bmn\b|\bmio\b", den):
        return 10.0
    if "rupee" in den or re.search(r"\brs\b", den) or "turnover" in den or "revenue" in den:
        # "per rupee of turnover" (the standard BRSR phrasing)
        return 1e7
    return None


# Absolute-quantity factors to the KPI's base unit, by quantity type.
def _energy_to_mj(num: str):
    n = num.replace(" ", "")
    n = n.replace("terajoule", "tj").replace("gigajoule", "gj").replace("megajoule", "mj")
    for key, f in (("tj", 1e6), ("gj", 1e3), ("mj", 1.0), ("gwh", 3.6e6), ("mwh", 3600.0), ("kwh", 3.6)):
        if key in n:
            return f
    return None


def _emission_to_t(num: str):
    n = num.replace(" ", "")
    # "MT CO2e" in Indian reports means metric tonnes, so only an explicit
    # "million" is treated as a million-tonne unit.
    if "million" in n:
        return 1e6
    if "ktco2" in n or "thousandtco2" in n or "'000tco2" in n:
        return 1e3
    if "kgco2" in n:
        return 1e-3
    if "tco2" in n or "tonne" in n or "ton" in n or n in ("t", "mt"):
        return 1.0
    return None


def _mass_to_kg(num: str):
    n = num.replace(" ", "")
    if n.startswith("kg") or "kilogram" in n:
        return 1.0
    if n in ("g", "gram", "grams"):
        return 1e-3
    if "tonne" in n or "ton" in n or n in ("t", "mt", "metrictonnes"):
        return 1e3
    return None


def _mass_to_t(num: str):
    f = _mass_to_kg(num)
    return None if f is None else f / 1e3


def _water_to_kl(num: str):
    n = num.replace(" ", "")
    if "kl" in n or "kilolit" in n or "m3" in n or "cubicmet" in n or "m³" in n:
        return 1.0
    if n in ("ml",) or "megalit" in n or "millionlit" in n:
        return 1e3
    if "billionlit" in n:
        return 1e6
    if "lit" in n or n == "l":
        return 1e-3
    return None


def _money_to_crore(unit: str):
    u = _clean_unit(unit)
    if not u:
        return 1.0  # schema asks for Rs. crore; an empty unit is taken as already in crore
    if any(t in u for t in ("usd", "$", "dollar", "eur", "€")):
        return None
    if "crore" in u or re.search(r"\bcr\b", u):
        return 1.0
    if "lakh" in u or "lac" in u:
        return 0.01
    if "billion" in u or re.search(r"\bbn\b", u):
        return 100.0
    if "million" in u or re.search(r"\bmn\b", u):
        return 0.1
    if "rupee" in u or re.search(r"\brs\b", u):
        return 1e-7
    return None


INTENSITY_KINDS = {
    # kind: (numerator converter, target-unit label)
    "energy_int": (_energy_to_mj, "MJ per Rs. crore"),
    "emission_int": (_emission_to_t, "tCO2e per Rs. crore"),
    "air_int": (_mass_to_kg, "kg per Rs. crore"),
    "water_int": (_water_to_kl, "kL per Rs. crore"),
    "waste_int": (_mass_to_t, "t per Rs. crore"),
}


def _is_pct_unit(unit) -> bool:
    u = _clean_unit(unit)
    return (not u) or "%" in u or "percent" in u


def _empty_year():
    return {"fy": None, "value": None, "unit": None, "components": {}, "text": None, "inputs": {}}


def _clean_year(raw) -> dict:
    """Coerces one LLM year object into the canonical slot shape."""
    out = _empty_year()
    if not isinstance(raw, dict):
        return out
    fy = raw.get("fy")
    out["fy"] = str(fy) if fy not in (None, "") else None
    value = raw.get("value")
    num = to_number(value)
    out["value"] = num if num is not None else None
    out["unit"] = raw.get("unit") or None
    text = raw.get("text")
    if text in (None, "") and value not in (None, "") and num is None:
        text = str(value)  # e.g. "Yes" put in value
    out["text"] = str(text) if text not in (None, "") else None
    comps = raw.get("components") or {}
    if isinstance(comps, dict):
        out["components"] = {k: v for k, v in comps.items() if v not in (None, "")}
    inputs = raw.get("inputs") or {}
    if isinstance(inputs, dict):
        clean = {}
        for k, v in inputs.items():
            if isinstance(v, dict):
                n = to_number(v.get("value"))
                if n is not None:
                    clean[k] = {"value": n, "unit": v.get("unit")}
            else:
                n = to_number(v)
                if n is not None:
                    clean[k] = {"value": n, "unit": None}
        out["inputs"] = clean
    return out


def _slot_score(kpi: dict, slot: dict) -> int:
    """3 = value in the required unit, 2 = value in another unit / text / components, 1 = inputs only."""
    has_value = slot["value"] is not None
    if has_value:
        kind = kpi["unit_kind"]
        if kind == "pct" and _is_pct_unit(slot["unit"]):
            return 3
        if kind in INTENSITY_KINDS:
            conv, _ = INTENSITY_KINDS[kind]
            num, den = _split_ratio_unit(slot["unit"] or "")
            if conv(num) == 1.0 and _per_crore_factor(den) == 1.0:
                return 3
            return 2
        return 3
    if slot["components"] or slot["text"]:
        return 2
    if slot["inputs"]:
        return 1
    return 0


# ---------------------------------------------------------------------
# Merge: many chunks -> one raw entry per KPI
# ---------------------------------------------------------------------
def merge_chunk_kpis(per_chunk: list[list[dict]]) -> dict:
    """
    per_chunk: list (one per chunk) of raw LLM kpi lists.
    Returns {kpi_id: {"current": slot, "previous": slot, "source_context", "pages"}}.
    Per year, the highest-scoring slot wins (ties: first seen); components and
    inputs are then filled field-by-field from the other candidates.
    """
    candidates = {kid: {y: [] for y in YEARS} for kid in KPI_IDS}

    for chunk_items in per_chunk:
        for item in chunk_items or []:
            if not isinstance(item, dict):
                continue
            kid = str(item.get("kpi_id") or "").strip().upper()
            if kid not in KPI_BY_ID:
                continue
            pages = item.get("pages") or []
            if isinstance(pages, (int, str)):
                pages = [pages]
            page_nums = [int(p) for p in pages if to_number(p) is not None]
            for y in YEARS:
                slot = _clean_year(item.get(y))
                if _slot_score(KPI_BY_ID[kid], slot) > 0:
                    candidates[kid][y].append((slot, item.get("source_context"), page_nums))

    merged = {}
    for kid in KPI_IDS:
        kpi = KPI_BY_ID[kid]
        entry = {"source_context": None, "pages": []}
        for y in YEARS:
            cands = candidates[kid][y]
            if not cands:
                entry[y] = _empty_year()
                continue
            best_idx = max(range(len(cands)), key=lambda i: (_slot_score(kpi, cands[i][0]), -i))
            best, ctx, pages = cands[best_idx]
            best = {**best, "components": dict(best["components"]), "inputs": dict(best["inputs"])}
            for other, _, _ in cands:
                for k, v in other["components"].items():
                    best["components"].setdefault(k, v)
                for k, v in other["inputs"].items():
                    best["inputs"].setdefault(k, v)
                if not best["fy"] and other["fy"]:
                    best["fy"] = other["fy"]
            entry[y] = best
            if ctx and not entry["source_context"]:
                entry["source_context"] = ctx
            for p in pages:
                if p not in entry["pages"]:
                    entry["pages"].append(p)
        entry["pages"].sort()
        merged[kid] = entry
    return merged


# ---------------------------------------------------------------------
# Normalize: convert units, compute derived values, fill one record per catalog KPI
# ---------------------------------------------------------------------
def _input(slot, name):
    v = slot["inputs"].get(name)
    return (v["value"], v.get("unit")) if v else (None, None)


def _num_component(slot, name):
    return to_number(slot["components"].get(name))


def _ratio_pct(a, b):
    if a is None or not b:
        return None
    return a / b * 100.0


def _revenue_crore(f1_slot):
    rev = _num_component(f1_slot, "revenue")
    if rev is None:
        return None
    factor = _money_to_crore(f1_slot.get("unit") or "")
    return rev * factor if factor is not None else None


def _normalize_slot(kpi, slot, revenue_cr, notes):
    """
    Mutates slot into its normalized form. Returns one of
    'reported' | 'derived' | 'reported_unit_mismatch' | 'not_disclosed'.
    notes: list collecting derivation strings for this KPI.
    """
    kind = kpi["unit_kind"]
    kid = kpi["id"]
    tag = None  # set to 'derived' when we convert or compute

    def done(status):
        slot.pop("inputs", None)
        return status

    # ---- money: convert value and money components to Rs. crore ----
    if kind == "money":
        has_any = slot["value"] is not None or any(
            to_number(v) is not None for k, v in slot["components"].items() if not k.endswith("_pct")
        )
        if has_any:
            factor = _money_to_crore(slot["unit"] or "")
            if factor is None:
                status = "reported_unit_mismatch"
            else:
                if factor != 1.0:
                    if slot["value"] is not None:
                        slot["value"] *= factor
                    for k, v in list(slot["components"].items()):
                        n = to_number(v)
                        if n is not None and not k.endswith("_pct"):
                            slot["components"][k] = n * factor
                    notes.append(f"converted from {slot['unit']} to Rs. crore (×{factor:g})")
                    tag = "derived"
                slot["unit"] = "Rs. crore"
                status = tag or "reported"
            if kid == "F2" and slot["components"].get("green_pct") is None:
                g = _num_component(slot, "green_capex")
                t = _num_component(slot, "total_capex")
                pct = _ratio_pct(g, t)
                if pct is not None:
                    slot["components"]["green_pct"] = round(pct, 2)
                    notes.append(f"green % = {_fmt(g)} / {_fmt(t)} × 100 = {pct:.2f}%")
                    if status == "reported":
                        status = "derived"
            return done(status)
        if slot["text"]:
            return done("reported")
        return done("not_disclosed")

    # ---- percentages ----
    if kind == "pct":
        if slot["value"] is not None:
            if _is_pct_unit(slot["unit"]):
                slot["unit"] = "%"
                return done("reported")
            u = _clean_unit(slot["unit"])
            if ("ratio" in u or "fraction" in u) and 0 <= slot["value"] <= 1:
                notes.append(f"{slot['value']} (fraction) × 100")
                slot["value"] *= 100
                slot["unit"] = "%"
                return done("derived")
            return done("reported_unit_mismatch")
        if kid == "S6":
            fp = _num_component(slot, "female_pct")
            mp = _num_component(slot, "male_pct")
            if fp is None and mp is None:
                fc, _ = _input(slot, "female_count")
                mc, _ = _input(slot, "male_count")
                if fc is not None and mc is not None and (fc + mc):
                    fp, mp = fc / (fc + mc) * 100, mc / (fc + mc) * 100
                    slot["components"].update({"female_pct": round(fp, 2), "male_pct": round(mp, 2)})
                    notes.append(f"female % = {_fmt(fc)} / ({_fmt(fc)} + {_fmt(mc)}) × 100 = {fp:.2f}%")
                    slot["unit"] = "%"
                    return done("derived")
                return done("not_disclosed")
            if fp is not None and mp is None:
                slot["components"]["male_pct"] = round(100 - fp, 2)
            if mp is not None and fp is None:
                slot["components"]["female_pct"] = round(100 - mp, 2)
            slot["unit"] = "%"
            return done("reported")
        pairs = {
            "E1": ("renewable_energy", "total_energy"),
            "E7": ("water_recycled", "water_consumption"),
            "E9": ("waste_recovered", "total_waste"),
            "S9": ("employees_trained", "total_employees"),
            "G1": ("women_directors", "board_size"),
            "G2": ("women_kmp", "total_kmp"),
        }
        if kid in pairs:
            a_name, b_name = pairs[kid]
            a, au = _input(slot, a_name)
            b, bu = _input(slot, b_name)
            if a is not None and b and _same_unit(au, bu):
                pct = a / b * 100
                slot["value"], slot["unit"] = round(pct, 2), "%"
                notes.append(f"{a_name} {_fmt(a)} / {b_name} {_fmt(b)} × 100 = {pct:.2f}%")
                return done("derived")
        if slot["text"]:
            return done("reported")
        return done("not_disclosed")

    # ---- intensities ----
    if kind in INTENSITY_KINDS:
        conv, target = INTENSITY_KINDS[kind]
        if slot["value"] is not None:
            num, den = _split_ratio_unit(slot["unit"] or "")
            nf = conv(num) if num else None
            df = _per_crore_factor(den)
            if nf is None or df is None:
                return done("reported_unit_mismatch")
            if nf == 1.0 and df == 1.0:
                slot["unit"] = target
                return done("reported")
            orig = slot["value"]
            slot["value"] = orig * nf * df
            notes.append(f"{_fmt(orig)} {slot['unit']} × {nf:g} × {df:g} = {_fmt(slot['value'])} {target}")
            slot["unit"] = target
            return done("derived")
        # compute absolute / revenue
        names = kpi.get("inputs") or []
        total, parts = 0.0, []
        for name in names:
            v, u = _input(slot, name)
            if v is None:
                total = None
                break
            f = conv(_clean_unit(u)) if u else (1.0 if kind != "air_int" else None)
            if f is None:
                total = None
                break
            total += v * f
            parts.append(f"{name} {_fmt(v)} {u or ''}".strip())
        if total is not None and names and revenue_cr:
            slot["value"] = total / revenue_cr
            slot["unit"] = target
            notes.append(f"({' + '.join(parts)}) / revenue {_fmt(revenue_cr)} Rs. crore = {_fmt(slot['value'])} {target}")
            return done("derived")
        if slot["text"]:
            return done("reported")
        return done("not_disclosed")

    # ---- absolute emissions ----
    if kind == "emission_abs":
        if slot["value"] is not None:
            f = _emission_to_t(_clean_unit(slot["unit"])) if slot["unit"] else 1.0
            if f is None:
                return done("reported_unit_mismatch")
            if f != 1.0:
                notes.append(f"{_fmt(slot['value'])} {slot['unit']} × {f:g} = tCO2e")
                slot["value"] *= f
                slot["unit"] = "tCO2e"
                return done("derived")
            slot["unit"] = "tCO2e"
            return done("reported")
        return done("reported" if slot["text"] else "not_disclosed")

    # ---- LTIFR ----
    if kind == "rate_pmh":
        if slot["value"] is None:
            return done("reported" if slot["text"] else "not_disclosed")
        u = _clean_unit(slot["unit"]).replace(",", "").replace(" ", "")
        if not u or "million" in u or "1000000" in u or "10lakh" in u:
            slot["unit"] = "per million hours worked"
            return done("reported")
        for marker, f in (("200000", 5.0), ("2lakh", 5.0), ("100000", 10.0), ("1lakh", 10.0), ("lakh", 10.0)):
            if marker in u:
                notes.append(f"{_fmt(slot['value'])} {slot['unit']} × {f:g} = per million hours")
                slot["value"] *= f
                slot["unit"] = "per million hours worked"
                return done("derived")
        return done("reported_unit_mismatch")

    # ---- ratios S7 / S8 ----
    if kind == "ratio":
        if slot["value"] is not None:
            return done("reported")
        a_name, b_name = kpi["inputs"]
        a, _ = _input(slot, a_name)
        b, _ = _input(slot, b_name)
        if a is not None and b:
            slot["value"] = round(a / b, 4)
            notes.append(f"{a_name} {_fmt(a)} / {b_name} {_fmt(b)} = {slot['value']}")
            return done("derived")
        return done("reported" if slot["text"] else "not_disclosed")

    # ---- counts ----
    if kind == "count":
        status = None
        if kid == "S1" and slot["value"] is None:
            e, _ = _input(slot, "employees")
            w, _ = _input(slot, "workers")
            if e is not None or w is not None:
                slot["value"] = (e or 0) + (w or 0)
                notes.append(f"employees {_fmt(e)} + workers {_fmt(w)} = {_fmt(slot['value'])}")
                slot["components"].setdefault("employees", e)
                slot["components"].setdefault("workers", w)
                status = "derived"
        if kid == "G3" and slot["components"].get("pct") is None:
            c = _num_component(slot, "count")
            if c is None and slot["value"] is not None:
                c = slot["value"]
                slot["components"]["count"] = c
            b, _ = _input(slot, "board_size")
            if c is not None and b:
                pct = c / b * 100
                slot["components"]["pct"] = round(pct, 2)
                notes.append(f"{_fmt(c)} independent / {_fmt(b)} directors × 100 = {pct:.2f}%")
                status = "derived"
        if status:
            return done(status)
        if slot["value"] is not None or slot["components"] or slot["text"]:
            return done("reported")
        return done("not_disclosed")

    # ---- text / categorical ----
    if slot["value"] is not None or slot["components"] or slot["text"]:
        return done("reported")
    return done("not_disclosed")


def _same_unit(a, b) -> bool:
    """Both None/empty, or the same after cleanup. Different units -> don't divide."""
    ca, cb = _clean_unit(a), _clean_unit(b)
    return (not ca and not cb) or ca == cb or not ca or not cb


_STATUS_RANK = {"not_disclosed": 0, "reported_unit_mismatch": 1, "derived": 2, "reported": 3}


def normalize_kpis(merged: dict, company_name: str = "Unknown") -> list[dict]:
    """Always returns exactly one record per catalog KPI, in catalog order."""
    merged = merged or {}
    # F1 first: its revenue is the denominator for every intensity KPI.
    order = ["F1"] + [k for k in KPI_IDS if k != "F1"]
    records = {}
    revenue = {}
    for kid in order:
        kpi = KPI_BY_ID[kid]
        raw = merged.get(kid) or {}
        notes = []
        year_status = {}
        slots = {}
        for y in YEARS:
            slot = dict(raw.get(y) or _empty_year())
            slot["components"] = dict(slot.get("components") or {})
            slot["inputs"] = dict(slot.get("inputs") or {})
            for key in ("fy", "value", "unit", "text"):
                slot.setdefault(key, None)
            year_notes = []
            year_status[y] = _normalize_slot(kpi, slot, revenue.get(y), year_notes)
            notes.extend(f"{y}: {n}" for n in year_notes)
            slots[y] = slot
        if kid == "F1":
            for y in YEARS:
                revenue[y] = _revenue_crore(slots[y])
        records[kid] = {
            "kpi_id": kid,
            "kpi_name": kpi["name"],
            "category": kpi["category"],
            "required_unit": kpi["required_unit"],
            "current": slots["current"],
            "previous": slots["previous"],
            "_year_status": year_status,
            "_notes": notes,
            "source_context": raw.get("source_context"),
            "pages": list(raw.get("pages") or []),
            "company": company_name,
        }

    # S13 from S12 components when S13 itself wasn't printed.
    s12, s13 = records["S12"], records["S13"]
    for y in YEARS:
        if s13["_year_status"][y] == "not_disclosed":
            b = _num_component(s12[y], "budgeted")
            sp = _num_component(s12[y], "spent")
            pct = _ratio_pct(sp, b)
            if pct is not None:
                s13[y]["value"], s13[y]["unit"] = round(pct, 2), "%"
                s13[y]["fy"] = s13[y]["fy"] or s12[y]["fy"]
                s13["_year_status"][y] = "derived"
                s13["_notes"].append(f"{y}: spent {_fmt(sp)} / budgeted {_fmt(b)} × 100 = {pct:.2f}% (from S12)")
                if not s13["pages"]:
                    s13["pages"] = list(s12["pages"])

    out = []
    for kid in KPI_IDS:
        rec = records[kid]
        ys = rec.pop("_year_status")
        notes = rec.pop("_notes")
        statuses = [ys[y] for y in YEARS if ys[y] != "not_disclosed"]
        if not statuses:
            status = "not_disclosed"
        elif "reported_unit_mismatch" in statuses:
            status = "reported_unit_mismatch"
        elif "derived" in statuses:
            status = "derived"
        else:
            status = "reported"
        rec["status"] = status
        rec["year_status"] = ys
        rec["derivation"] = "; ".join(notes) if notes else None
        out.append(rec)
    return out


# ---------------------------------------------------------------------
# Display helpers shared by the UI, exports and prompts
# ---------------------------------------------------------------------
def is_disclosed(record: dict) -> bool:
    return record.get("status") not in (None, "not_disclosed")


def format_year(slot: dict | None) -> str:
    """Human-readable cell for one year: value, components and/or text."""
    if not slot:
        return ""
    bits = []
    if slot.get("value") is not None:
        bits.append(_fmt(slot["value"]))
    comps = slot.get("components") or {}
    if comps:
        def show(k, v):
            n = to_number(v)
            if n is None:
                return str(v)
            if "year" in k and n == int(n):
                return str(int(n))  # 2040, not 2,040
            return _fmt(n)
        bits.append(" / ".join(f"{k.replace('_', ' ')} {show(k, v)}" for k, v in comps.items() if v is not None))
    if slot.get("text"):
        bits.append(str(slot["text"]))
    return " · ".join(b for b in bits if b)


def headline_value(slot: dict | None):
    """A single number for charts / deltas: value, else the first numeric component."""
    if not slot:
        return None
    if slot.get("value") is not None:
        return slot["value"]
    for v in (slot.get("components") or {}).values():
        n = to_number(v)
        if n is not None:
            return n
    return None


def yoy_change(record: dict):
    cur, prev = headline_value(record.get("current")), headline_value(record.get("previous"))
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev) * 100


def coverage(records: list[dict]) -> dict:
    disclosed = [r for r in records if is_disclosed(r)]
    by_cat = {c: sum(1 for r in disclosed if r["category"] == c) for c in CATEGORIES}
    totals = {c: sum(1 for k in KPI_CATALOG if k["category"] == c) for c in CATEGORIES}
    return {
        "disclosed": len(disclosed),
        "derived": sum(1 for r in records if r.get("status") == "derived"),
        "total": len(KPI_CATALOG),
        "by_category": by_cat,
        "category_totals": totals,
        "missing": [r for r in records if not is_disclosed(r)],
    }


def compact_for_prompt(records: list[dict]) -> list[dict]:
    """Small JSON for chat/summary prompts."""
    rows = []
    for r in records:
        rows.append({
            "company": r.get("company"),
            "id": r["kpi_id"], "kpi": r["kpi_name"], "category": r["category"],
            "unit": r["current"].get("unit") or r["required_unit"],
            "current_fy": r["current"].get("fy"), "current": format_year(r["current"]) or None,
            "previous_fy": r["previous"].get("fy"), "previous": format_year(r["previous"]) or None,
            "status": r["status"], "derivation": r.get("derivation"), "pages": r.get("pages"),
        })
    return rows


def report_ready(report: dict) -> bool:
    """A saved report counts as extracted when at least one KPI was disclosed."""
    return any(is_disclosed(r) for r in (report or {}).get("kpis") or [])


def is_legacy_report(report: dict) -> bool:
    """Saved before the fixed-KPI schema: has free-form metrics but no KPI records."""
    return bool((report or {}).get("metrics")) and not (report or {}).get("kpis")
