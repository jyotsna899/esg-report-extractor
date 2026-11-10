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
    assert standardize_fy(None) == ""


def test_company_multi_year_frame_and_comparative():
    from kpis import company_multi_year_frame, comparative_analysis_frame, export_excel_workbook
    _, records = run([
        {"kpi_id": "E1", "current": {"fy": "FY 2024-25", "value": 31.0, "unit": "%"},
                        "previous": {"fy": "FY 2023-24", "value": 24.5, "unit": "%"}},
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

    # Comparative analysis frame
    comp_df = comparative_analysis_frame([report], target_fy="FY2025")
    assert "S. No." in comp_df.columns
    assert "KPI Name" in comp_df.columns
    assert "Unit" in comp_df.columns
    assert "Acme Corp" in comp_df.columns
    e1_row = comp_df[comp_df["S. No."] == "E1"].iloc[0]
    assert "31" in str(e1_row["Acme Corp"])

    # Excel export
    excel_bytes = export_excel_workbook([report], target_fy="FY2025")
    assert len(excel_bytes) > 1000
