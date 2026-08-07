# gen_datajson.py
from pathlib import Path
import argparse
from excel2svg import read_nodes_from_excel, include_external_dependencies, compute_groups, write_viewer_datajson

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("excel")
    ap.add_argument("--sheet")
    ap.add_argument("-o", "--output", default="data.json")
    args = ap.parse_args()

    nodes = read_nodes_from_excel(Path(args.excel), sheet=args.sheet)
    nodes = include_external_dependencies(nodes)
    _, groups, _ = compute_groups(nodes)
    write_viewer_datajson(nodes, groups, Path(args.output))
    print(f"Wrote {args.output}")
