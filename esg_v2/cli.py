"""
Command-line batch runner (no Streamlit needed) - good for bulk runs.

    # one report
    python -m esg_v2.cli reports/Infosys_FY25.pdf --company "Infosys" --out out/infosys.xlsx

    # a whole folder (company name taken from the file name); finished PDFs are
    # cached in out/cache/ so a crashed / rate-limited run resumes where it stopped
    python -m esg_v2.cli reports/ --out out/results.xlsx

Output: an .xlsx (KPI Output / Needs Review / Raw Datapoints) or a .csv,
in the column template from config/output_columns.csv.
"""

import argparse
import json
import os
import re
import sys

from .engine import extract_report
from .export import to_csv_bytes, to_xlsx_bytes


def _company_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\b(fy|annual|report|brsr|integrated|20\d\d(\s*\d\d)?)\b", " ", stem, flags=re.I)
    return " ".join(stem.split()).strip() or os.path.basename(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract ESG KPIs from annual/BRSR report PDFs.")
    parser.add_argument("input", help="A PDF file or a folder of PDFs")
    parser.add_argument("--company", help="Company name (single-file mode)")
    parser.add_argument("--out", default="esg_output.xlsx", help="Output .xlsx or .csv path")
    parser.add_argument("--current-only", action="store_true", help="Only export the current FY rows")
    parser.add_argument("--no-cache", action="store_true", help="Re-extract even if a cached result exists")
    args = parser.parse_args(argv)

    try:
        from extractor import _load_env_file  # reuse the repo's .env loader

        _load_env_file()
    except Exception:
        pass

    if os.path.isdir(args.input):
        pdfs = sorted(os.path.join(args.input, f) for f in os.listdir(args.input) if f.lower().endswith(".pdf"))
    else:
        pdfs = [args.input]
    if not pdfs:
        print("No PDFs found.", file=sys.stderr)
        return 1

    out_dir = os.path.dirname(os.path.abspath(args.out))
    cache_dir = os.path.join(out_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    reports = []
    for i, pdf in enumerate(pdfs, 1):
        company = args.company if (args.company and len(pdfs) == 1) else _company_from_filename(pdf)
        cache = os.path.join(cache_dir, os.path.basename(pdf) + ".json")
        if os.path.isfile(cache) and not args.no_cache:
            with open(cache, encoding="utf-8") as fh:
                reports.append(json.load(fh))
            print(f"[{i}/{len(pdfs)}] {company}: cached")
            continue
        print(f"[{i}/{len(pdfs)}] {company}: extracting...")
        try:
            report = extract_report(pdf, company, on_status=lambda f, t: print(f"    {f:5.0%} {t}", end="\r"))
        except Exception as exc:
            print(f"\n    FAILED: {exc}")
            continue
        report["source_file"] = os.path.basename(pdf)
        lean = {k: v for k, v in report.items() if k not in ("full_text", "page_map")}
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(lean, fh, ensure_ascii=False, indent=1)
        disclosed = sum(1 for r in report["kpis"] if r["status"] != "not_disclosed")
        flagged = sum(1 for r in report["kpis"] if r.get("flags"))
        print(f"\n    {disclosed}/{len(report['kpis'])} KPIs found, {flagged} flagged for review")
        reports.append(report)

    kwargs = {"include_previous": not args.current_only}
    data = to_csv_bytes(reports, **kwargs) if args.out.lower().endswith(".csv") else to_xlsx_bytes(reports, **kwargs)
    with open(args.out, "wb") as fh:
        fh.write(data)
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
