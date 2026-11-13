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
     "inputs": ["renewable_energy", "total_energy", "non_renewable_energy"]},
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
     "required_unit": "rate per million hours worked", "unit_kind": "rate_pmh", "direction": "lower",
     "inputs": ["injuries", "hours_worked"]},
    {"id": "S4", "name": "Employee Well-being Investment", "category": "Social",
     "required_unit": "% of total payroll/revenue as reported", "unit_kind": "pct", "direction": "higher",
     "inputs": ["wellbeing_spend", "payroll"]},
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
     "components": ["revenue", "ebitda", "pat", "revenue_standalone", "revenue_consolidated", "boundary"]},
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
    "renewable_energy": "energy from renewable sources (value + unit, e.g. GJ or MWh)",
    "non_renewable_energy": "energy from non-renewable sources (value + unit, e.g. GJ)",
    "total_energy": "total energy consumption including renewable and non-renewable (value + unit, e.g. GJ)",
    "scope1": "Scope 1 emissions (value + unit, e.g. tCO2e)",
    "scope2": "Scope 2 emissions (value + unit, e.g. tCO2e)",
    "sox": "SOx emissions (value + unit, e.g. MT or kg)",
    "nox": "NOx emissions (value + unit, e.g. MT or kg)",
    "water_consumption": "total water consumption (value + unit, e.g. kL or ML)",
    "water_recycled": "water recycled/reused (value + unit, e.g. kL)",
    "total_waste": "total waste generated (value + unit, e.g. metric tonnes)",
    "waste_recovered": "waste recycled + re-used + recovered (value + unit, e.g. metric tonnes)",
    "injuries": "number of lost time injuries",
    "hours_worked": "total person-hours worked during the financial year",
    "wellbeing_spend": "amount spent on employee well-being (Rs. crore or Lakh)",
    "payroll": "total employee benefit expense / payroll (Rs. crore)",
    "employees": "number of permanent + other employees",
    "workers": "number of permanent + other workers",
    "female_count": "number of female employees + workers",
    "male_count": "number of male employees + workers",
    "highest_paid": "remuneration of the highest-paid person (Rs. crore or INR)",
    "median_remuneration": "median remuneration of employees (Rs. crore or INR)",
    "median_female_pay": "median remuneration of female employees",
    "median_male_pay": "median remuneration of male employees",
    "employees_trained": "number of employees who received skill/safety training",
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
    return {"fy": None, "value": None, "unit": None, "components": {}, "text": None, "inputs": {}, "pages": []}


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
    pages = raw.get("pages")
    if isinstance(pages, list):
        out["pages"] = [int(p) for p in pages if to_number(p) is not None]
    elif to_number(pages) is not None:
        out["pages"] = [int(to_number(pages))]
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
                raw_slot = item.get(y)
                slot = _clean_year(raw_slot)
                slot_pages = list(slot.get("pages") or [])
                if not slot_pages and page_nums:
                    if len(page_nums) == 1:
                        slot_pages = list(page_nums)
                    elif len(page_nums) == 2:
                        slot_pages = [page_nums[0]] if y == "previous" else [page_nums[1]]
                    else:
                        slot_pages = list(page_nums)
                slot["pages"] = slot_pages
                if _slot_score(KPI_BY_ID[kid], slot) > 0:
                    candidates[kid][y].append((slot, item.get("source_context"), slot_pages))

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
            best = {**best, "components": dict(best["components"]), "inputs": dict(best["inputs"]), "pages": list(pages or best.get("pages") or [])}
            for other, _, other_pages in cands:
                for k, v in other["components"].items():
                    best["components"].setdefault(k, v)
                for k, v in other["inputs"].items():
                    best["inputs"].setdefault(k, v)
                if not best["fy"] and other["fy"]:
                    best["fy"] = other["fy"]
                for p in (other_pages or other.get("pages") or []):
                    if p not in best["pages"]:
                        best["pages"].append(p)
            best["pages"].sort()
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


def _revenue_crore(f1_slot, boundary: str | None = None):
    if not f1_slot:
        return None
    factor = _money_to_crore(f1_slot.get("unit") or "") or 1.0
    comps = f1_slot.get("components") or {}
    b = str(boundary or comps.get("boundary") or f1_slot.get("text") or "").lower()

    rev_std = _num_component(f1_slot, "revenue_standalone")
    rev_con = _num_component(f1_slot, "revenue_consolidated")
    rev_gen = _num_component(f1_slot, "revenue")
    if rev_gen is None and f1_slot.get("value") is not None:
        rev_gen = to_number(f1_slot["value"])

    if "consolidat" in b:
        rev = rev_con if rev_con is not None else (rev_gen if rev_gen is not None else rev_std)
    elif "standalone" in b:
        rev = rev_std if rev_std is not None else (rev_gen if rev_gen is not None else rev_con)
    else:
        rev = rev_gen if rev_gen is not None else (rev_con if rev_con is not None else rev_std)

    return rev * factor if rev is not None else None


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
        # E1: Renewable Energy Share %
        if kid == "E1":
            ren_val, ren_u = _input(slot, "renewable_energy")
            tot_val, tot_u = _input(slot, "total_energy")
            non_val, non_u = _input(slot, "non_renewable_energy")
            if ren_val is not None and tot_val is None and non_val is not None:
                tot_val = ren_val + non_val
                tot_u = ren_u
            if ren_val is not None and tot_val and _same_unit(ren_u, tot_u):
                derived_pct = round((ren_val / tot_val) * 100.0, 2)
                orig_val = slot["value"]
                # If reported value is missing, differs by > 2%, or is 100% when ren < tot, use derived
                if orig_val is None or (orig_val == 100.0 and derived_pct < 99.5) or abs(orig_val - derived_pct) > 2.0:
                    slot["value"] = derived_pct
                    slot["unit"] = "%"
                    notes.append(f"renewable {_fmt(ren_val)} / total {_fmt(tot_val)} × 100 = {derived_pct:.2f}%")
                    return done("derived")
                else:
                    slot["unit"] = "%"
                    return done("reported")

        # S4: Employee Well-being Investment %
        if kid == "S4":
            spend, su = _input(slot, "wellbeing_spend")
            pay, pu = _input(slot, "payroll")
            if spend is not None and pay:
                sf = _money_to_crore(su or "") or 1.0
                pf = _money_to_crore(pu or "") or 1.0
                pct = round(((spend * sf) / (pay * pf)) * 100.0, 2)
                slot["value"] = pct
                slot["unit"] = "%"
                notes.append(f"well-being spend {_fmt(spend)} / payroll {_fmt(pay)} × 100 = {pct:.2f}%")
                return done("derived")

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
        # First priority: compute from raw physical inputs and revenue_cr
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
            derived_int = round(total / revenue_cr, 4)
            orig_val = slot["value"]
            orig_u = _clean_unit(slot.get("unit"))
            # If reported value is missing, not in standard target unit, or differs by > 5%, use derived
            if orig_val is None or target.lower() not in orig_u:
                slot["value"] = derived_int
                slot["unit"] = target
                notes.append(f"({' + '.join(parts)}) / revenue {_fmt(revenue_cr)} Rs. crore = {_fmt(slot['value'])} {target}")
                return done("derived")
            elif abs(orig_val - derived_int) / max(derived_int, 1.0) > 0.05:
                slot["value"] = derived_int
                slot["unit"] = target
                notes.append(f"derived: ({' + '.join(parts)}) / revenue {_fmt(revenue_cr)} Rs. crore = {_fmt(slot['value'])} {target} (replaces reported {_fmt(orig_val)})")
                return done("derived")
            else:
                slot["unit"] = target
                return done("reported")

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
        inj, _ = _input(slot, "injuries")
        hrs, _ = _input(slot, "hours_worked")
        if inj is not None and hrs:
            ltifr = round((inj * 1_000_000.0) / hrs, 4)
            slot["value"] = ltifr
            slot["unit"] = "per million hours worked"
            notes.append(f"injuries {_fmt(inj)} × 1,000,000 / hours {_fmt(hrs)} = {slot['value']} per million hours")
            return done("derived")
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
        if kid == "S2":
            emp = _num_component(slot, "employees")
            wrk = _num_component(slot, "workers")
            if emp is None and wrk is None:
                emp, _ = _input(slot, "employees")
                wrk, _ = _input(slot, "workers")
            if emp is not None or wrk is not None:
                tot = (emp or 0) + (wrk or 0)
                slot["value"] = int(tot) if float(tot).is_integer() else tot
                slot["components"].setdefault("employees", emp if emp is not None else 0)
                slot["components"].setdefault("workers", wrk if wrk is not None else 0)
                status = "reported"
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


def normalize_kpis(merged: dict, company_name: str = "Unknown", reporting_boundary: str | None = None) -> list[dict]:
    """Always returns exactly one record per catalog KPI, in catalog order."""
    merged = merged or {}
    f1_raw = merged.get("F1") or {}
    detected_boundary = reporting_boundary
    if not detected_boundary:
        for y in YEARS:
            comps = (f1_raw.get(y) or {}).get("components") or {}
            b = comps.get("boundary")
            if b and str(b).lower() in ("standalone", "consolidated"):
                detected_boundary = str(b).lower()
                break
            txt = str((f1_raw.get(y) or {}).get("text") or "").lower()
            if "consolidat" in txt:
                detected_boundary = "consolidated"
                break
            elif "standalone" in txt:
                detected_boundary = "standalone"
                break
    detected_boundary = detected_boundary or "standalone"

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
            slot["pages"] = list(slot.get("pages") or raw.get("pages") or [])
            for key in ("fy", "value", "unit", "text"):
                slot.setdefault(key, None)
            year_notes = []
            year_status[y] = _normalize_slot(kpi, slot, revenue.get(y), year_notes)
            notes.extend(f"{y}: {n}" for n in year_notes)
            slots[y] = slot
        if kid == "F1":
            for y in YEARS:
                revenue[y] = _revenue_crore(slots[y], boundary=detected_boundary)
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
            "reporting_boundary": detected_boundary,
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
                s13[y]["pages"] = list(s12[y].get("pages") or s12["pages"])
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
        years_map = {}
        raw_rec = merged.get(kid) or {}
        if raw_rec.get("years") and isinstance(raw_rec["years"], dict):
            for k, v in raw_rec["years"].items():
                std_k = standardize_fy(k)
                if std_k and _slot_has_data(v):
                    v_dict = dict(v)
                    v_dict.setdefault("pages", list(raw_rec.get("pages") or []))
                    years_map[std_k] = v_dict
        rec["years"] = years_map
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


# ---------------------------------------------------------------------
# Multi-year formatting & Excel/CSV export helpers
# ---------------------------------------------------------------------
def _slot_has_data(slot: dict | None) -> bool:
    """Returns True if the slot contains an actual value, component, or qualitative text."""
    if not slot or not isinstance(slot, dict):
        return False
    if slot.get("value") is not None:
        return True
    comps = slot.get("components") or {}
    if any(v is not None and v != "" for v in comps.values()):
        return True
    if slot.get("text"):
        return True
    return False


def standardize_fy(raw: str | None) -> str:
    """
    Standardizes financial and calendar year strings into clean 'FY20XX' format.
    Handles 'FY 2024-25', '2024-25', 'FY25', 'Jan 24 - Dec 24', 'CY 2024', '2024'.
    Returns '' for non-year strings so ghost columns are never created.
    """
    if not raw:
        return ""
    s = str(raw).strip()

    # 1. Calendar year range like 'Jan 24 - Dec 24', 'Jan 2024 - Dec 2024', 'Jan 24 – Dec 24'
    m = re.search(r"(?:jan|dec)[a-z]*\s*(\d{2,4})\s*[-/–—]\s*(?:jan|dec)[a-z]*\s*(\d{2,4})", s, re.IGNORECASE)
    if m:
        yr = m.group(2)
        return f"FY20{yr}" if len(yr) == 2 else f"FY{yr}"

    # 2. Indian FY range: 'FY 2024-25', '2024-25', 'FY 24-25', '2024-2025'
    m = re.search(r"(?:FY\s*)?(\d{2,4})\s*[-/–—]\s*(\d{2,4})", s, re.IGNORECASE)
    if m:
        if not re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", s, re.IGNORECASE):
            end_yr = m.group(2)
            return f"FY20{end_yr}" if len(end_yr) == 2 else f"FY{end_yr}"

    # 3. Calendar Year like 'CY 2024' or 'CY24'
    m = re.search(r"CY\s*(\d{2,4})", s, re.IGNORECASE)
    if m:
        yr = m.group(1)
        return f"FY20{yr}" if len(yr) == 2 else f"FY{yr}"

    # 4. Standard FY like 'FY 2025' or 'FY25'
    m = re.search(r"FY\s*(\d{2,4})", s, re.IGNORECASE)
    if m:
        yr = m.group(1)
        return f"FY20{yr}" if len(yr) == 2 else f"FY{yr}"

    # 5. Standalone 4-digit year like '2025'
    m = re.search(r"\b(20\d\d)\b", s)
    if m:
        return f"FY{m.group(1)}"

    # 6. Fallback: if already starts with FY and followed by 4 digits
    clean = re.sub(r"\s+", "", s).upper()
    if re.match(r"^FY20\d\d$", clean):
        return clean

    return ""


def extract_report_dominant_years(report: dict) -> tuple[str, str]:
    """Infers the primary current and previous FY for a report from its disclosed KPIs."""
    cur_counts = {}
    prev_counts = {}
    for r in (report or {}).get("kpis") or []:
        c_fy = standardize_fy((r.get("current") or {}).get("fy"))
        p_fy = standardize_fy((r.get("previous") or {}).get("fy"))
        if c_fy and re.match(r"^FY20\d\d$", c_fy):
            cur_counts[c_fy] = cur_counts.get(c_fy, 0) + 1
        if p_fy and re.match(r"^FY20\d\d$", p_fy):
            prev_counts[p_fy] = prev_counts.get(p_fy, 0) + 1
    dom_cur = max(cur_counts, key=cur_counts.get) if cur_counts else "FY2025"
    dom_prev = max(prev_counts, key=prev_counts.get) if prev_counts else "FY2024"
    return dom_cur, dom_prev


def get_kpi_slot_for_fy(record: dict, target_fy: str, report: dict | None = None):
    """Finds the data slot (value, unit, components, etc.) for a specific financial year."""
    if not record:
        return None
    target_std = standardize_fy(target_fy)
    if not target_std:
        return None

    cur_slot = record.get("current") or {}
    prev_slot = record.get("previous") or {}
    cur_fy = standardize_fy(cur_slot.get("fy"))
    prev_fy = standardize_fy(prev_slot.get("fy"))

    # 1. Match against report dominant current and previous financial years
    if report:
        dom_cur, dom_prev = extract_report_dominant_years(report)
        if target_std == dom_cur and _slot_has_data(cur_slot):
            return cur_slot
        if target_std == dom_prev and _slot_has_data(prev_slot):
            return prev_slot
        # In a single report, cur_slot is dom_cur and prev_slot is dom_prev.
        # Additional historical years come from record["years"] if merged from multiple reports.
        years_dict = record.get("years") or {}
        for y_key, slot in years_dict.items():
            if standardize_fy(y_key) == target_std and _slot_has_data(slot):
                return slot
        return None

    # 2. Check explicit matching FY on the slots when report is not provided
    if cur_fy == target_std and _slot_has_data(cur_slot):
        return cur_slot
    if prev_fy == target_std and _slot_has_data(prev_slot):
        return prev_slot

    # 3. Check explicit 'years' dictionary if present (from merged multi-report uploads)
    years_dict = record.get("years") or {}
    for y_key, slot in years_dict.items():
        if standardize_fy(y_key) == target_std and _slot_has_data(slot):
            return slot

    return None


def extract_numeric_kpi_value(record: dict, slot: dict | None) -> float | int | str:
    """
    Extracts strictly the numeric metric value for a KPI slot.
    Returns int or float if a numeric value is present.
    If no numeric metric value is found, returns 'Not Found'.
    """
    if not slot or not isinstance(slot, dict):
        return "Not Found"

    kid = (record.get("kpi_id") or "").strip().upper()
    kpi_meta = KPI_BY_ID.get(kid, {})
    unit_kind = kpi_meta.get("unit_kind")

    # 1. Direct numeric value in slot
    if slot.get("value") is not None:
        n = to_number(slot["value"])
        if n is not None:
            if float(n).is_integer():
                return int(n)
            return round(float(n), 4)

    comps = slot.get("components") or {}

    # 2. Key KPI component resolution
    if kid == "S1":
        # Workforce Headcount: employees + workers
        e = to_number(comps.get("employees"))
        w = to_number(comps.get("workers"))
        if e is not None or w is not None:
            tot = (e or 0) + (w or 0)
            return int(tot) if float(tot).is_integer() else tot

    elif kid == "S2":
        # Workplace Fatalities: employees + workers (can be 0)
        e = to_number(comps.get("employees"))
        w = to_number(comps.get("workers"))
        if e is not None or w is not None:
            tot = (e or 0) + (w or 0)
            return int(tot) if float(tot).is_integer() else tot

    elif kid == "S6":
        # Gender Diversity: female_pct
        fp = to_number(comps.get("female_pct"))
        if fp is not None:
            return int(fp) if float(fp).is_integer() else round(float(fp), 4)

    elif kid == "S10":
        # POSH Complaints: filed count (fallback resolved)
        f = to_number(comps.get("filed"))
        if f is not None:
            return int(f) if float(f).is_integer() else f
        res = to_number(comps.get("resolved"))
        if res is not None:
            return int(res) if float(res).is_integer() else res

    elif kid == "S11":
        # Customer Complaints: received count (fallback resolved)
        rec = to_number(comps.get("received"))
        if rec is not None:
            return int(rec) if float(rec).is_integer() else rec
        res = to_number(comps.get("resolved"))
        if res is not None:
            return int(res) if float(res).is_integer() else res

    elif kid == "S12":
        # CSR Budget: spent (Rs. crore) or budgeted
        sp = to_number(comps.get("spent"))
        if sp is not None:
            return int(sp) if float(sp).is_integer() else round(float(sp), 4)
        bg = to_number(comps.get("budgeted"))
        if bg is not None:
            return int(bg) if float(bg).is_integer() else round(float(bg), 4)

    elif kid == "G3":
        # Independent Directors: count or pct
        c = to_number(comps.get("count"))
        if c is not None:
            return int(c) if float(c).is_integer() else c
        pct = to_number(comps.get("pct"))
        if pct is not None:
            return int(pct) if float(pct).is_integer() else round(float(pct), 4)

    elif kid == "F1":
        # Revenue
        rev = to_number(comps.get("revenue"))
        if rev is not None:
            return int(rev) if float(rev).is_integer() else round(float(rev), 4)

    elif kid == "F2":
        # Green Capex
        gc = to_number(comps.get("green_capex"))
        if gc is not None:
            return int(gc) if float(gc).is_integer() else round(float(gc), 4)
        gp = to_number(comps.get("green_pct"))
        if gp is not None:
            return int(gp) if float(gp).is_integer() else round(float(gp), 4)

    # 3. Qualitative / Text KPIs have no numeric metric value
    if unit_kind == "text":
        return "Not Found"

    # 4. Check any numeric component for non-text KPIs
    for v in comps.values():
        n = to_number(v)
        if n is not None:
            if float(n).is_integer():
                return int(n)
            return round(float(n), 4)

    # 5. Check inputs if slot had inputs
    inputs = slot.get("inputs") or {}
    for inp in inputs.values():
        if isinstance(inp, dict) and inp.get("value") is not None:
            n = to_number(inp["value"])
            if n is not None:
                if float(n).is_integer():
                    return int(n)
                return round(float(n), 4)

    return "Not Found"


def get_kpi_value_for_fy(record: dict, target_fy: str, report: dict | None = None, numeric_only: bool = True) -> float | int | str:
    """Returns numeric metric value for a specific financial year, or 'Not Found' if unavailable."""
    slot = get_kpi_slot_for_fy(record, target_fy, report)
    if not slot:
        return "Not Found"
    if numeric_only:
        return extract_numeric_kpi_value(record, slot)
    val = format_year(slot)
    return val if val else "Not Found"


def get_kpi_page_citation(record: dict, years: list[str], report: dict | None = None) -> str:
    """
    Builds the trailing page verification citation string across the displayed financial years,
    formatted as: 'FY24 data page 60, FY25 data page 70'.
    Only cites pages for years that have valid numeric metric data.
    If no data is found for any year, returns 'Not Found'.
    """
    if not record:
        return "Not Found"

    active_year_slots = []
    for fy in years:
        val = get_kpi_value_for_fy(record, fy, report)
        if val != "Not Found":
            slot = get_kpi_slot_for_fy(record, fy, report)
            active_year_slots.append((fy, slot))

    if not active_year_slots:
        return "Not Found"

    rec_pages = list(record.get("pages") or [])
    citations = []

    for idx, (fy, slot) in enumerate(active_year_slots):
        m = re.search(r"(\d\d)$", fy)
        short_fy = f"FY{m.group(1)}" if m else fy

        pages = []
        if slot and slot.get("pages"):
            pages = [p for p in slot["pages"] if p is not None]
        elif rec_pages:
            if len(rec_pages) == 1:
                pages = rec_pages
            elif len(rec_pages) == len(active_year_slots):
                pages = [rec_pages[idx]]
            else:
                pages = rec_pages

        if pages:
            unique_pages = sorted(set(int(p) for p in pages if to_number(p) is not None))
            if unique_pages:
                page_str = ", ".join(str(p) for p in unique_pages)
                citations.append(f"{short_fy} data page {page_str}")

    return ", ".join(citations) if citations else ""


def company_multi_year_frame(report: dict, years: list[str] | None = None):
    """
    Returns a DataFrame in the format:
    S. No. | Unit | Env./Social/Gov. | KPI | FY2024 | FY2025 | FY2026 ... | Source Page(s)
    """
    import pandas as pd
    kpis = (report or {}).get("kpis") or []

    # Collect valid financial years
    found_years = set()
    dom_cur, dom_prev = extract_report_dominant_years(report)
    if dom_cur and re.match(r"^FY20\d\d$", dom_cur):
        found_years.add(dom_cur)
    if dom_prev and re.match(r"^FY20\d\d$", dom_prev):
        found_years.add(dom_prev)

    for r in kpis:
        for slot in (r.get("current"), r.get("previous")):
            if slot and _slot_has_data(slot):
                std = standardize_fy(slot.get("fy"))
                if std and re.match(r"^FY20\d\d$", std):
                    found_years.add(std)
        for y_key, slot in (r.get("years") or {}).items():
            if _slot_has_data(slot):
                std = standardize_fy(y_key)
                if std and re.match(r"^FY20\d\d$", std):
                    found_years.add(std)

    if years:
        all_years = sorted(set(standardize_fy(y) or y for y in years if standardize_fy(y) or y))
    else:
        base = {"FY2024", "FY2025", "FY2026"}
        all_years = sorted(base.union(found_years))

    rows = []
    for r in kpis:
        cur_unit = (r.get("current") or {}).get("unit")
        prev_unit = (r.get("previous") or {}).get("unit")
        unit = cur_unit or prev_unit or r.get("required_unit") or ""
        row = {
            "S. No.": r["kpi_id"],
            "Unit": unit,
            "Env./Social/Gov.": r["category"],
            "KPI": r["kpi_name"],
        }
        for fy in all_years:
            row[fy] = get_kpi_value_for_fy(r, fy, report)
        row["Source Page(s)"] = get_kpi_page_citation(r, all_years, report)
        rows.append(row)
    return pd.DataFrame(rows)


def comparative_analysis_frame(reports: list[dict], target_fy: str = "FY2026"):
    """
    Returns a DataFrame in the format:
    S. No. | KPI Name | Unit | Company 1 | Company 2 | Company 3 ...
    """
    import pandas as pd
    target_fy_std = standardize_fy(target_fy) or target_fy
    ready = [r for r in reports if report_ready(r)]
    by_company = []
    for rep in ready:
        c_name = rep.get("name", "Unknown")
        by_id = {r["kpi_id"]: r for r in rep.get("kpis", [])}
        by_company.append((c_name, rep, by_id))

    rows = []
    for kpi in KPI_CATALOG:
        kid = kpi["id"]
        row = {
            "S. No.": kid,
            "KPI Name": kpi["name"],
            "Unit": kpi["required_unit"],
        }
        for c_name, rep, by_id in by_company:
            rec = by_id.get(kid)
            if not rec:
                row[c_name] = "Not Found"
            else:
                row[c_name] = get_kpi_value_for_fy(rec, target_fy_std, rep)
        rows.append(row)
    return pd.DataFrame(rows)


def consolidated_multi_year_frame(reports: list[dict], years: list[str] | None = None):
    """Returns a consolidated multi-year DataFrame across all ready companies."""
    import pandas as pd
    frames = []
    ready = [r for r in reports if report_ready(r)]
    for rep in ready:
        df = company_multi_year_frame(rep, years=years)
        df.insert(0, "Company", rep.get("name", "Unknown"))
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def export_excel_workbook(reports: list[dict], target_fy: str = "FY2026") -> bytes:
    """
    Exports a multi-sheet Excel workbook (.xlsx):
      - Sheet 1: Comparative Analysis for target_fy
      - Sheet 2: Consolidated Multi-Year table for all companies
      - Subsequent Sheets: Per-company multi-year breakdown
    """
    import io
    import pandas as pd

    target_fy_std = standardize_fy(target_fy) or target_fy
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        comp_df = comparative_analysis_frame(reports, target_fy=target_fy_std)
        comp_sheet = f"Comparative_{target_fy_std}"[:31]
        comp_df.to_excel(writer, sheet_name=comp_sheet, index=False)

        all_multi = consolidated_multi_year_frame(reports)
        if not all_multi.empty:
            all_multi.to_excel(writer, sheet_name="All_Companies_MultiYear"[:31], index=False)

        used_sheets = {comp_sheet.lower(), "all_companies_multiyear"}
        for rep in reports:
            if not report_ready(rep):
                continue
            c_name = rep.get("name", "Company")
            c_df = company_multi_year_frame(rep)
            base_sheet = re.sub(r"[^\w\s-]", "", c_name).strip()[:28] or "Company"
            sheet_title = base_sheet
            suffix = 2
            while sheet_title.lower() in used_sheets:
                sheet_title = f"{base_sheet[:25]}_{suffix}"
                suffix += 1
            used_sheets.add(sheet_title.lower())
            c_df.to_excel(writer, sheet_name=sheet_title, index=False)

    return output.getvalue()
