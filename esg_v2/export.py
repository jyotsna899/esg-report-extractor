"""
Long-format export in the column template defined by config/output_columns.csv:
one row per company x financial year x KPI.

Available fields for output_columns.csv:
    company, fy, kpi_id, kpi_name, category, value, unit, type, pages, derivation,
    quote, status, flags, components, source_file
"""

import io

import pandas as pd

from .config import load_output_columns

STATUS_TEXT = {
    "reported": "Reported",
    "derived": "Derived",
    "reported_unit_mismatch": "Reported (unit not standardised)",
    "not_disclosed": "Not disclosed",
}


def _year_derivation(derivation, year):
    if not derivation:
        return ""
    parts = [p.strip() for p in derivation.split(" | ")]
    mine = [p.split(": ", 1)[1] for p in parts if p.startswith(f"{year}: ")]
    return " | ".join(mine) if mine else derivation


def _components_text(comps):
    if not comps:
        return ""
    return "; ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in comps.items())


def long_rows(report: dict, include_previous: bool = True, include_undisclosed: bool = True) -> list[dict]:
    rows = []
    company = report.get("name", "")
    fy_labels = report.get("fy_labels") or {}
    years = ("current", "previous") if include_previous else ("current",)
    for rec in report.get("kpis") or []:
        for year in years:
            slot = rec.get(year)
            if not slot and (year == "previous" or not include_undisclosed):
                continue
            slot = slot or {}
            value = slot.get("value")
            if value is None and slot.get("text"):
                value = slot["text"]
            flags = rec.get("flags") or []
            other_year = "previous:" if year == "current" else "current:"
            year_flags = [f for f in flags if other_year not in f]
            rows.append({
                "company": company,
                "fy": slot.get("fy") or fy_labels.get(year, ""),
                "kpi_id": rec.get("kpi_id"),
                "kpi_name": rec.get("kpi_name"),
                "category": rec.get("category"),
                "value": value if value is not None else "",
                "unit": "" if (slot.get("unit") or rec.get("required_unit")) == "text"
                        else (slot.get("unit") or rec.get("required_unit") or ""),
                "type": rec.get("kpi_type") or "",
                "pages": ", ".join(str(p) for p in rec.get("pages") or []),
                "derivation": _year_derivation(rec.get("derivation"), year),
                "quote": rec.get("source_context") or "",
                "status": STATUS_TEXT.get(rec.get("status") if slot else "not_disclosed", rec.get("status")),
                "flags": "; ".join(year_flags),
                "components": _components_text(slot.get("components")),
                "source_file": report.get("source_file", ""),
            })
    return rows


def template_frame(reports: list[dict], **kwargs) -> pd.DataFrame:
    columns = load_output_columns()
    rows = [r for rep in reports for r in long_rows(rep, **kwargs)]
    data = [{header: row.get(field, "") for header, field in columns} for row in rows]
    return pd.DataFrame(data, columns=[h for h, _ in columns])


def datapoint_frame(reports: list[dict]) -> pd.DataFrame:
    rows = []
    for rep in reports:
        for d in (rep.get("datapoints") or {}).values():
            base = {"Company": rep.get("name"), "Datapoint": d["id"], "Name": d["name"],
                    "Group": d["group"], "Page": d.get("page"), "Quote": d.get("quote") or "",
                    "Note": d.get("note") or "", "Flags": "; ".join(d.get("flags") or [])}
            if d.get("kind") == "text":
                rows.append({**base, "Year": "current", "Printed": d.get("text") or "", "Printed unit": "",
                             "Standardised": "", "Std unit": ""})
                continue
            for y in ("current", "previous"):
                s = (d.get("years") or {}).get(y) or {}
                rows.append({**base, "Year": (rep.get("fy_labels") or {}).get(y, y),
                             "Printed": s.get("raw", "") if s else "", "Printed unit": s.get("unit", ""),
                             "Standardised": s.get("norm_value", ""), "Std unit": s.get("norm_unit", "")})
    return pd.DataFrame(rows)


def to_csv_bytes(reports: list[dict], **kwargs) -> bytes:
    return template_frame(reports, **kwargs).to_csv(index=False).encode("utf-8-sig")


def to_xlsx_bytes(reports: list[dict], **kwargs) -> bytes:
    buf = io.BytesIO()
    main = template_frame(reports, **kwargs)
    audit = datapoint_frame(reports)
    flags_col = next((h for h, f in load_output_columns() if f == "flags"), None)
    review = main[main[flags_col].astype(str).str.len() > 0] if flags_col and not main.empty else main.iloc[0:0]
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        main.to_excel(xw, sheet_name="KPI Output", index=False)
        review.to_excel(xw, sheet_name="Needs Review", index=False)
        audit.to_excel(xw, sheet_name="Raw Datapoints", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col[:200] if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 60)
    return buf.getvalue()
