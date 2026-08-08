#!/usr/bin/env python3
"""Emit the synthetic product-analytics estate the benchmark investigates.

The showcase pack gives the catalog ambient realism. This gives us the assets
the 11 incidents actually reference, with lineage we control, so a case can say
"walk up from signup_conversion_daily" and mean something.

Everything here is invented. No table, column, or identifier from any real
production system appears.

    python3 harness/emit_estate.py            # emit
    python3 harness/emit_estate.py --dry-run  # print the plan only
"""

from __future__ import annotations

import argparse
import sys

PLATFORM_PG = "postgres"
PLATFORM_DBT = "dbt"
ENV = "PROD"

# name -> (platform, description, upstream logical names)
# Ordered roughly raw -> staging -> marts so the lineage reads top to bottom.
DATASETS: dict[str, tuple[str, str, list[str]]] = {
    # --- raw ---------------------------------------------------------------
    "raw.web_sessions": (PLATFORM_PG, "One row per visit. Carries an is_automated flag that not every producer sets.", []),
    "raw.channel_events_raw": (PLATFORM_PG, "Acquisition events keyed by channel, written by per-channel collectors.", []),
    "raw.checkout_events": (PLATFORM_PG, "Funnel steps emitted during checkout, including the browser user agent.", []),
    "raw.orders": (PLATFORM_PG, "Confirmed orders.", []),
    "raw.customers_dim": (PLATFORM_PG, "Customer dimension. customer_code is the join key used downstream.", []),
    "raw.pricing_page_events": (PLATFORM_PG, "Views and interactions on the pricing page.", []),
    "raw.feature_usage_events": (PLATFORM_PG, "Client-side usage events per feature module.", []),
    "raw.release_deploy_log": (PLATFORM_PG, "Deploy records. Says a build shipped, not that anyone reached it.", []),
    "raw.schema_migration_log": (PLATFORM_PG, "Applied migrations, including collation and type changes.", []),
    "raw.metric_definition_history": (PLATFORM_PG, "Versioned definitions for every published metric.", []),
    "legacy.orders_legacy_deprecated": (PLATFORM_PG, "Superseded orders table. Retained, no longer written to.", []),
    # --- staging -----------------------------------------------------------
    "staging.events_daily": (PLATFORM_DBT, "Daily event counts. Buckets on local time.", ["raw.channel_events_raw", "raw.web_sessions"]),
    "staging.events_weekly_rollup": (PLATFORM_DBT, "Weekly event counts. Buckets on UTC.", ["raw.channel_events_raw"]),
    "staging.orders_current": (PLATFORM_DBT, "Current orders model. Replaced the legacy table.", ["raw.orders"]),
    # --- marts -------------------------------------------------------------
    "marts.signups": (PLATFORM_DBT, "Signups by day and channel.", ["staging.events_daily"]),
    "marts.signup_conversion_daily": (PLATFORM_DBT, "Signups divided by sessions. Sensitive to anything that moves the denominator.", ["marts.signups", "raw.web_sessions"]),
    "marts.active_users_daily": (PLATFORM_DBT, "Active users per day, per the current definition.", ["raw.web_sessions", "raw.metric_definition_history"]),
    "marts.nightly_orders_snapshot": (PLATFORM_DBT, "Nightly snapshot of orders. Refreshed every 24 hours.", ["staging.orders_current"]),
    "marts.customer_enrichment": (PLATFORM_DBT, "Customers joined to order history on customer_code.", ["raw.customers_dim", "staging.orders_current"]),
    "marts.checkout_completions": (PLATFORM_DBT, "Completed checkouts, segmentable by browser version.", ["raw.checkout_events"]),
    "marts.feature_adoption": (PLATFORM_DBT, "Usage per released feature.", ["raw.feature_usage_events", "raw.release_deploy_log"]),
    "sandbox.writeback_demo": (PLATFORM_DBT, "Write-back demonstration target. No case cites this dataset.", []),
}

# A dataset no case references, so an approved write-back can be demonstrated
# without touching evidence. Descriptions in this corpus are themselves answer
# keys: MTI-003 turns on `staging.events_daily` saying it buckets on local time,
# and a write against it would destroy the case while the corpus is being scored.
# This one exists to be written to.
SANDBOX_DATASET = "sandbox.writeback_demo"

# job logical name -> (description, inputs, outputs)
JOBS: dict[str, tuple[str, list[str], list[str]]] = {
    "channel_ingest_job": ("Per-channel collector. One task per channel; a dead task produces silence, not an error.",
                           [], ["raw.channel_events_raw"]),
    "orders_ingest_job": ("Loads confirmed orders. Same schedule every day of the week.",
                          [], ["raw.orders"]),
    "nightly_snapshot_job": ("Builds the nightly orders snapshot.",
                             ["staging.orders_current"], ["marts.nightly_orders_snapshot"]),
}

# assertion logical name -> (description, dataset)
ASSERTIONS: dict[str, tuple[str, str]] = {
    "nightly_orders_freshness_assertion": ("Freshness check on the nightly snapshot.", "marts.nightly_orders_snapshot"),
    "orders_quality_monitor": ("Row-count and null-rate monitor for orders.", "legacy.orders_legacy_deprecated"),
}

TAGS = [
    # Pre-created on purpose. DataHub will not apply a tag that does not exist
    # yet, so an agent writing an unseeded tag fails silently. Every root cause
    # the agent can conclude has to exist here before the first run.
    "root-cause:collector_dead",
    "root-cause:bot_traffic_mixed",
    "root-cause:tz_mismatch",
    "root-cause:freshness_window_too_wide",
    "root-cause:below_fold_unreachable",
    "root-cause:detector_misaimed",
    "root-cause:collation_join_break",
    "root-cause:price_sensitivity",
    "root-cause:metric_definition_widened",
    "root-cause:seasonal_demand_shift",
    "root-cause:browser_specific_product_defect",
    "verdict:instrument_failure",
    "verdict:customer_behavior",
    "verdict:definition_change",
    "verdict:upstream_data_defect",
    "investigated-by-witness-graph",
]

PREFIX = "witnessgraph"


ORCHESTRATOR = "airflow"


def dataset_urn(name: str, platform: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{PREFIX}.{name},{ENV})"


def dataflow_urn() -> str:
    return f"urn:li:dataFlow:({ORCHESTRATOR},{PREFIX},{ENV})"


def datajob_urn(name: str) -> str:
    return f"urn:li:dataJob:({dataflow_urn()},{name})"


def assertion_urn(name: str) -> str:
    return f"urn:li:assertion:{name}"


def build_plan() -> dict:
    datasets = []
    for name, (platform, desc, upstreams) in DATASETS.items():
        datasets.append({
            "logical": name,
            "urn": dataset_urn(name, platform),
            "description": desc,
            "upstreams": [dataset_urn(u, DATASETS[u][0]) for u in upstreams],
        })
    jobs = []
    for name, (desc, inputs, outputs) in JOBS.items():
        jobs.append({
            "logical": name,
            "urn": datajob_urn(name),
            "description": desc,
            "inputs": [dataset_urn(i, DATASETS[i][0]) for i in inputs],
            "outputs": [dataset_urn(o, DATASETS[o][0]) for o in outputs],
        })
    assertions = []
    for name, (desc, dataset) in ASSERTIONS.items():
        assertions.append({
            "logical": name,
            "urn": assertion_urn(name),
            "description": desc,
            "dataset": dataset_urn(dataset, DATASETS[dataset][0]),
        })
    return {"datasets": datasets, "jobs": jobs, "assertions": assertions, "tags": TAGS}


def emit(plan: dict) -> int:
    from datahub.emitter.mce_builder import make_tag_urn
    from datahub.emitter.mcp import MetadataChangeProposalWrapper
    from datahub.ingestion.graph.client import get_default_graph
    import datahub.metadata.schema_classes as models

    graph = get_default_graph()
    n = 0

    for tag in plan["tags"]:
        mcp = MetadataChangeProposalWrapper(
            entityUrn=make_tag_urn(tag),
            aspect=models.TagPropertiesClass(name=tag),
        )
        graph.emit(mcp)
        n += 1
    print(f"  tags pre-created: {len(plan['tags'])}")

    for d in plan["datasets"]:
        graph.emit(MetadataChangeProposalWrapper(
            entityUrn=d["urn"],
            aspect=models.DatasetPropertiesClass(description=d["description"], name=d["logical"]),
        ))
        n += 1
        if d["upstreams"]:
            graph.emit(MetadataChangeProposalWrapper(
                entityUrn=d["urn"],
                aspect=models.UpstreamLineageClass(upstreams=[
                    models.UpstreamClass(dataset=u, type=models.DatasetLineageTypeClass.TRANSFORMED)
                    for u in d["upstreams"]
                ]),
            ))
            n += 1
    print(f"  datasets emitted: {len(plan['datasets'])}")
    edges = sum(len(d["upstreams"]) for d in plan["datasets"])
    print(f"  lineage edges:    {edges}")

    # Jobs and assertions used to sit in the plan, get counted in the summary,
    # and never leave this process, because only tags and datasets had a loop.
    # Four cases name a job or an assertion in must_cite, so for those the
    # answer key pointed at assets no lookup could return: an investigation
    # could not reach full citation recall however well it reasoned, and the
    # scorer read the gap as a lucky guess.
    graph.emit(MetadataChangeProposalWrapper(
        entityUrn=dataflow_urn(),
        aspect=models.DataFlowInfoClass(name=PREFIX, description="Ingest and modelling jobs."),
    ))
    n += 1
    for j in plan["jobs"]:
        graph.emit(MetadataChangeProposalWrapper(
            entityUrn=j["urn"],
            aspect=models.DataJobInfoClass(
                name=j["logical"], type=models.AzkabanJobTypeClass.COMMAND,
                description=j["description"], flowUrn=dataflow_urn(),
            ),
        ))
        n += 1
        # A collector's outputs are what makes "this table went silent" a
        # question about a job rather than about a table.
        graph.emit(MetadataChangeProposalWrapper(
            entityUrn=j["urn"],
            aspect=models.DataJobInputOutputClass(
                inputDatasets=j["inputs"], outputDatasets=j["outputs"],
            ),
        ))
        n += 1
    print(f"  jobs emitted:     {len(plan['jobs'])}")

    for a in plan["assertions"]:
        if "freshness" in a["logical"]:
            info = models.AssertionInfoClass(
                type=models.AssertionTypeClass.FRESHNESS,
                description=a["description"],
                freshnessAssertion=models.FreshnessAssertionInfoClass(
                    type=models.FreshnessAssertionTypeClass.DATASET_CHANGE,
                    entity=a["dataset"],
                    schedule=models.FreshnessAssertionScheduleClass(
                        type=models.FreshnessAssertionScheduleTypeClass.FIXED_INTERVAL,
                        # The window is the evidence. A check that accepts a
                        # 36-hour gap cannot see a snapshot that stalled for one
                        # day, and the number has to be readable to be cited.
                        fixedInterval=models.FixedIntervalScheduleClass(
                            unit=models.CalendarIntervalClass.HOUR, multiple=36,
                        ),
                    ),
                ),
            )
        else:
            info = models.AssertionInfoClass(
                type=models.AssertionTypeClass.DATASET,
                description=a["description"],
                datasetAssertion=models.DatasetAssertionInfoClass(
                    dataset=a["dataset"],
                    scope=models.DatasetAssertionScopeClass.DATASET_ROWS,
                    operator=models.AssertionStdOperatorClass.GREATER_THAN,
                ),
            )
        graph.emit(MetadataChangeProposalWrapper(entityUrn=a["urn"], aspect=info))
        n += 1
    print(f"  assertions:       {len(plan['assertions'])}")
    print(f"  total aspects:    {n}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    plan = build_plan()
    print(f"Estate plan: {len(plan['datasets'])} datasets, "
          f"{len(plan['jobs'])} jobs, {len(plan['assertions'])} assertions, "
          f"{len(plan['tags'])} tags")

    # Every logical name the cases cite must exist, or an investigation cannot
    # possibly reach full citation recall. Cheaper to fail here than mid-demo.
    known = set(DATASETS) | set(JOBS) | set(ASSERTIONS)
    import json, pathlib
    missing = {}
    for f in sorted(pathlib.Path("cases").glob("MTI-*.json")):
        case = json.loads(f.read_text(encoding="utf-8"))
        for ref in case.get("must_cite", []):
            base = ref.split(".")[-1] if "." in ref else ref
            if ref not in known and not any(k.endswith("." + ref) or k == ref for k in known):
                missing.setdefault(case["case_id"], []).append(ref)
    if missing:
        print("\nUNRESOLVED must_cite references:")
        for cid, refs in missing.items():
            print(f"  {cid}: {', '.join(refs)}")
        print("\nAdd these assets to the estate before running the benchmark.")
        return 1
    print("all must_cite references resolve against the estate")

    if args.dry_run:
        print("\n(dry run, nothing emitted)")
        return 0
    print("\nEmitting...")
    emit(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
