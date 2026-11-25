"""
Accuracy check against a hand-filled answer key.

1. Copy eval/answer_key_template.csv, fill in the correct values for a few
   companies (by reading the reports yourself).
2. Run the tool and export CSV/XLSX.
3. python eval/evaluate.py eval/answer_key.csv out/results.xlsx

Prints overall accuracy, accuracy per KPI (fix the worst first), and every
mismatch with the page the tool used. Numbers match within 1 % (or 0.01
absolute for small values); text matches if the key is contained in the
tool's answer (case-insensitive), so "Yes" matches "Yes - CSR & ESG Committee".
"""

import argparse
import re
import sys

import pandas as pd


def _norm_company(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _norm_fy(s):
    m = re.findall(r"20\d\d", str(s))
    if m:
        return m[-1]
    m = re.findall(r"\b\d\d\b", str(s))
    return f"20{m[-1]}" if m else str(s).strip()


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _match(expected, actual):
    if str(expected).strip().lower() in ("", "nan", "not disclosed", "nd", "-"):
        return str(actual).strip().lower() in ("", "nan")
    e, a = _num(expected), _num(actual)
    if e is not None:
        if a is None:
            return False
        return abs(a - e) <= max(0.01, abs(e) * 0.01)
    return str(expected).strip().lower() in str(actual).strip().lower()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("answer_key")
    p.add_argument("tool_output", help=".csv or .xlsx produced by the tool")
    args = p.parse_args(argv)

    key = pd.read_csv(args.answer_key, dtype=str).fillna("")
    out = (pd.read_excel(args.tool_output, sheet_name="KPI Output", dtype=str)
           if args.tool_output.lower().endswith(".xlsx") else pd.read_csv(args.tool_output, dtype=str)).fillna("")

    out["_k"] = out["Company"].map(_norm_company) + "|" + out["Financial Year"].map(_norm_fy) + "|" + out["KPI ID"]
    lookup = out.set_index("_k")
    rows = []
    for _, r in key.iterrows():
        k = f"{_norm_company(r['Company'])}|{_norm_fy(r['Financial Year'])}|{r['KPI ID']}"
        got = lookup.loc[k] if k in lookup.index else None
        if isinstance(got, pd.DataFrame):
            got = got.iloc[0]
        actual = "" if got is None else got.get("Value", "")
        rows.append({"Company": r["Company"], "FY": r["Financial Year"], "KPI ID": r["KPI ID"],
                     "Expected": r["Expected Value"], "Tool": actual,
                     "Page": "" if got is None else got.get("Source Page(s)", ""),
                     "Flags": "" if got is None else got.get("Review Flags", ""),
                     "OK": _match(r["Expected Value"], actual)})
    res = pd.DataFrame(rows)
    if res.empty:
        print("Answer key is empty.")
        return 1
    print(f"\nOverall accuracy: {res['OK'].mean():.1%} ({res['OK'].sum()}/{len(res)})\n")
    per = res.groupby("KPI ID")["OK"].agg(["mean", "count"]).sort_values("mean")
    print("Per KPI (worst first):")
    for kid, row in per.iterrows():
        print(f"  {kid:5s} {row['mean']:6.0%}  (n={int(row['count'])})")
    bad = res[~res["OK"]]
    if not bad.empty:
        print("\nMismatches:")
        print(bad.drop(columns=["OK"]).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
