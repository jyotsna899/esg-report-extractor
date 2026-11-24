"""Tests for the v2 engine. The LLM is replaced by a fake, so no API key is needed."""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from esg_v2.config import load_datapoints, load_kpis, load_output_columns  # noqa: E402
from esg_v2.formulas import evaluate_with_alternatives  # noqa: E402
from esg_v2.units import intensity_to_per_crore, normalize, parse_number  # noqa: E402
from esg_v2.validate import ground_value  # noqa: E402


# ---------------------------------------------------------------- config
def test_config_is_consistent():
    dps = load_datapoints()
    kpis = load_kpis()
    exported = [k for k in kpis if k.type.lower() != "helper"]
    assert len(exported) == 39
    known = set(dps) | {k.kpi_id for k in kpis if k.type.lower() == "helper"}
    from esg_v2.formulas import names_in

    for k in kpis:
        for alt in k.formula.split("||"):
            for name in names_in(alt.strip()):
                assert name in known, f"{k.kpi_id} formula uses unknown name {name}"
        for _, expr in k.components:
            for name in names_in(expr):
                assert name in known, f"{k.kpi_id} component uses unknown name {name}"
    assert load_output_columns()[0] == ("Company", "company")


# ---------------------------------------------------------------- units
@pytest.mark.parametrize("raw,expected", [
    ("1,23,456.78", 123456.78), ("(12)", -12), ("Nil", 0), ("245:1", 245), ("-", None),
    ("18.4%", 18.4), ("1.2 lakh", 120000), (None, None), (3, 3.0),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize("value,unit,kind,expected", [
    (1, "Rs. in crore", "money", 1), (5e9, "Rs.", "money", 500), (250, "₹ lakh", "money", 2.5),
    (3, "INR Million", "money", 0.3), (10, "MTCO2e", "emission", 10), (2, "MT", "air_mass", 2000),
    (5, "MWh", "energy", 18), (5, "Lakh GJ", "energy", 500000), (7, "ML", "water", 7000),
    (7, "Kilolitres", "water", 7), (1, "kg", "waste_mass", 0.001), (1, "Mt CO2e", "emission", 1e6),
])
def test_normalize(value, unit, kind, expected):
    v, _, problem = normalize(value, unit, kind)
    assert problem is None
    assert v == pytest.approx(expected)


def test_normalize_refuses_unknown_unit():
    v, _, problem = normalize(5, "barrels", "energy")
    assert v is None and problem


def test_intensity_conversion():
    assert intensity_to_per_crore(0.0000123, "GJ per rupee of turnover", "energy") == pytest.approx(123)
    assert intensity_to_per_crore(1, "GJ per rupee of turnover adjusted for PPP", "energy") is None


# ---------------------------------------------------------------- formulas
def test_formula_alternatives():
    f = "renewable_energy / total_energy * 100 || renewable_energy / (renewable_energy + non_renewable_energy) * 100"
    v, expr, used = evaluate_with_alternatives(f, {"renewable_energy": 25, "non_renewable_energy": 75})
    assert v == pytest.approx(25) and "non_renewable_energy" in used
    v, _, missing = evaluate_with_alternatives("a / b", {"a": 1})
    assert v is None and missing == ["b"]
    v, _, missing = evaluate_with_alternatives("a / b", {"a": 1, "b": 0})
    assert v is None


def test_formula_rejects_code():
    with pytest.raises(ValueError):
        evaluate_with_alternatives("__import__('os')", {})


# ---------------------------------------------------------------- grounding
def test_grounding():
    pages = {10: "Total energy consumption 1,23,456 GJ", 11: "Scope 1 4,567"}
    assert ground_value(123456, 10, pages, [10, 11]) == (10, None)
    assert ground_value(4567, 10, pages, [10, 11]) == (11, "page_corrected")
    assert ground_value(999999, 10, pages, [10, 11]) == (10, "not_found_in_text")
    assert ground_value(3, 10, pages, [10, 11]) == (10, None)  # tiny integers are not checked


# ---------------------------------------------------------------- end to end
PAGES = {
    3: ["SECTION A: GENERAL DISCLOSURES", "Details of the listed entity", "Turnover (in Rs.) 50,000,000,000",
        "Reporting boundary: standalone basis", "Employees and workers (including differently abled)",
        "TOTAL EMPLOYEES (D+E) 10,000 7,000 3,000", "TOTAL WORKERS (F+G) 2,000 1,800 200",
        "Participation/Inclusion/Representation of women", "Board of Directors 10 3",
        "Key Management Personnel 4 1"],
    20: ["SECTION B: MANAGEMENT AND PROCESS DISCLOSURES",
         "Details of the specified committee of the board responsible for decision making on sustainability",
         "CSR & ESG Committee of the Board"],
    40: ["PRINCIPLE 6", "Details of total energy consumption (in Joules or multiples) and energy intensity",
         "FY 2024-25 FY 2023-24", "Total energy consumed from renewable sources (A+B+C) 25,000 20,000",
         "Total energy consumed from non-renewable sources (D+E+F) 75,000 80,000",
         "Total energy consumption (A+B+C+D+E+F) 1,00,000 1,00,000",
         "Energy intensity per rupee of turnover 0.0000020 0.0000021"],
    41: ["PRINCIPLE 6 greenhouse gas emissions", "Total Scope 1 emissions tCO2e 1,500 1,600",
         "Total Scope 2 emissions tCO2e 3,500 3,400", "Total volume of water consumption (in kilolitres) 2,50,000"],
}


def _make_pdf(path, total_pages=50):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for p in range(1, total_pages + 1):
        lines = PAGES.get(p, [f"Annual report narrative page {p}. Our values and vision."])
        y = 800
        for line in lines:
            c.drawString(40, y, line)
            y -= 18
        c.showPage()
    c.save()


def _fake_llm(pdf_bytes, prompt, schema):
    ids = schema["properties"]["datapoints"]["items"]["properties"]["id"]["enum"]
    assert pdf_bytes[:4] == b"%PDF"
    answers = {
        "turnover_brsr": dict(current_raw="50,000,000,000", current_unit="Rs.", page=3, quote="Turnover (in Rs.)"),
        "employees_total": dict(current_raw="10,000", current_unit="", page=3),
        "female_employees": dict(current_raw="3,000", page=3),
        "male_employees": dict(current_raw="7,000", page=3),
        "workers_total": dict(current_raw="2,000", page=3),
        "female_workers": dict(current_raw="200", page=3),
        "board_size": dict(current_raw="10", page=3),
        "women_directors": dict(current_raw="3", page=3),
        "renewable_energy": dict(current_raw="25,000", current_unit="GJ", previous_raw="20,000",
                                 previous_unit="GJ", page=40),
        "non_renewable_energy": dict(current_raw="75,000", current_unit="GJ", page=40),
        "total_energy": dict(current_raw="1,00,000", current_unit="GJ", previous_raw="1,00,000",
                             previous_unit="GJ", page=41),  # wrong page on purpose -> corrected
        "energy_intensity_reported": dict(current_raw="0.0000020", current_unit="GJ per rupee of turnover", page=40),
        "scope1": dict(current_raw="1,500", current_unit="tCO2e", page=41),
        "scope2": dict(current_raw="3,500", current_unit="tCO2e", page=41),
        "water_consumption": dict(current_raw="2,50,000", current_unit="kL", page=41),
        "esg_committee": dict(text="Yes - CSR & ESG Committee", page=20),
        "scope3": dict(current_raw="88,888", current_unit="tCO2e", page=41),  # hallucinated -> flagged
    }
    out = []
    for i in ids:
        if i in answers:
            out.append({"id": i, "found": True, **answers[i]})
        else:
            out.append({"id": i, "found": False})
    return {"fy_current": "FY 2024-25", "fy_previous": "FY 2023-24", "datapoints": out}


@pytest.fixture()
def report(tmp_path):
    pytest.importorskip("reportlab")
    from esg_v2.engine import extract_report

    pdf = tmp_path / "Acme_BRSR_FY25.pdf"
    _make_pdf(pdf)
    return extract_report(str(pdf), "Acme", llm_fn=_fake_llm)


def _rec(report, kid):
    return next(r for r in report["kpis"] if r["kpi_id"] == kid)


def test_end_to_end_values(report):
    assert len(report["kpis"]) == 39
    assert report["fy_labels"]["current"]
    assert _rec(report, "F1")["current"]["value"] == pytest.approx(5000)       # Rs. -> crore
    assert _rec(report, "E1")["current"]["value"] == pytest.approx(25)
    assert _rec(report, "E1")["previous"]["value"] == pytest.approx(20)
    assert _rec(report, "E2")["current"]["value"] == pytest.approx(20000)      # 1e5 GJ*1000/5000 cr
    assert _rec(report, "E3")["current"]["value"] == pytest.approx(1.0)
    assert _rec(report, "S1")["current"]["value"] == 12000
    assert _rec(report, "S6")["current"]["value"] == pytest.approx(3200 / 12000 * 100)
    assert _rec(report, "G1")["current"]["value"] == pytest.approx(30)
    assert _rec(report, "G5")["current"]["text"].startswith("Yes")
    assert _rec(report, "E1")["status"] == "derived"
    assert _rec(report, "S7")["status"] == "not_disclosed"


def test_end_to_end_audit_trail(report):
    e2 = _rec(report, "E2")
    assert 40 in e2["pages"] and 3 in e2["pages"]            # energy page (corrected) + turnover page
    assert "total_energy" in e2["derivation"] and "REVENUE" in e2["derivation"]
    assert not any("company-reported" in f for f in e2["flags"])  # 0.000002 GJ/Rs = 20,000 MJ/cr: matches
    assert any("not found in text" in f for f in _rec(report, "E4")["flags"])


def test_export(report):
    from esg_v2.export import template_frame, to_xlsx_bytes

    df = template_frame([report])
    assert list(df.columns)[:4] == ["Company", "Financial Year", "KPI ID", "KPI"]
    e1 = df[(df["KPI ID"] == "E1") & (df["Financial Year"] == report["fy_labels"]["current"])]
    assert float(e1["Value"].iloc[0]) == pytest.approx(25)
    import pandas as pd

    book = pd.read_excel(io.BytesIO(to_xlsx_bytes([report])), sheet_name=None)
    assert {"KPI Output", "Needs Review", "Raw Datapoints"} <= set(book)
