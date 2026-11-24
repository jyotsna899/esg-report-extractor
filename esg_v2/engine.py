"""
End-to-end extraction for one PDF. Returns a report dict that the existing
Streamlit app understands (same KPI record shape as kpis.py), plus an audit
trail of every raw datapoint.
"""

import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import GROUP_ORDER, load_datapoints, load_kpis
from .formulas import evaluate, evaluate_with_alternatives, names_in
from .llm import build_prompt, gemini_extract, model_name, response_schema
from .page_finder import select_pages
from .pdf_pages import is_mostly_scanned, read_page_texts, subset_pdf_bytes
from .units import CANONICAL_UNIT, intensity_to_per_crore, normalize, parse_number
from .validate import ground_value, sanity_checks

YEARS = ("current", "previous")
SCANNED_PAGE_CAP = 250

# KPI -> (reported-intensity datapoint, numerator kind, factor from canonical-per-crore to KPI unit)
INTENSITY_CROSSCHECK = {
    "E2": ("energy_intensity_reported", "energy", 1000.0),  # GJ/cr -> MJ/cr
    "E3": ("ghg_intensity_reported", "emission", 1.0),
    "E6": ("water_intensity_reported", "water", 1.0),
    "E8": ("waste_intensity_reported", "waste_mass", 1.0),
}


def _standardize_fy(raw):
    try:
        from kpis import standardize_fy

        return standardize_fy(raw)
    except Exception:
        return (raw or "").strip()


def _printed_number(raw):
    """The figure exactly as printed (no multiplier words applied) - used for grounding."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"\d[\d,]*(?:\.\d+)?|\.\d+", str(raw))
    return float(m.group(0).replace(",", "")) if m else None


def _fmt(v):
    if v is None:
        return "-"
    if abs(v) >= 1000:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


# ---------------------------------------------------------------------
# Step A: one LLM call per group
# ---------------------------------------------------------------------
def _plan_pages(page_texts, groups, scanned):
    if scanned:
        all_pages = sorted(page_texts)[:SCANNED_PAGE_CAP]
        return {g: all_pages for g in groups}
    return {g: select_pages(page_texts, dps, g) for g, dps in groups.items()}


def _parse_group_response(resp, group_dps, page_texts, pages, debug):
    """Turns one model response into {dp_id: result} with units normalized and grounding checked."""
    by_id = {dp.id: dp for dp in group_dps}
    results = {}
    items = (resp or {}).get("datapoints") or []
    for item in items:
        dp = by_id.get(str(item.get("id")))
        if dp is None or dp.id in results:
            continue
        found = bool(item.get("found"))
        page = item.get("page")
        try:
            page = int(page) if page is not None else None
        except (TypeError, ValueError):
            page = None
        res = {
            "id": dp.id, "name": dp.name, "group": dp.group, "kind": dp.kind,
            "found": found, "years": {}, "text": None, "page": page,
            "quote": item.get("quote"), "note": item.get("note"), "flags": [],
        }
        if not found:
            results[dp.id] = res
            continue
        if dp.kind == "text":
            res["text"] = (item.get("text") or "").strip() or None
            res["found"] = bool(res["text"])
            results[dp.id] = res
            continue

        for year in YEARS:
            raw = item.get(f"{year}_raw")
            value = parse_number(raw)
            if value is None and item.get(f"{year}_value") is not None and raw in (None, ""):
                value = parse_number(item.get(f"{year}_value"))
            unit = (item.get(f"{year}_unit") or item.get("current_unit") or "").strip()
            if value is None:
                res["years"][year] = None
                continue
            norm_value, norm_unit, problem = normalize(value, unit, dp.kind)
            slot = {"raw": raw, "value": value, "unit": unit, "norm_value": norm_value,
                    "norm_unit": norm_unit, "problem": problem}
            res["years"][year] = slot
            if problem:
                res["flags"].append(f"{year}: {problem}")
            new_page, gflag = ground_value(_printed_number(raw if raw not in (None, "") else value),
                                           page, page_texts, pages)
            if gflag == "page_corrected":
                # the figure was verified on another selected page: fix the page silently
                if year == "current" or not res["page"]:
                    res["page"] = new_page
            elif gflag:
                res["flags"].append(f"{year}: {gflag.replace('_', ' ')}")
        res["found"] = any(res["years"].get(y) for y in YEARS)
        results[dp.id] = res

    for dp in group_dps:  # model skipped it entirely
        if dp.id not in results:
            results[dp.id] = {"id": dp.id, "name": dp.name, "group": dp.group, "kind": dp.kind,
                              "found": False, "years": {}, "text": None, "page": None,
                              "quote": None, "note": None, "flags": []}
    return results


# ---------------------------------------------------------------------
# Step B: KPIs from datapoints
# ---------------------------------------------------------------------
def _env_for_year(dps, year):
    env = {}
    for dp_id, d in dps.items():
        slot = (d.get("years") or {}).get(year)
        if slot and slot.get("norm_value") is not None:
            env[dp_id] = slot["norm_value"]
    return env


def _describe_input(name, dps, year, env):
    d = dps.get(name)
    if not d:
        return f"{name} = {_fmt(env.get(name))}"
    slot = (d.get("years") or {}).get(year) or {}
    page = f", p.{d['page']}" if d.get("page") else ""
    conv = ""
    if slot.get("unit") and slot.get("norm_unit") and slot.get("value") != slot.get("norm_value"):
        conv = f" [printed {slot.get('raw') or _fmt(slot.get('value'))} {slot['unit']}]"
    unit = f" {slot.get('norm_unit')}" if slot.get("norm_unit") else ""
    return f"{name} = {_fmt(slot.get('norm_value'))}{unit}{conv}{page}"


def compute_kpis(dps, fy_labels, company_name):
    kpis_cfg = load_kpis()
    helpers = {k.kpi_id: k for k in kpis_cfg if k.type.lower() == "helper"}
    envs = {y: _env_for_year(dps, y) for y in YEARS}
    helper_trace = {y: {} for y in YEARS}
    for y in YEARS:
        for hid, h in helpers.items():
            value, expr, used = evaluate_with_alternatives(h.formula, envs[y])
            if value is not None:
                envs[y][hid] = value
                helper_trace[y][hid] = (expr, used)

    def expand(names, y):
        """Replace helper names by the datapoints they were built from."""
        out = []
        for n in names:
            if n in helper_trace[y]:
                out.extend(expand(helper_trace[y][n][1], y))
            elif n not in out:
                out.append(n)
        return out

    records = []
    for k in kpis_cfg:
        if k.type.lower() == "helper":
            continue
        rec = {
            "kpi_id": k.kpi_id, "kpi_name": k.name, "category": k.category,
            "required_unit": k.output_unit, "kpi_type": k.type, "direction": k.direction,
            "current": None, "previous": None, "status": "not_disclosed",
            "derivation": None, "source_context": None, "pages": [], "flags": [],
            "company": company_name,
        }
        alt_names = names_in(k.formula.split("||")[0]) if k.formula else []
        is_text = len(alt_names) == 1 and dps.get(alt_names[0], {}).get("kind") == "text"

        if is_text:
            d = dps.get(alt_names[0], {})
            if d.get("text"):
                rec["current"] = {"fy": fy_labels["current"], "value": None, "unit": "", "components": {},
                                  "text": d["text"]}
                rec["status"] = "reported"
                rec["pages"] = [d["page"]] if d.get("page") else []
                rec["source_context"] = d.get("quote")
                rec["flags"] = list(d.get("flags") or [])
                if d.get("note"):
                    rec["flags"].append(f"note: {d['note']}")
            records.append(rec)
            continue

        derivations, used_all = [], []
        for y in YEARS:
            value, expr, used = evaluate_with_alternatives(k.formula, envs[y]) if k.formula else (None, None, [])
            comps = {}
            for cname, cexpr in k.components:
                cnames = names_in(cexpr)
                if len(cnames) == 1 and dps.get(cnames[0], {}).get("kind") == "text":
                    if y == "current" and dps[cnames[0]].get("text"):
                        comps[cname] = dps[cnames[0]]["text"]
                    continue
                try:
                    comps[cname] = round(evaluate(cexpr, envs[y]), 6)
                except Exception:
                    pass
            if value is None and not comps:
                continue
            base = expand(used, y) if value is not None else []
            used_all.extend(n for n in base if n not in used_all)
            rec[y] = {"fy": fy_labels[y], "value": None if value is None else round(value, 6),
                      "unit": k.output_unit, "components": comps, "text": None}
            if value is not None:
                inputs = "; ".join(_describe_input(n, dps, y, envs[y]) for n in base)
                if k.type.lower() == "derived" or len(base) > 1 or expr != (base[0] if base else expr):
                    derivations.append(f"{y}: {expr} = {_fmt(value)} ({inputs})")
                else:
                    derivations.append(f"{y}: {inputs}")

        # Unit-mismatch fallback for direct KPIs: keep the printed figure
        if rec["current"] is None and rec["previous"] is None and len(alt_names) == 1:
            d = dps.get(alt_names[0])
            if d:
                for y in YEARS:
                    slot = (d.get("years") or {}).get(y)
                    if slot and slot.get("value") is not None and slot.get("norm_value") is None:
                        rec[y] = {"fy": fy_labels[y], "value": slot["value"], "unit": slot["unit"],
                                  "components": {}, "text": None}
                        rec["status"] = "reported_unit_mismatch"
                        used_all = [alt_names[0]]
                        derivations.append(f"{y}: kept as printed ({slot.get('problem')})")

        # Intensity cross-check / fallback against the company's own printed intensity
        if k.kpi_id in INTENSITY_CROSSCHECK:
            rep_id, num_kind, factor = INTENSITY_CROSSCHECK[k.kpi_id]
            rep = dps.get(rep_id) or {}
            for y in YEARS:
                slot = (rep.get("years") or {}).get(y)
                if not slot:
                    continue
                conv = intensity_to_per_crore(slot["value"], slot["unit"], num_kind)
                if conv is None:
                    continue
                conv *= factor
                ours = (rec.get(y) or {}).get("value")
                if ours is None:
                    rec[y] = {"fy": fy_labels[y], "value": round(conv, 6), "unit": k.output_unit,
                              "components": {}, "text": None}
                    derivations.append(f"{y}: converted from company-reported intensity "
                                       f"{slot.get('raw')} {slot['unit']} = {_fmt(conv)} {k.output_unit}"
                                       + (f" (p.{rep['page']})" if rep.get("page") else ""))
                    if rep_id not in used_all:
                        used_all.append(rep_id)
                elif conv > 0 and abs(ours - conv) / conv > 0.25:
                    rec["flags"].append(f"{y}: computed {_fmt(ours)} vs company-reported {_fmt(conv)} "
                                        f"{k.output_unit} - check revenue/units")

        if rec["current"] or rec["previous"]:
            if rec["status"] == "not_disclosed":
                rec["status"] = "derived" if k.type.lower() == "derived" else "reported"
            rec["derivation"] = " | ".join(derivations) or None
            pages, quotes = [], []
            for n in used_all:
                d = dps.get(n) or {}
                if d.get("page") and d["page"] not in pages:
                    pages.append(d["page"])
                if d.get("quote"):
                    quotes.append(f"{n}: {d['quote']}")
                rec["flags"].extend(f"{n} {f}" for f in d.get("flags") or [])
                if d.get("note"):
                    rec["flags"].append(f"{n} note: {d['note']}")
            rec["pages"] = sorted(pages)
            rec["source_context"] = " | ".join(quotes) or None
            rec["source_by_input"] = {n: (dps.get(n) or {}).get("page") for n in used_all}
        records.append(rec)
    return records


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------
def extract_report(pdf_path, company_name, on_status=None, llm_fn=None):
    def status(fraction, text):
        if on_status:
            on_status(max(0.0, min(fraction, 1.0)), text)

    status(0.01, "Reading pages")
    page_texts = read_page_texts(pdf_path, lambda c, t: status(0.15 * c / max(t, 1), f"Reading {c}/{t}"))
    scanned = is_mostly_scanned(page_texts)
    debug_log = []
    if scanned:
        debug_log.append({"chunk": 0, "status": "info",
                          "detail": "PDF looks scanned/image-based: sending all pages; numbers can't be "
                                    "verified against text and will be flagged."})

    all_dps = load_datapoints()
    groups = {g: [dp for dp in all_dps.values() if dp.group == g] for g in GROUP_ORDER}
    groups = {g: v for g, v in groups.items() if v}
    plan = _plan_pages(page_texts, groups, scanned)

    llm = llm_fn or (lambda pdf_bytes, prompt, schema: gemini_extract(
        pdf_bytes, prompt, schema,
        on_wait=lambda s, a: status(0.15, f"Model busy - retrying in {s:.0f}s (attempt {a})")))

    # PDFium is not thread-safe: build every page-subset PDF here, in one thread,
    # and only run the network calls in parallel.
    subsets = {g: subset_pdf_bytes(pdf_path, plan[g]) for g in groups if plan[g]}

    def run_group(group):
        if group not in subsets:
            return group, None, "no relevant pages found"
        pages = plan[group]
        return group, llm(subsets[group], build_prompt(groups[group], pages), response_schema(groups[group])), None

    responses, done = {}, 0
    workers = max(1, int(os.environ.get("ESG_PARALLEL", "3")))
    status(0.16, f"Extracting {len(groups)} sections with {model_name()}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_group, g): g for g in groups}
        for fut in as_completed(futures):
            g = futures[fut]
            try:
                _, resp, err = fut.result()
            except Exception as exc:
                resp, err = None, f"{type(exc).__name__}: {exc}"
            responses[g] = resp
            done += 1
            debug_log.append({"chunk": g, "status": "error" if err else "ok",
                              "detail": err or f"pages {plan[g]}"})
            status(0.16 + 0.78 * done / len(groups), f"Section '{g}' done ({done}/{len(groups)})")

    dps, fy_cur, fy_prev = {}, Counter(), Counter()
    for g, dps_list in groups.items():
        resp = responses.get(g)
        dps.update(_parse_group_response(resp, dps_list, page_texts, plan[g], debug_log))
        if resp:
            if resp.get("fy_current"):
                fy_cur[_standardize_fy(resp["fy_current"])] += 1
            if resp.get("fy_previous"):
                fy_prev[_standardize_fy(resp["fy_previous"])] += 1
    fy_labels = {"current": fy_cur.most_common(1)[0][0] if fy_cur else "",
                 "previous": fy_prev.most_common(1)[0][0] if fy_prev else ""}

    sanity_checks(dps)
    status(0.96, "Computing KPIs")
    records = compute_kpis(dps, fy_labels, company_name)
    page_map = {p: t for p, t in page_texts.items() if t}
    full_text = "\n".join(f"\n--- Page {p} ---\n{t}" for p, t in page_map.items())
    status(1.0, "Done")
    return {
        "name": company_name,
        "kpis": records,
        "datapoints": dps,
        "fy_labels": fy_labels,
        "page_plan": plan,
        "engine": f"v2 ({model_name()})",
        "topic_index": [],
        "page_map": page_map,
        "full_text": full_text,
        "chat_history": [],
        "summary": "",
        "debug_log": debug_log,
    }
