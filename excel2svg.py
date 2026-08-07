from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

REQUIRED_COLUMNS = [
    "Dep ID",
    "Activity Name",
    "Dependency 1",
    "Dependency 2",
    "Dependency 3",
    "Dependency 4",
    "Dependency 5",
]

OPTIONAL_DETAILS_COLUMN = "Details"
OPTIONAL_COST_TO_ACHIEVE_COLUMN = "Cost to Achieve"
OPTIONAL_COST_SAVINGS_COLUMN = "Cost Savings"
LABEL_WRAP_LENGTH = 30


def _norm_col(c: str) -> str:
    return " ".join(str(c).strip().split()).lower()


def _clean_cell(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip()


def _escape_label(s: str) -> str:
    return s.replace('"', r"\"")


def _wrap_activity_name(name: str, max_len: int = LABEL_WRAP_LENGTH) -> str:
    if len(name) <= max_len or "\n" in name or "\\n" in name or "<br" in name.lower():
        return name

    mid = len(name) // 2
    breakpoints = [i for i, ch in enumerate(name) if ch.isspace()]
    if not breakpoints:
        return name

    split_at = min(breakpoints, key=lambda i: abs(i - mid))
    return name[:split_at].rstrip() + "\\n" + name[split_at:].lstrip()


def _alias(dep_id: str) -> str:
    a = dep_id.strip().replace("-", "_").replace(" ", "_")
    if not a or not a[0].isalpha():
        a = f"ID_{a}"
    return a


def _parse_money(v) -> float:
    """
    Parse currency-ish values from Excel:
      $1,234 -> 1234
      (1,234) -> -1234
      1234.56 -> 1234.56
      blank / non-numeric -> 0.0
    """
    if v is None:
        return 0.0
    try:
        if pd.isna(v):
            return 0.0
    except Exception:
        pass

    if isinstance(v, (int, float)) and pd.notna(v):
        return float(v)

    s = str(v).strip()
    if not s:
        return 0.0

    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1].strip()

    s = s.replace("$", "").replace(",", "").strip()
    try:
        n = float(s)
        return -n if neg else n
    except Exception:
        return 0.0


@dataclass(frozen=True)
class Node:
    dep_id: str
    name: str
    deps: Tuple[str, ...]
    details: str = ""
    cost_to_achieve: float = 0.0
    cost_savings: float = 0.0


def _read_excel_dataframe_resilient(xlsx_path: Path, sheet: Optional[str]) -> "pd.DataFrame":
    """
    Read .xlsx even if open in Excel by copying to temp first.
    """
    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / f"{xlsx_path.stem}__pumlcopy__{os.getpid()}{xlsx_path.suffix}"
    shutil.copy2(xlsx_path, tmp_path)
    try:
        return pd.read_excel(tmp_path, sheet_name=sheet, dtype=object)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def read_nodes_from_excel(xlsx_path: Path, sheet: Optional[str] = None) -> Dict[str, Node]:
    df = _read_excel_dataframe_resilient(xlsx_path, sheet)

    if isinstance(df, dict):
        df = df[next(iter(df))]

    if "Action" in df.columns:
        df = df[~df["Action"].astype(str).str.strip().str.lower().eq("ignore")]

    col_map = {_norm_col(c): c for c in df.columns}
    missing = [c for c in REQUIRED_COLUMNS if _norm_col(c) not in col_map]
    if missing:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing) +
            "\nFound columns: " + ", ".join(map(str, df.columns))
        )

    def col(name: str) -> str:
        return col_map[_norm_col(name)]

    details_col_name = col_map.get(_norm_col(OPTIONAL_DETAILS_COLUMN))
    cta_col_name     = col_map.get(_norm_col(OPTIONAL_COST_TO_ACHIEVE_COLUMN))
    sav_col_name     = col_map.get(_norm_col(OPTIONAL_COST_SAVINGS_COLUMN))

    nodes: Dict[str, Node] = {}
    for _, row in df.iterrows():
        dep_id = _clean_cell(row[col("Dep ID")])
        if not dep_id:
            continue

        activity_name = _wrap_activity_name(_clean_cell(row[col("Activity Name")]) or dep_id)

        deps_raw = [
            _clean_cell(row[col("Dependency 1")]),
            _clean_cell(row[col("Dependency 2")]),
            _clean_cell(row[col("Dependency 3")]),
            _clean_cell(row[col("Dependency 4")]),
            _clean_cell(row[col("Dependency 5")]),
        ]
        deps = tuple(d for d in deps_raw if d and d != "-" and d.lower() != "nan")

        details = ""
        if details_col_name:
            details = _clean_cell(row[details_col_name])

        cost_to_achieve = 0.0
        if cta_col_name:
            cost_to_achieve = _parse_money(row[cta_col_name])

        cost_savings = 0.0
        if sav_col_name:
            cost_savings = _parse_money(row[sav_col_name])

        nodes[dep_id] = Node(
            dep_id=dep_id,
            name=activity_name,
            deps=deps,
            details=details,
            cost_to_achieve=cost_to_achieve,
            cost_savings=cost_savings
        )

    if not nodes:
        raise ValueError("No Dep ID rows found in the spreadsheet.")

    return nodes


def include_external_dependencies(nodes: Dict[str, Node]) -> Dict[str, Node]:
    all_ids = set(nodes.keys())
    referenced: Set[str] = set()
    for n in nodes.values():
        referenced.update(n.deps)

    missing = sorted(referenced - all_ids)
    if not missing:
        return nodes

    extended = dict(nodes)
    for dep_id in missing:
        extended[dep_id] = Node(dep_id=dep_id, name=dep_id, deps=tuple(), details="", cost_to_achieve=0.0, cost_savings=0.0)
    return extended


def compute_groups(nodes: Dict[str, Node]) -> Tuple[Dict[str, int], List[List[str]], List[str]]:
    indeg: Dict[str, int] = {nid: 0 for nid in nodes}
    children: Dict[str, List[str]] = defaultdict(list)

    for nid, n in nodes.items():
        for d in n.deps:
            children[d].append(nid)
            indeg[nid] += 1

    q = deque([nid for nid, deg in indeg.items() if deg == 0])
    group_of: Dict[str, int] = {nid: 0 for nid in q}

    while q:
        u = q.popleft()
        for v in children.get(u, []):
            group_of[v] = max(group_of.get(v, 0), group_of[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)

    cycle_nodes = [nid for nid, deg in indeg.items() if deg > 0]
    if cycle_nodes:
        max_group = max(group_of.values()) if group_of else 0
        for nid in cycle_nodes:
            group_of[nid] = max_group + 1

    max_group = max(group_of.values()) if group_of else 0
    groups: List[List[str]] = [[] for _ in range(max_group + 1)]
    for nid, t in group_of.items():
        groups[t].append(nid)
    for t in groups:
        t.sort()

    return group_of, groups, sorted(cycle_nodes)


def generate_plantuml_elk(nodes: Dict[str, Node], groups: List[List[str]], cycle_nodes: List[str]) -> str:
    lines: List[str] = []
    lines.append("@startuml")
    lines.append("title Application Decommission Dependency Graph")
    lines.append("skinparam nodesep 60")
    lines.append("skinparam ranksep 120")
    lines.append("skinparam Padding 12")
    lines.append("skinparam ArrowThickness 1.2")
    lines.append("skinparam ArrowPadding 14")

    lines.append("skinparam shadowing false")
    lines.append("skinparam packageStyle rectangle")
    lines.append("skinparam linetype ortho")
    lines.append("skinparam ArrowFontSize 1")
    lines.append("skinparam ArrowFontColor transparent")

    lines.append("skinparam rectangle<<node>> {")
    lines.append("BackgroundColor #E1ECF9")
    lines.append("BorderColor #4F6FA1")
    lines.append("BorderThickness 1.5")
    lines.append("FontColor #1F2937")
    lines.append("}")

    lines.append("skinparam package<<group>> {")
    lines.append("BackgroundColor #EEF2F7")
    lines.append("BorderColor #CBD5E1")
    lines.append("BorderThickness 1")
    lines.append("FontColor #374151")
    lines.append("}")

    lines.append("")

    for t_idx, ids in enumerate(groups):
        group_label = f"Group {t_idx}"
        lines.append(f'package "{_escape_label(group_label)}" <<group>> {{')
        if not ids:
            lines.append(f'  rectangle "" as group_{t_idx}_EMPTY <<hidden>>')
        else:
            for nid in ids:
                n = nodes[nid]
                lines.append(f'  rectangle "{_escape_label(n.name)}" as {_alias(nid)} <<node>>')
        lines.append("}")
        lines.append("")

    lines.append("' Dependencies (Prerequisite -> Dependent)")
    for nid, n in sorted(nodes.items(), key=lambda kv: kv[0]):
        dst = _alias(nid)
        for d in n.deps:
            src = _alias(d)
            meta = f"__SRC={src}__DST={dst}__"
            lines.append(f'{src} --> {dst} : [[{meta} .]]')

    if cycle_nodes:
        joined = ", ".join(cycle_nodes)
        lines.append("")
        lines.append("note right")
        lines.append(f"  Cycle detected among: {joined}")
        lines.append("  These were placed in the last group.")
        lines.append("end note")

    lines.append("@enduml")
    return "\n".join(lines)


def write_details_json(nodes: Dict[str, Node], out_json_path: Path) -> None:
    payload = {}
    for dep_id, n in sorted(nodes.items(), key=lambda kv: kv[0]):
        payload[_alias(dep_id)] = {
            "dep_id": dep_id,
            "name": n.name,
            "details": n.details or "",
            "costToAchieve": n.cost_to_achieve,
            "costSavings": n.cost_savings,
        }
    out_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_viewer_datajson(nodes: Dict[str, Node],
                          groups: List[List[str]],
                          out_path: Path) -> None:
    group_defs = [{"id": f"G{i}", "label": f"Group {i}", "order": i}
                  for i in range(len(groups))]

    node_items = []
    id_to_group = {}
    for gi, ids in enumerate(groups):
        gid = f"G{gi}"
        for nid in ids:
            id_to_group[nid] = gid

    for nid, n in sorted(nodes.items(), key=lambda kv: kv[0]):
        node_items.append({
            "id": nid,
            "label": n.name or nid,
            "group": id_to_group.get(nid, "G0"),
            "details": n.details or "",
            "costToAchieve": n.cost_to_achieve,
            "costSavings": n.cost_savings
        })

    edges = []
    for nid, n in nodes.items():
        for dep in n.deps:
            edges.append({"source": dep, "target": nid})

    payload = {"groups": group_defs, "nodes": node_items, "edges": edges}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Generate ELK PlantUML from Excel dependency sheet + JSON outputs.")
    ap.add_argument("excel", help="Path to input Excel (.xlsx)")
    ap.add_argument("--sheet", help="Sheet name (optional). If omitted, first sheet is used.")
    ap.add_argument("-o", "--output", help="Output .puml path (default: <excel>.puml)")
    ap.add_argument("--details-json", help="Output details.json path (default: alongside .puml as details.json)")
    ap.add_argument("--viewer-json", help="Output viewer data.json path (default: alongside .puml as data.json)")
    ap.add_argument("--svg", help="Path to diagram.svg (not used by this script directly; used by packager step).")
    args = ap.parse_args()

    xlsx_path = Path(args.excel)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Excel file not found: {xlsx_path}")

    out_puml_path = Path(args.output) if args.output else xlsx_path.with_suffix(".puml")

    nodes = read_nodes_from_excel(xlsx_path, sheet=args.sheet)
    nodes = include_external_dependencies(nodes)
    _, groups, cycle_nodes = compute_groups(nodes)

    out_puml_path.write_text(generate_plantuml_elk(nodes, groups, cycle_nodes), encoding="utf-8")
    print(f"Wrote PlantUML to: {out_puml_path}")

    out_details_path = Path(args.details_json) if args.details_json else out_puml_path.with_name("details.json")
    write_details_json(nodes, out_details_path)
    print(f"Wrote Details JSON to: {out_details_path}")

    out_viewer_path = Path(args.viewer_json) if args.viewer_json else out_puml_path.with_name("data.json")
    write_viewer_datajson(nodes, groups, out_viewer_path)
    print(f"Wrote Viewer data.json to: {out_viewer_path}")

    if args.svg:
        print(f"SVG provided (for next step packager): {args.svg}")


if __name__ == "__main__":
    main()
