import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kpis import (  # noqa: E402
    KPI_CATALOG,
    build_kpi_prompt_block,
    coverage,
    format_year,
    merge_chunk_kpis,
    normalize_kpis,
    report_ready,
    yoy_change,
)


def run(*chunks):
    records = normalize_kpis(merge_chunk_kpis(list(chunks)), "Acme")
    return {r["kpi_id"]: r for r in records}, records


F1 = {"kpi_id": "F1",
      "current": {"fy": "FY25", "unit": "Rs. crore", "components": {"revenue": 10000, "ebitda": 2000, "pat": 900}},
      "previous": {"fy": "FY24", "unit": "Rs. crore", "components": {"revenue": 8000, "ebitda": 1500, "pat": 700}}}


def test_catalog_matches_requested_list():
    ids = [k["id"] for k in KPI_CATALOG]
    assert len(ids) == len(set(ids)) == 39
    assert ids[:11] == [f"E{i}" for i in range(1, 12)]
    assert [i for i in ids if i.startswith("S")] == [f"S{i}" for i in range(1, 17)]
    assert [i for i in ids if i.startswith("G")] == [f"G{i}" for i in range(1, 11)]
    assert [i for i in ids if i.startswith("F")] == ["F1", "F2"]
    block = build_kpi_prompt_block()
    for kid in ids:
        assert f"{kid} |" in block


def test_one_record_per_kpi_even_when_empty():
    by_id, records = run()
    assert len(records) == len(KPI_CATALOG)
    assert all(r["status"] == "not_disclosed" for r in records)
    assert not report_ready({"kpis": records})
    assert coverage(records)["disclosed"] == 0


def test_stated_in_required_unit_is_reported():
    by_id, _ = run([{"kpi_id": "E1", "current": {"fy": "FY25", "value": 42.5, "unit": "%"},
                     "previous": {"fy": "FY24", "value": 40, "unit": "%"}}])
    e1 = by_id["E1"]
    assert e1["status"] == "reported"
    assert e1["current"]["value"] == 42.5 and e1["previous"]["value"] == 40
    assert yoy_change(e1) == pytest.approx(6.25)


def test_per_rupee_intensity_converted_to_mj_per_crore():
    by_id, _ = run([{"kpi_id": "E2", "current": {"value": 0.0000123, "unit": "GJ per rupee of turnover"}}])
    e2 = by_id["E2"]
    assert e2["status"] == "derived"
    # 0.0000123 GJ/Rs × 1000 MJ/GJ × 1e7 Rs/crore
    assert e2["current"]["value"] == pytest.approx(123000)
    assert e2["current"]["unit"] == "MJ per Rs. crore"
    assert "1e+07" in e2["derivation"] or "1e7" in e2["derivation"]


def test_ppp_adjusted_intensity_is_unit_mismatch():
    by_id, _ = run([{"kpi_id": "E3", "current": {"value": 0.5, "unit": "tCO2e per rupee of turnover adjusted for PPP"}}])
    assert by_id["E3"]["status"] == "reported_unit_mismatch"
    assert by_id["E3"]["current"]["value"] == 0.5  # kept as printed


def test_intensity_from_absolute_over_revenue():
    by_id, _ = run([F1, {"kpi_id": "E3",
                         "current": {"inputs": {"scope1": {"value": 30000, "unit": "tCO2e"},
                                                "scope2": {"value": 20000, "unit": "tCO2e"}}},
                         "previous": {"inputs": {"scope1": {"value": 32000, "unit": "tCO2e"},
                                                 "scope2": {"value": 24000, "unit": "tCO2e"}}}}])
    e3 = by_id["E3"]
    assert e3["status"] == "derived"
    assert e3["current"]["value"] == pytest.approx(5.0)   # 50000 / 10000
    assert e3["previous"]["value"] == pytest.approx(7.0)  # 56000 / 8000


def test_air_emissions_sum_tonnes_to_kg():
    by_id, _ = run([F1, {"kpi_id": "E5", "current": {"inputs": {"sox": {"value": 10, "unit": "MT"},
                                                                 "nox": {"value": 5, "unit": "tonnes"}}}}])
    assert by_id["E5"]["current"]["value"] == pytest.approx(1.5)  # 15000 kg / 10000 cr


def test_water_megalitres_to_kl():
    by_id, _ = run([F1, {"kpi_id": "E6", "current": {"inputs": {"water_consumption": {"value": 2, "unit": "ML"}}}}])
    assert by_id["E6"]["current"]["value"] == pytest.approx(0.2)  # 2000 kL / 10000 cr


@pytest.mark.parametrize("kid,inputs,expected", [
    ("E7", {"water_recycled": {"value": 25, "unit": "kL"}, "water_consumption": {"value": 100, "unit": "kL"}}, 25.0),
    ("E9", {"waste_recovered": {"value": 30, "unit": "MT"}, "total_waste": {"value": 120, "unit": "MT"}}, 25.0),
    ("G1", {"women_directors": {"value": 3}, "board_size": {"value": 12}}, 25.0),
])
def test_percentage_ratios(kid, inputs, expected):
    by_id, _ = run([{"kpi_id": kid, "current": {"inputs": inputs}}])
    assert by_id[kid]["status"] == "derived"
    assert by_id[kid]["current"]["value"] == pytest.approx(expected)


def test_ratio_not_computed_across_different_units():
    by_id, _ = run([{"kpi_id": "E7", "current": {"inputs": {"water_recycled": {"value": 25, "unit": "ML"},
                                                             "water_consumption": {"value": 100, "unit": "kL"}}}}])
    assert by_id["E7"]["status"] == "not_disclosed"


def test_s13_from_s12_components():
    by_id, _ = run([{"kpi_id": "S12", "current": {"unit": "Rs. crore", "components": {"budgeted": 50, "spent": 45}}}])
    assert by_id["S13"]["status"] == "derived"
    assert by_id["S13"]["current"]["value"] == pytest.approx(90.0)


def test_money_in_lakh_converted_to_crore():
    by_id, _ = run([{"kpi_id": "S12", "current": {"unit": "Rs. lakh", "components": {"budgeted": 5000, "spent": 4000}}}])
    s12 = by_id["S12"]
    assert s12["status"] == "derived"
    assert s12["current"]["components"] == {"budgeted": 50.0, "spent": 40.0}


def test_f2_green_pct_and_g3_pct_and_s1_total():
    by_id, _ = run([
        {"kpi_id": "F2", "current": {"unit": "Rs. crore", "components": {"green_capex": 200, "total_capex": 1000}}},
        {"kpi_id": "G3", "current": {"components": {"count": 6}, "inputs": {"board_size": {"value": 12}}}},
        {"kpi_id": "S1", "current": {"inputs": {"employees": {"value": 900}, "workers": {"value": 100}}}},
    ])
    assert by_id["F2"]["current"]["components"]["green_pct"] == pytest.approx(20.0)
    assert by_id["G3"]["current"]["components"]["pct"] == pytest.approx(50.0)
    assert by_id["S1"]["current"]["value"] == 1000
    assert all(by_id[k]["status"] == "derived" for k in ("F2", "G3", "S1"))


def test_ltifr_per_200k_hours_scaled():
    by_id, _ = run([{"kpi_id": "S3", "current": {"value": 0.2, "unit": "per 200,000 hours worked"}}])
    assert by_id["S3"]["current"]["value"] == pytest.approx(1.0)


def test_merge_prefers_stated_value_over_inputs():
    chunk1 = [{"kpi_id": "E7", "current": {"inputs": {"water_recycled": {"value": 10, "unit": "kL"},
                                                      "water_consumption": {"value": 100, "unit": "kL"}}}}]
    chunk2 = [{"kpi_id": "E7", "current": {"fy": "FY25", "value": 12, "unit": "%"}, "pages": [44]}]
    by_id, _ = run(chunk1, chunk2)
    e7 = by_id["E7"]
    assert e7["status"] == "reported"
    assert e7["current"]["value"] == 12
    assert e7["pages"] == [44]


def test_merge_fills_components_across_chunks():
    by_id, _ = run(
        [{"kpi_id": "S10", "current": {"components": {"filed": 5}}}],
        [{"kpi_id": "S10", "current": {"components": {"resolved": 4, "pending": 1}}}],
    )
    assert by_id["S10"]["current"]["components"] == {"filed": 5, "resolved": 4, "pending": 1}
    assert "filed 5" in format_year(by_id["S10"]["current"])


def test_text_kpi_and_unknown_ids_ignored():
    by_id, records = run([
        {"kpi_id": "e10", "current": {"components": {"status": "Yes", "target_year": "2040", "scope": "Scope 1+2"}}},
        {"kpi_id": "Z99", "current": {"value": 1}},
        "garbage",
    ])
    assert by_id["E10"]["status"] == "reported"
    assert len(records) == len(KPI_CATALOG)
    assert report_ready({"kpis": records})


def test_standardize_fy():
    from kpis import standardize_fy
    assert standardize_fy("FY 2024-25") == "FY2025"
    assert standardize_fy("2024-25") == "FY2025"
    assert standardize_fy("FY25") == "FY2025"
    assert standardize_fy("FY 2023-24") == "FY2024"
    assert standardize_fy("FY24") == "FY2024"
    assert standardize_fy("FY 2025-26") == "FY2026"
    assert standardize_fy("FY26") == "FY2026"
    assert standardize_fy("2026") == "FY2026"
    assert standardize_fy("Jan 24 - Dec 24") == "FY2024"
    assert standardize_fy("Jan 25 - Dec 25") == "FY2025"
    assert standardize_fy("CY 2024") == "FY2024"
    assert standardize_fy("invalid-text") == ""
    assert standardize_fy(None) == ""


def test_component_and_text_kpi_year_slots():
    from kpis import company_multi_year_frame
    _, records = run([
        {"kpi_id": "E1", "current": {"fy": "FY 2025-26", "value": 97.5}, "previous": {"fy": "FY 2024-25", "value": 96.98}, "pages": [60, 70]},
        {"kpi_id": "S2", "current": {"components": {"employees": 0, "workers": 0}},
                        "previous": {"components": {"employees": 0, "workers": 0}}},
        {"kpi_id": "S10", "current": {"components": {"filed": 11, "resolved": 11, "pending": 0}},
                         "previous": {"components": {"filed": 7, "resolved": 7, "pending": 0}}},
        {"kpi_id": "E4", "current": {"fy": "Jan 25 - Dec 25", "value": 10535856},
                        "previous": {"fy": "Jan 24 - Dec 24", "value": 9962535}},
    ])
    report = {"name": "BRSR26 HUL", "kpis": records}
    df = company_multi_year_frame(report)

    # S2 must be numeric 0 for FY2026 and FY2025, and 'Not Found' for unpopulated FY2024
    s2_row = df[df["S. No."] == "S2"].iloc[0]
    assert s2_row["FY2026"] == 0
    assert s2_row["FY2025"] == 0
    assert s2_row["FY2024"] == "Not Found"

    # S10 must be numeric 11 and 7
    s10_row = df[df["S. No."] == "S10"].iloc[0]
    assert s10_row["FY2026"] == 11
    assert s10_row["FY2025"] == 7
    assert s10_row["FY2024"] == "Not Found"

    # E4 Scope 3 must be numeric
    e4_row = df[df["S. No."] == "E4"].iloc[0]
    assert e4_row["FY2026"] == 10535856
    assert e4_row["FY2025"] == 9962535
    assert e4_row["FY2024"] == "Not Found"

    # E1 Source Page(s) verification citation
    e1_row = df[df["S. No."] == "E1"].iloc[0]
    assert "FY25 data page 60" in e1_row["Source Page(s)"]
    assert "FY26 data page 70" in e1_row["Source Page(s)"]

    # Ghost columns must NOT exist
    assert "FYJAN" not in "".join(df.columns)


def test_company_multi_year_frame_and_comparative():
    from kpis import company_multi_year_frame, comparative_analysis_frame, consolidated_multi_year_frame, export_excel_workbook
    _, records = run([
        {"kpi_id": "E1", "current": {"fy": "FY 2024-25", "value": 31.0, "unit": "%"},
                        "previous": {"fy": "FY 2023-24", "value": 24.5, "unit": "%"},
                        "pages": [40, 50]},
        {"kpi_id": "E2", "current": {"fy": "FY 2024-25", "value": 120.0, "unit": "MJ per Rs. crore"},
                        "previous": {"fy": "FY 2023-24", "value": 130.0, "unit": "MJ per Rs. crore"}},
    ])
    report = {"name": "Acme Corp", "kpis": records}
    df_multi = company_multi_year_frame(report)
    assert "S. No." in df_multi.columns
    assert "Unit" in df_multi.columns
    assert "Env./Social/Gov." in df_multi.columns
    assert "KPI" in df_multi.columns
    assert "FY2024" in df_multi.columns
    assert "FY2025" in df_multi.columns
    assert "FY2026" in df_multi.columns
    assert "Source Page(s)" in df_multi.columns

    # Verify numeric values and Not Found
    e1_row = df_multi[df_multi["S. No."] == "E1"].iloc[0]
    assert e1_row["FY2024"] == 24.5
    assert e1_row["FY2025"] == 31.0
    assert e1_row["FY2026"] == "Not Found"
    assert "FY24 data page 40" in e1_row["Source Page(s)"]
    assert "FY25 data page 50" in e1_row["Source Page(s)"]

    # Comparative analysis frame
    comp_df = comparative_analysis_frame([report], target_fy="FY2025")
    assert "S. No." in comp_df.columns
    assert "KPI Name" in comp_df.columns
    assert "Unit" in comp_df.columns
    assert "Acme Corp" in comp_df.columns
    e1_row = comp_df[comp_df["S. No."] == "E1"].iloc[0]
    assert e1_row["Acme Corp"] == 31.0

    # Unpopulated KPI in comparative analysis must be 'Not Found'
    e10_row = comp_df[comp_df["S. No."] == "E10"].iloc[0]
    assert e10_row["Acme Corp"] == "Not Found"

    # Consolidated multi-year table
    consol_df = consolidated_multi_year_frame([report])
    assert consol_df.columns[0] == "Company"
    assert consol_df.columns[-1] == "Source Page(s)"
    assert consol_df["Company"].iloc[0] == "Acme Corp"

    # Excel export
    excel_bytes = export_excel_workbook([report], target_fy="FY2025")
    assert len(excel_bytes) > 1000


def test_extract_numeric_kpi_value_and_edge_cases():
    from kpis import extract_numeric_kpi_value

    # Direct floats and integers
    assert extract_numeric_kpi_value({"kpi_id": "E1"}, {"value": 96.98}) == 96.98
    assert extract_numeric_kpi_value({"kpi_id": "E2"}, {"value": "57,700"}) == 57700
    assert extract_numeric_kpi_value({"kpi_id": "E3"}, {"value": 0.15}) == 0.15

    # Fatalities 0 must be 0, never 'Not Found'
    assert extract_numeric_kpi_value({"kpi_id": "S2"}, {"components": {"employees": 0, "workers": 0}}) == 0
    assert extract_numeric_kpi_value({"kpi_id": "S2"}, {"value": 0}) == 0

    # Components
    assert extract_numeric_kpi_value({"kpi_id": "S1"}, {"components": {"employees": 10000, "workers": 7699}}) == 17699
    assert extract_numeric_kpi_value({"kpi_id": "S6"}, {"components": {"female_pct": 17.38, "male_pct": 82.62}}) == 17.38
    assert extract_numeric_kpi_value({"kpi_id": "S10"}, {"components": {"filed": 11, "resolved": 11, "pending": 0}}) == 11
    assert extract_numeric_kpi_value({"kpi_id": "S11"}, {"components": {"received": 4, "resolved": 4}}) == 4
    assert extract_numeric_kpi_value({"kpi_id": "S12"}, {"components": {"spent": 25.6, "budgeted": 25.0}}) == 25.6
    assert extract_numeric_kpi_value({"kpi_id": "G3"}, {"components": {"count": 6, "pct": 50.0}}) == 6
    assert extract_numeric_kpi_value({"kpi_id": "F1"}, {"components": {"revenue": 60000, "ebitda": 14000, "pat": 10000}}) == 60000

    # Text / Qualitative KPIs return 'Not Found'
    assert extract_numeric_kpi_value({"kpi_id": "E10"}, {"components": {"status": "Yes", "target_year": "2040"}}) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "E11"}, {"text": "Alliance for Water Stewardship"}) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "G5"}, {"components": {"status": "Yes"}}) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "G7"}, {"components": {"status": "Yes"}}) == "Not Found"

    # Empty slots return 'Not Found'
    assert extract_numeric_kpi_value({"kpi_id": "E1"}, None) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "E1"}, {}) == "Not Found"


def test_trailing_page_citations():
    from kpis import get_kpi_page_citation

    # When slot pages are present
    rec = {
        "kpi_id": "E1",
        "current": {"fy": "FY 2024-25", "value": 31.0, "pages": [70]},
        "previous": {"fy": "FY 2023-24", "value": 24.5, "pages": [60]},
        "pages": [60, 70],
    }
    citation = get_kpi_page_citation(rec, ["FY2024", "FY2025", "FY2026"])
    assert "FY24 data page 60" in citation
    assert "FY25 data page 70" in citation
    assert "FY26" not in citation  # Not Found year is not cited

    # Undisclosed KPI returns 'Not Found'
    empty_rec = {"kpi_id": "E1", "current": {}, "previous": {}, "pages": []}
    assert get_kpi_page_citation(empty_rec, ["FY2024", "FY2025", "FY2026"]) == "Not Found"
