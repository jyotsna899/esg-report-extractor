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


def test_merge_derives_from_raw_inputs_over_stated_value():
    chunk1 = [{"kpi_id": "E7", "current": {"inputs": {"water_recycled": {"value": 10, "unit": "kL"},
                                                      "water_consumption": {"value": 100, "unit": "kL"}}}}]
    chunk2 = [{"kpi_id": "E7", "current": {"fy": "FY25", "value": 12, "unit": "%"}, "pages": [44]}]
    by_id, _ = run(chunk1, chunk2)
    e7 = by_id["E7"]
    assert e7["status"] == "derived"
    assert e7["current"]["value"] == 10.0
    assert 44 in e7["pages"]


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

    # Qualitative KPIs return target year or clean status string
    assert extract_numeric_kpi_value({"kpi_id": "E10"}, {"components": {"status": "Yes", "target_year": "2040"}}) == 2040
    assert extract_numeric_kpi_value({"kpi_id": "E11"}, {"text": "Alliance for Water Stewardship"}) == "Alliance for Water Stewardship"
    assert extract_numeric_kpi_value({"kpi_id": "G5"}, {"components": {"status": "Yes"}}) == "Yes"
    assert extract_numeric_kpi_value({"kpi_id": "G7"}, {"components": {"status": "Yes"}}) == "Yes"

    # Empty slots return 'Not Found'
    assert extract_numeric_kpi_value({"kpi_id": "E1"}, None) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "E1"}, {}) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "E10"}, None) == "Not Found"
    assert extract_numeric_kpi_value({"kpi_id": "G5"}, {}) == "Not Found"


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


def test_hul_renewable_energy_and_energy_intensity_derivation():
    # Test based on Hindustan Unilever (HUL) manager review:
    # 1. Renewable Energy: 35,60,000 GJ renewable out of 36,51,000 GJ total -> 97.51%, overriding false 100%
    # 2. Energy Intensity: 36,51,000 GJ * 1000 MJ/GJ / 60,000 cr revenue = 60,850 MJ per Rs. crore
    hul_f1 = {
        "kpi_id": "F1",
        "current": {
            "fy": "FY 2025-26",
            "unit": "Rs. crore",
            "components": {
                "revenue": 60000,
                "revenue_standalone": 58000,
                "revenue_consolidated": 60000,
                "boundary": "consolidated",
            },
        },
    }
    hul_e1 = {
        "kpi_id": "E1",
        "current": {
            "fy": "FY 2025-26",
            "value": 100.0,  # Erroneous headline 100%
            "unit": "%",
            "inputs": {
                "renewable_energy": {"value": 3560000, "unit": "GJ"},
                "total_energy": {"value": 3651000, "unit": "GJ"},
            },
        },
    }
    hul_e2 = {
        "kpi_id": "E2",
        "current": {
            "fy": "FY 2025-26",
            "value": 57500,  # Mismatched pre-printed intensity
            "unit": "MJ per Rs. crore",
            "inputs": {
                "total_energy": {"value": 3651000, "unit": "GJ"},
            },
        },
    }

    by_id, records = run([hul_f1, hul_e1, hul_e2])

    e1 = by_id["E1"]
    assert e1["status"] == "derived"
    assert e1["current"]["value"] == pytest.approx(97.51, rel=1e-3)
    assert e1["current"]["unit"] == "%"

    e2 = by_id["E2"]
    assert e2["status"] == "derived"
    # 3651000 GJ * 1000 MJ/GJ / 60000 cr = 60850 MJ / Rs. cr
    assert e2["current"]["value"] == pytest.approx(60850.0, rel=1e-3)
    assert e2["current"]["unit"] == "MJ per Rs. crore"


def test_reporting_boundary_revenue_denominator():
    # Standalone boundary vs Consolidated boundary
    f1_both = {
        "kpi_id": "F1",
        "current": {
            "fy": "FY 2025-26",
            "unit": "Rs. crore",
            "components": {
                "revenue_standalone": 50000,
                "revenue_consolidated": 60000,
                "boundary": "consolidated",
            },
        },
    }
    e2_input = {
        "kpi_id": "E2",
        "current": {
            "fy": "FY 2025-26",
            "inputs": {
                "total_energy": {"value": 3000000, "unit": "GJ"},
            },
        },
    }

    # Consolidated boundary: revenue = 60,000 -> 3,000,000 * 1000 / 60,000 = 50,000 MJ/cr
    by_id_con, _ = run([f1_both, e2_input])
    assert by_id_con["E2"]["current"]["value"] == pytest.approx(50000.0)

    # Standalone boundary: revenue = 50,000 -> 3,000,000 * 1000 / 50,000 = 60,000 MJ/cr
    f1_both["current"]["components"]["boundary"] = "standalone"
    by_id_std, _ = run([f1_both, e2_input])
    assert by_id_std["E2"]["current"]["value"] == pytest.approx(60000.0)


def test_s3_and_s4_derivations():
    s3_kpi = {
        "kpi_id": "S3",
        "current": {
            "inputs": {
                "injuries": {"value": 5},
                "hours_worked": {"value": 5000000},
            }
        },
    }
    s4_kpi = {
        "kpi_id": "S4",
        "current": {
            "inputs": {
                "wellbeing_spend": {"value": 25, "unit": "Rs. crore"},
                "payroll": {"value": 500, "unit": "Rs. crore"},
            }
        },
    }
    by_id, _ = run([s3_kpi, s4_kpi])
    assert by_id["S3"]["current"]["value"] == pytest.approx(1.0)
    assert by_id["S3"]["current"]["unit"] == "per million hours worked"
    assert by_id["S4"]["current"]["value"] == pytest.approx(5.0)
    assert by_id["S4"]["current"]["unit"] == "%"


def test_e2_overrides_verbatim_57500_with_derived_formula():
    """HUL case: Company reported verbatim 57,500, but raw total energy is 3,651,000 GJ and revenue is 60,000 cr."""
    f1 = {
        "kpi_id": "F1",
        "current": {
            "fy": "FY 2025-26",
            "unit": "Rs. crore",
            "components": {"revenue": 60000, "boundary": "consolidated"},
        },
    }
    e1 = {
        "kpi_id": "E1",
        "current": {
            "fy": "FY 2025-26",
            "inputs": {
                "renewable_energy": {"value": 3560000, "unit": "GJ"},
                "total_energy": {"value": 3651000, "unit": "GJ"},
            },
        },
    }
    e2_verbatim = {
        "kpi_id": "E2",
        "current": {
            "fy": "FY 2025-26",
            "value": 57500,
            "unit": "MJ per Rs. crore",
        },
    }
    by_id, _ = run([f1, e1, e2_verbatim])
    e2 = by_id["E2"]
    # Formula: 3,651,000 GJ * 1000 / 60,000 cr = 60,850 MJ / Rs. crore
    assert e2["current"]["value"] == pytest.approx(60850.0)
    assert e2["status"] == "derived"
    assert "replaces reported 57,500" in e2["derivation"]


def test_cross_kpi_pooling_water_and_waste():
    """Tests that E6 uses E7's water consumption, and E8 uses E9's total waste."""
    f1 = {
        "kpi_id": "F1",
        "current": {"fy": "FY25", "unit": "Rs. crore", "components": {"revenue": 10000}},
    }
    e6 = {"kpi_id": "E6", "current": {"value": 45.8, "unit": "kL per Rs. crore"}}
    e7 = {
        "kpi_id": "E7",
        "current": {
            "inputs": {
                "water_recycled": {"value": 50000, "unit": "kL"},
                "water_consumption": {"value": 200000, "unit": "kL"},
            }
        },
    }
    e8 = {"kpi_id": "E8", "current": {"value": 1.8, "unit": "t per Rs. crore"}}
    e9 = {
        "kpi_id": "E9",
        "current": {
            "inputs": {
                "waste_recovered": {"value": 15000, "unit": "MT"},
                "total_waste": {"value": 30000, "unit": "MT"},
            }
        },
    }
    by_id, _ = run([f1, e6, e7, e8, e9])
    # E6: 200,000 kL / 10,000 cr = 20.0 kL / Rs. crore (replaces 45.8)
    assert by_id["E6"]["current"]["value"] == pytest.approx(20.0)
    assert by_id["E6"]["status"] == "derived"
    # E7: 50,000 / 200,000 * 100 = 25.0%
    assert by_id["E7"]["current"]["value"] == pytest.approx(25.0)
    assert by_id["E7"]["status"] == "derived"
    # E8: 30,000 MT / 10,000 cr = 3.0 MT / Rs. crore (replaces 1.8)
    assert by_id["E8"]["current"]["value"] == pytest.approx(3.0)
    assert by_id["E8"]["status"] == "derived"
    # E9: 15,000 / 30,000 * 100 = 50.0%
    assert by_id["E9"]["current"]["value"] == pytest.approx(50.0)
    assert by_id["E9"]["status"] == "derived"


def test_cross_kpi_pooling_csr_allocation():
    """Tests that S13 derives efficiency from S12's budgeted and spent components."""
    s12 = {
        "kpi_id": "S12",
        "current": {"components": {"budgeted": 20.0, "spent": 25.0}, "unit": "Rs. crore"},
    }
    s13 = {"kpi_id": "S13", "current": {"value": 90.0, "unit": "%"}}  # inaccurate reported
    by_id, _ = run([s12, s13])
    # S13: (25.0 / 20.0) * 100 = 125.0%
    assert by_id["S13"]["current"]["value"] == pytest.approx(125.0)
    assert by_id["S13"]["status"] == "derived"


def test_revenue_back_calculation_and_intensity_derivation():
    """
    Simulates the exact HUL BRSR report case:
    - F1 revenue is missing.
    - E2 has raw energy 3,651,354 GJ and reported intensity 57.5 GJ/crore.
    - System back-calculates revenue = 3,651,354 / 57.5 = 63,493.12 cr.
    - E3 raw scope 1+2 = 7054 tCO2e is divided by revenue = 0.1111 tCO2e / Rs. crore.
    - E5 raw air emissions = 427,000 kg is divided by revenue = 6.725 kg / Rs. crore.
    - E6 water consumption = 2,908,019 kL is divided by revenue = 45.8 kL / Rs. crore.
    """
    e1 = {
        "kpi_id": "E1",
        "current": {
            "inputs": {
                "renewable_energy": {"value": 3560000, "unit": "GJ"},
                "total_energy": {"value": 3651354, "unit": "GJ"},
            }
        },
    }
    e2 = {
        "kpi_id": "E2",
        "current": {
            "value": 57.5,
            "unit": "GJ per Rs. crore of turnover",
            "inputs": {"total_energy": {"value": 3651354, "unit": "GJ"}},
        },
    }
    e3 = {
        "kpi_id": "E3",
        "current": {
            "value": 7054,
            "unit": "tCO2e",
            "inputs": {"scope1": {"value": 7054, "unit": "tCO2e"}, "scope2": {"value": 0, "unit": "tCO2e"}},
        },
    }
    e5 = {
        "kpi_id": "E5",
        "current": {
            "inputs": {"sox": {"value": 135000, "unit": "kg"}, "nox": {"value": 292000, "unit": "kg"}},
        },
    }
    e6 = {
        "kpi_id": "E6",
        "current": {
            "value": 45.8,
            "unit": "kL per Rs. crore",
            "inputs": {"water_consumption": {"value": 2908019, "unit": "kL"}},
        },
    }
    by_id, _ = run([e1, e2, e3, e5, e6])

    # F1 is back-filled
    f1 = by_id["F1"]
    assert f1["current"]["value"] == pytest.approx(63493.12, rel=1e-3)
    assert f1["status"] == "derived"

    # E2 intensity: 3651354 * 1000 / 63493.12 = 57,507.9 MJ / Rs. crore (or scaled 57,500)
    assert by_id["E2"]["current"]["value"] == pytest.approx(57500.0, rel=1e-2)
    assert by_id["E2"]["current"]["unit"] == "MJ per Rs. crore"

    # E3 emission intensity: 7054 / 63493.12 = 0.1111 tCO2e / Rs. crore
    assert by_id["E3"]["current"]["value"] == pytest.approx(0.1111, rel=1e-2)
    assert by_id["E3"]["current"]["unit"] == "tCO2e per Rs. crore"

    # E5 air emission intensity: (135,000 + 292,000) / 63493.12 = 6.725 kg / Rs. crore
    assert by_id["E5"]["current"]["value"] == pytest.approx(6.725, rel=1e-2)
    assert by_id["E5"]["current"]["unit"] == "kg per Rs. crore"

    # E6 water intensity: 2908019 / 63493.12 = 45.8 kL / Rs. crore
    assert by_id["E6"]["current"]["value"] == pytest.approx(45.8, rel=1e-2)


def test_extract_numeric_kpi_value_all_categories():
    """Tests extract_numeric_kpi_value across numerical and qualitative KPIs."""
    from kpis import extract_numeric_kpi_value

    # E10: Net Zero target year
    e10_rec = {"kpi_id": "E10"}
    assert extract_numeric_kpi_value(e10_rec, {"components": {"status": "Yes", "target_year": 2039}}) == 2039
    assert extract_numeric_kpi_value(e10_rec, {"components": {"status": "Yes"}}) == "Yes"

    # E11: International Targets
    e11_rec = {"kpi_id": "E11"}
    assert extract_numeric_kpi_value(e11_rec, {"components": {"status": "RE100, UNGC"}}) == "RE100, UNGC"

    # S5: Attrition rate
    s5_rec = {"kpi_id": "S5"}
    assert extract_numeric_kpi_value(s5_rec, {"components": {"permanent_employees": 18.4}}) == 18.4

    # S9: Training coverage
    s9_rec = {"kpi_id": "S9"}
    assert extract_numeric_kpi_value(s9_rec, {"components": {"employees": 87.3}}) == 87.3

    # S14: Rural & Semi-Urban Job Creation
    s14_rec = {"kpi_id": "S14"}
    assert extract_numeric_kpi_value(s14_rec, {"components": {"rural": 32.0, "semi_urban": 7.3}}) == 39.3

    # S15: Aspirational Districts spend
    s15_rec = {"kpi_id": "S15"}
    assert extract_numeric_kpi_value(s15_rec, {"components": {"spent": 25.62}}) == 25.62

    # G3: Independent Directors count
    g3_rec = {"kpi_id": "G3"}
    assert extract_numeric_kpi_value(g3_rec, {"components": {"count": 3, "pct": 33.3}}) == 3

    # G4: Board Committees count
    g4_rec = {"kpi_id": "G4"}
    assert extract_numeric_kpi_value(g4_rec, {"components": {"count": 3}}) == 3
    assert extract_numeric_kpi_value(g4_rec, {"components": {"committees": ["Audit", "CSR-ESG", "Management"]}}) == 3

    # G5, G7, G8, G9, G10: Governance statuses
    assert extract_numeric_kpi_value({"kpi_id": "G5"}, {"components": {"status": "Yes"}}) == "Yes"
    assert extract_numeric_kpi_value({"kpi_id": "G6"}, {"components": {"attendance_pct": 100}}) == 100
    assert extract_numeric_kpi_value({"kpi_id": "G7"}, {"components": {"status": "Yes"}}) == "Yes"
    assert extract_numeric_kpi_value({"kpi_id": "G8"}, {"components": {"status": "Reasonable"}}) == "Reasonable"
    assert extract_numeric_kpi_value({"kpi_id": "G9"}, {"components": {"status": "Yes"}}) == "Yes"
    assert extract_numeric_kpi_value({"kpi_id": "G10"}, {"components": {"status": "No"}}) == "No"

    # F1 & F2
    assert extract_numeric_kpi_value({"kpi_id": "F1"}, {"components": {"turnover": 63493}}) == 63493
    assert extract_numeric_kpi_value({"kpi_id": "F2"}, {"components": {"green_pct": 15.2}}) == 15.2


def test_multi_year_frame_image2_format():
    """Tests that company_multi_year_frame generates the exact clean row in Image 2."""
    from kpis import company_multi_year_frame, consolidated_multi_year_frame

    report = {
        "name": "BRSR26 HUL",
        "kpis": [
            {
                "kpi_id": "E2",
                "kpi_name": "Energy Intensity",
                "category": "Environment",
                "required_unit": "MJ per Rs. crore of revenue",
                "status": "derived",
                "current": {"fy": "FY2026", "value": 57500, "unit": "MJ per Rs. crore", "pages": [28]},
                "previous": {"fy": "FY2025", "value": 57700, "unit": "MJ per Rs. crore", "pages": [28]},
                "years": {
                    "FY2026": {"value": 57500, "unit": "MJ per Rs. crore", "pages": [28]},
                    "FY2025": {"value": 57700, "unit": "MJ per Rs. crore", "pages": [28]},
                },
                "pages": [28],
            }
        ],
    }
    df = consolidated_multi_year_frame([report], years=["FY2024", "FY2025", "FY2026"])
    row = df.iloc[0]
    assert row["Company"] == "BRSR26 HUL"
    assert row["S. No."] == "E2"
    assert row["Unit"] == "MJ per Rs. crore"
    assert row["Env./Social/Gov."] == "Environment"
    assert row["KPI"] == "Energy Intensity"
    assert row["FY2024"] == "Not Found"
    assert row["FY2025"] == 57700
    assert row["FY2026"] == 57500
    assert row["Source Page(s)"] == "FY25 data page 28, FY26 data page 28"


def test_full_hul_brsr_extraction_dataset():
    """
    Tests complete KPI coverage and formatting for Hindustan Unilever Limited (HUL) BRSR 2026,
    verifying all 36 KPIs against disclosed report figures.
    """
    from kpis import company_multi_year_frame, extract_numeric_kpi_value

    hul_raw_kpis = [
        {"kpi_id": "F1", "current": {"fy": "FY2026", "value": 63493, "unit": "Rs. crore", "components": {"turnover": 63493, "boundary": "consolidated"}}, "previous": {"fy": "FY2025", "value": 59711, "unit": "Rs. crore", "components": {"turnover": 59711}}, "pages": [8, 28]},
        {"kpi_id": "F2", "current": {"fy": "FY2026", "value": 15.2, "unit": "%", "components": {"green_pct": 15.2, "rd_pct": 13.1}}, "pages": [16]},
        {"kpi_id": "E1", "current": {"fy": "FY2026", "inputs": {"renewable_energy": {"value": 3560085, "unit": "GJ"}, "total_energy": {"value": 3651354, "unit": "GJ"}}}, "previous": {"fy": "FY2025", "inputs": {"renewable_energy": {"value": 3341108, "unit": "GJ"}, "total_energy": {"value": 3445152, "unit": "GJ"}}}, "pages": [28]},
        {"kpi_id": "E2", "current": {"fy": "FY2026", "value": 57.5, "unit": "GJ per Rs. crore of turnover"}, "previous": {"fy": "FY2025", "value": 57.7, "unit": "GJ per Rs. crore"}, "pages": [28]},
        {"kpi_id": "E3", "current": {"fy": "FY2026", "value": 7054, "unit": "tCO2e", "inputs": {"scope1": {"value": 7054, "unit": "tCO2e"}, "scope2": {"value": 0, "unit": "tCO2e"}}}, "previous": {"fy": "FY2025", "value": 8944, "unit": "tCO2e", "inputs": {"scope1": {"value": 8944, "unit": "tCO2e"}, "scope2": {"value": 0, "unit": "tCO2e"}}}, "pages": [30]},
        {"kpi_id": "E4", "current": {"fy": "FY2026", "value": 10535856, "unit": "tCO2e"}, "previous": {"fy": "FY2025", "value": 9962535, "unit": "tCO2e"}, "pages": [32]},
        {"kpi_id": "E5", "current": {"fy": "FY2026", "inputs": {"sox": {"value": 135000, "unit": "kg"}, "nox": {"value": 292000, "unit": "kg"}}}, "pages": [30]},
        {"kpi_id": "E6", "current": {"fy": "FY2026", "value": 45.8, "unit": "kL per Rs. crore"}, "previous": {"fy": "FY2025", "value": 46.8, "unit": "kL per Rs. crore"}, "pages": [29]},
        {"kpi_id": "E7", "current": {"fy": "FY2026", "value": 100, "unit": "%"}, "pages": [29]},
        {"kpi_id": "E8", "current": {"fy": "FY2026", "value": 1.8, "unit": "metric tonnes per Rs. crore"}, "previous": {"fy": "FY2025", "value": 1.5, "unit": "metric tonnes per Rs. crore"}, "pages": [31]},
        {"kpi_id": "E9", "current": {"fy": "FY2026", "value": 99.26, "unit": "%"}, "previous": {"fy": "FY2025", "value": 99.45, "unit": "%"}, "pages": [31]},
        {"kpi_id": "E10", "current": {"fy": "FY2026", "components": {"status": "Yes", "target_year": 2039, "scope": "Scope 1, 2, 3"}}, "pages": [17, 32]},
        {"kpi_id": "E11", "current": {"fy": "FY2026", "components": {"status": "AWS Platinum, RE100, UNGC"}}, "pages": [1, 10, 30]},
        {"kpi_id": "S1", "current": {"fy": "FY2026", "components": {"employees": 7434, "workers": 17555}}, "pages": [26, 141]},
        {"kpi_id": "S2", "current": {"fy": "FY2026", "components": {"employees": 0, "workers": 1}}, "previous": {"fy": "FY2025", "components": {"employees": 0, "workers": 0}}, "pages": [169]},
        {"kpi_id": "S3", "current": {"fy": "FY2026", "value": 0.06, "unit": "per million hours worked"}, "previous": {"fy": "FY2025", "value": 0.23, "unit": "per million hours worked"}, "pages": [169]},
        {"kpi_id": "S4", "current": {"fy": "FY2026", "value": 0.2, "unit": "%"}, "pages": [166]},
        {"kpi_id": "S5", "current": {"fy": "FY2026", "components": {"permanent_employees": 18.4, "permanent_workers": 4.8}}, "previous": {"fy": "FY2025", "components": {"permanent_employees": 18.9, "permanent_workers": 5.1}}, "pages": [8]},
        {"kpi_id": "S6", "current": {"fy": "FY2026", "components": {"female_pct": 31.36, "male_pct": 68.64}}, "pages": [26]},
        {"kpi_id": "S7", "current": {"fy": "FY2026", "value": 49.65, "unit": "ratio"}, "pages": [26]},
        {"kpi_id": "S8", "current": {"fy": "FY2026", "value": 1.0021, "unit": "ratio"}, "pages": [26]},
        {"kpi_id": "S9", "current": {"fy": "FY2026", "components": {"employees": 87.3, "workers": 91.7}}, "previous": {"fy": "FY2025", "components": {"employees": 78.9, "workers": 85.2}}, "pages": [21]},
        {"kpi_id": "S10", "current": {"fy": "FY2026", "components": {"filed": 11, "resolved": 11, "pending": 0}}, "previous": {"fy": "FY2025", "components": {"filed": 7, "resolved": 7}}, "pages": [26]},
        {"kpi_id": "S11", "current": {"fy": "FY2026", "components": {"received": 2, "resolved": 2, "pending": 0}}, "previous": {"fy": "FY2025", "components": {"received": 4, "resolved": 4}}, "pages": [38]},
        {"kpi_id": "S12", "current": {"fy": "FY2026", "components": {"spent": 25.62, "budgeted": 25.62}, "unit": "Rs. crore"}, "pages": [36]},
        {"kpi_id": "S13", "current": {"fy": "FY2026", "value": 100.0, "unit": "%"}, "pages": [36]},
        {"kpi_id": "S14", "current": {"fy": "FY2026", "components": {"rural": 32.0, "semi_urban": 7.3}}, "pages": [36]},
        {"kpi_id": "S15", "current": {"fy": "FY2026", "components": {"spent": 25.62}, "unit": "Rs. crore"}, "pages": [36]},
        {"kpi_id": "S16", "current": {"fy": "FY2026", "components": {"status": "Living wage by 2026"}}, "pages": [7, 10]},
        {"kpi_id": "G1", "current": {"fy": "FY2026", "value": 33.33, "unit": "%"}, "pages": [26, 142]},
        {"kpi_id": "G2", "current": {"fy": "FY2026", "value": 66.67, "unit": "%"}, "pages": [26, 142]},
        {"kpi_id": "G3", "current": {"fy": "FY2026", "components": {"count": 3, "pct": 33.3}}, "pages": [13]},
        {"kpi_id": "G4", "current": {"fy": "FY2026", "components": {"count": 3, "committees": ["Audit", "CSR-ESG", "Management"]}}, "pages": [13]},
        {"kpi_id": "G5", "current": {"fy": "FY2026", "components": {"status": "Yes", "detail": "CSR-ESG Committee"}}, "pages": [13]},
        {"kpi_id": "G6", "current": {"fy": "FY2026", "components": {"attendance_pct": 100}}, "pages": [14]},
        {"kpi_id": "G7", "current": {"fy": "FY2026", "components": {"status": "Yes", "detail": "BRSR / SEBI"}}, "pages": [1, 13]},
        {"kpi_id": "G8", "current": {"fy": "FY2026", "components": {"status": "Reasonable", "detail": "B S R & Co. LLP"}}, "pages": [3, 4]},
        {"kpi_id": "G9", "current": {"fy": "FY2026", "components": {"status": "Yes", "detail": "GHG Protocol"}}, "pages": [9, 30]},
        {"kpi_id": "G10", "current": {"fy": "FY2026", "components": {"status": "No"}}, "pages": [15]},
    ]
    by_id, normalized_list = run(hul_raw_kpis)
    assert len(normalized_list) == 39

    # Verify no 'Not Found' for disclosed FY26 metrics
    rep = {"name": "BRSR26 HUL", "kpis": normalized_list}
    df = company_multi_year_frame(rep, years=["FY2024", "FY2025", "FY2026"])
    assert len(df) == 39

    fy26_col = df["FY2026"].tolist()
    # All 36 KPIs should have real values for FY26 (none 'Not Found')
    assert "Not Found" not in fy26_col

    # Check key indicators
    e2_row = df[df["S. No."] == "E2"].iloc[0]
    assert e2_row["FY2026"] == pytest.approx(57500, rel=1e-2)
    assert e2_row["FY2025"] == pytest.approx(57700, rel=1e-2)

    e3_row = df[df["S. No."] == "E3"].iloc[0]
    assert e3_row["FY2026"] == pytest.approx(0.1111, rel=1e-2)

    s5_row = df[df["S. No."] == "S5"].iloc[0]
    assert s5_row["FY2026"] == 18.4
    assert s5_row["FY2025"] == 18.9

    s9_row = df[df["S. No."] == "S9"].iloc[0]
    assert s9_row["FY2026"] == 87.3
    assert s9_row["FY2025"] == 78.9

    s14_row = df[df["S. No."] == "S14"].iloc[0]
    assert s14_row["FY2026"] == 39.3

    e10_row = df[df["S. No."] == "E10"].iloc[0]
    assert e10_row["FY2026"] == 2039

    g5_row = df[df["S. No."] == "G5"].iloc[0]
    assert g5_row["FY2026"] == "Yes"



