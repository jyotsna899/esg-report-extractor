"""Loads the editable CSV configuration in config/."""

import csv
import os
from dataclasses import dataclass, field
from functools import lru_cache

CONFIG_DIR = os.environ.get(
    "ESG_CONFIG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"),
)

GROUP_ORDER = ["general", "governance", "employees", "environment", "community", "financial"]

# Headings that mark where each group's disclosures usually sit. Used by
# page_finder as a bonus on top of the per-datapoint keywords.
GROUP_HEADINGS = {
    "general": ["section a", "general disclosures", "details of the listed entity", "employees and workers"],
    "governance": ["section b", "management and process disclosures", "report on corporate governance",
                   "composition of the board", "committees of the board"],
    "employees": ["principle 3", "principle 5", "well-being", "human rights", "occupational health and safety"],
    "environment": ["principle 6", "principle 2", "energy consumption", "greenhouse gas", "water", "waste"],
    "community": ["principle 8", "principle 9", "annual report on csr", "corporate social responsibility"],
    "financial": ["statement of profit and loss", "financial highlights", "cash flow statement", "financial performance"],
}


@dataclass
class Datapoint:
    id: str
    group: str
    name: str
    kind: str
    brsr_reference: str
    instruction: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class KPI:
    kpi_id: str
    name: str
    category: str
    type: str  # Direct | Derived | Helper
    output_unit: str
    formula: str
    components: list[tuple[str, str]]  # (component name, expression)
    direction: str


def _read(name: str, config_dir: str | None = None) -> list[dict]:
    path = os.path.join(config_dir or CONFIG_DIR, name)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [{k.strip(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


@lru_cache(maxsize=4)
def load_datapoints(config_dir: str | None = None) -> dict[str, Datapoint]:
    out = {}
    for row in _read("datapoints.csv", config_dir):
        if not row.get("id"):
            continue
        dp = Datapoint(
            id=row["id"], group=row["group"].lower(), name=row["name"], kind=row["kind"].lower(),
            brsr_reference=row.get("brsr_reference", ""), instruction=row.get("instruction", ""),
            keywords=[k.strip().lower() for k in row.get("keywords", "").split("|") if k.strip()],
        )
        if dp.id in out:
            raise ValueError(f"Duplicate datapoint id in datapoints.csv: {dp.id}")
        out[dp.id] = dp
    return out


def _parse_components(raw: str) -> list[tuple[str, str]]:
    comps = []
    for part in raw.split(";"):
        if "=" in part:
            name, expr = part.split("=", 1)
            comps.append((name.strip(), expr.strip()))
    return comps


@lru_cache(maxsize=4)
def load_kpis(config_dir: str | None = None) -> list[KPI]:
    kpis = []
    for row in _read("kpis.csv", config_dir):
        if not row.get("kpi_id"):
            continue
        kpis.append(KPI(
            kpi_id=row["kpi_id"], name=row["name"], category=row["category"],
            type=row.get("type", "Direct"), output_unit=row.get("output_unit", ""),
            formula=row.get("formula", ""), components=_parse_components(row.get("components", "")),
            direction=row.get("direction", ""),
        ))
    return kpis


@lru_cache(maxsize=4)
def load_output_columns(config_dir: str | None = None) -> list[tuple[str, str]]:
    return [(r["column_header"], r["field"]) for r in _read("output_columns.csv", config_dir) if r.get("field")]
