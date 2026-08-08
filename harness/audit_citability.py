#!/usr/bin/env python3
"""Assert that every must_cite asset is reachable by some tool the agent has.

A benchmark that asks an investigation to cite an asset it cannot reach does not
measure investigation quality. It measures whether the agent guessed. This check
fails loudly so that a case cannot be added with an unreachable answer key.

An asset counts as citable when either
  - it is a relation in the case warehouse, so warehouse.query can name it, or
  - it is published to the catalog with a urn whose logical name survives
    score.logical_names(), so a datahub.* observation can name it.

    python3 harness/audit_citability.py
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emit_estate
import score
import warehouse


def catalog_names() -> dict:
    plan = emit_estate.build_plan()
    out = {}
    for d in plan["datasets"]:
        out[d["logical"].split(".")[-1]] = d["urn"]
    for j in plan["jobs"]:
        out[j["logical"]] = j["urn"]
    for a in plan["assertions"]:
        out[a["logical"]] = a["urn"]
    return out


def main() -> int:
    root = HERE.parent
    cases = sorted((root / "cases").glob("MTI-*.json"))
    if not cases:
        print("no cases found", file=sys.stderr)
        return 2
    catalog = catalog_names()
    warehouses = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else root / "warehouse"
    failures = []
    for path in cases:
        case = json.loads(path.read_text(encoding="utf-8"))
        db = warehouses / f"{case['case_id']}.db"
        relations = {r["name"] for r in warehouse.schema_of(db)} if db.exists() else set()
        for asset in case.get("must_cite", []):
            urn = catalog.get(asset)
            citable = asset in relations or (urn and asset in score.logical_names(urn))
            if not citable:
                failures.append((case["case_id"], asset))
    for case_id, asset in failures:
        print(f"UNCITABLE {case_id}: {asset}")
    print(f"{len(cases)} cases, {len(failures)} uncitable must_cite assets")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
