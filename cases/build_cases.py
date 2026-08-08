#!/usr/bin/env python3
"""Generate the benchmark case files.

Each case is derived from an incident this team actually hit in production,
stripped of service names, personal names and internal identifiers, then
restated against the synthetic catalog. The answer key is what separates a
benchmark from a demo.

`must_cite` holds LOGICAL asset names. bind_urns.py resolves them against the
loaded catalog once ingestion finishes, so the corpus stays readable and stays
portable across whatever dataset we seed on top of.

    python3 cases/build_cases.py --out cases/
"""

from __future__ import annotations

import argparse
import json
import pathlib

CASES = [
    {
        "case_id": "MTI-001",
        "title": "One channel's signups flatlined at zero",
        "symptom": "Signups attributed to the affiliate channel dropped to zero on the 14th and have stayed there for 22 days.",
        "difficulty": "easy",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "collector_dead",
            "explanation": "The ingestion job for that channel stopped emitting. The zero measures missing collection, not missing customers.",
        },
        "seed": [{"op": "stop_collector", "target": "channel_events_raw", "params": {"channel": "affiliate", "from_day": 14}}],
        "must_cite": ["channel_events_raw", "channel_ingest_job"],
        "must_not_conclude": ["customer_churn", "tz_mismatch"],
        "distractors": ["A competitor launched a campaign in the same week.", "Overall site traffic genuinely dipped 4 percent."],
        "why_this_is_hard": "Zero is the most confidently misread number in analytics. Nothing about the metric itself distinguishes 'nobody came' from 'nobody counted'.",
    },
    {
        "case_id": "MTI-002",
        "title": "Traffic up, conversion collapsed",
        "symptom": "Sessions rose 38 percent week over week while the signup conversion rate fell by roughly a third. Absolute signups were flat.",
        "difficulty": "medium",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "bot_traffic_mixed",
            "explanation": "Automated internal traffic entered the sessions table without a flag separating it from human visits, inflating the denominator only.",
        },
        "seed": [{"op": "inject_bot_traffic", "target": "web_sessions", "params": {"share": 0.28, "unflagged": True}}],
        "must_cite": ["web_sessions", "signup_conversion_daily"],
        "must_not_conclude": ["customer_churn", "definition_change"],
        "distractors": ["A pricing page redesign shipped the same week."],
        "why_this_is_hard": "Absolute signups being flat is the tell: a real conversion collapse moves the numerator. Only the denominator moved.",
    },
    {
        "case_id": "MTI-003",
        "title": "Daily totals exceed the weekly rollup",
        # Measured against the seeded warehouse, not estimated. The affected week
        # runs 1194 daily against 895 weekly, so the excess is a third and not the
        # 9 percent this line used to claim, and naming the week gives the
        # investigation somewhere to start. The generator had drifted from the
        # committed JSON here: regenerating the corpus silently replaced a correct
        # symptom with a wrong one.
        "symptom": "For the week of 2026-06-22, summing the daily signup table gives about a third more signups than the weekly rollup reports for the same week.",
        "difficulty": "medium",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "tz_mismatch",
            "explanation": "The daily job buckets on local time while the rollup buckets on UTC, so events near midnight fall into both days.",
        },
        "seed": [{"op": "shift_timezone", "target": "events_daily", "params": {"column": "occurred_at", "applied_to": "daily_only"}}],
        "must_cite": ["events_daily", "events_weekly_rollup"],
        "must_not_conclude": ["bot_traffic_mixed", "collector_dead"],
        "distractors": ["A genuine marketing campaign started mid-window."],
        "why_this_is_hard": "Both tables are correct in isolation. The defect exists only in the relationship between them, which is exactly what lineage is for.",
    },
    {
        "case_id": "MTI-004",
        "title": "The freshness check never fires",
        "symptom": "A nightly table is reported fresh every morning, but downstream consumers periodically see day-old numbers.",
        "difficulty": "hard",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "freshness_window_too_wide",
            "explanation": "The freshness assertion tolerates a window wider than the refresh cadence, so a single skipped run cannot violate it.",
        },
        "seed": [{"op": "widen_freshness_window", "target": "nightly_orders_snapshot", "params": {"cadence_hours": 24, "window_hours": 36}}],
        "must_cite": ["nightly_orders_snapshot", "nightly_orders_freshness_assertion"],
        "must_not_conclude": ["collector_dead", "upstream_schema_change"],
        "distractors": ["The upstream job's runtime genuinely grew by 20 minutes."],
        "why_this_is_hard": "The monitor is green and the data is stale at the same time. A detector whose window exceeds the cadence cannot observe a single-period death.",
    },
    {
        "case_id": "MTI-005",
        "title": "Shipped feature, zero usage",
        # A symptom has to name something the seeded data contains. This one used
        # to describe "a new recommendation module released on the 3rd", and the
        # warehouse holds price_alerts shipped on day 20 and quick_reorder on day
        # 12, with no recommendation feature and no day-3 deploy anywhere in the
        # release log. A run went looking for the feature the symptom named,
        # correctly reported the release log contains no such thing, and
        # concluded the zero was real usage. It was right about the data and the
        # case was wrong, so the scorer recorded a sound investigation as a miss.
        "symptom": "The price alerts feature shipped on day 20 and the deploy is confirmed in the release log, but its usage metric has never left zero.",
        "difficulty": "medium",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "below_fold_unreachable",
            "explanation": "The feature shipped but renders outside the first viewport, so almost nobody reaches it. Deployment was verified; reachability never was.",
        },
        "seed": [{"op": "repoint_detector", "target": "feature_usage_events", "params": {"records": "deploy_marker_only"}}],
        "must_cite": ["feature_usage_events", "release_deploy_log"],
        "must_not_conclude": ["collector_dead", "customer_churn"],
        "distractors": ["The release note mentions a partial rollout flag."],
        "why_this_is_hard": "The deploy check passes, so the team concludes the feature is live and users are ignoring it. Shipped and reachable are different claims.",
    },
    {
        "case_id": "MTI-006",
        "title": "The anomaly detector has reported nothing for a month",
        # Says which pipeline. Without that the symptom named no subject area at
        # all, and a run investigated the signup assets instead: it reached the
        # right root cause by general reasoning about silent detectors and cited
        # signup_conversion_daily, channel_events_raw and web_sessions, touching
        # none of the three orders assets in must_cite. Citation recall 0.0 on a
        # correct answer, which the scorer correctly recorded as a lucky guess.
        # Naming the pipeline gives up nothing: which of the two orders tables the
        # detector actually watches is the question, and that is what separates
        # detector_misaimed from a real data problem.
        "symptom": "A data quality detector on the orders pipeline has raised no alerts in 31 days, across a period that included two known incidents.",
        "difficulty": "hard",
        "ground_truth": {
            "verdict": "instrument_failure",
            "root_cause_key": "detector_misaimed",
            "explanation": "The detector is pointed at a table that is no longer written to. Its silence measures its own irrelevance, not the health of the data.",
        },
        "seed": [{"op": "repoint_detector", "target": "orders_quality_monitor", "params": {"to": "orders_legacy_deprecated"}}],
        "must_cite": ["orders_quality_monitor", "orders_legacy_deprecated", "orders_current"],
        "must_not_conclude": ["freshness_window_too_wide", "customer_behavior"],
        "distractors": ["The detector's own logs show healthy runs every hour."],
        "why_this_is_hard": "A green detector and a dead detector produce identical output. Auditing a detector means checking what it points at, not whether it runs.",
    },
    {
        "case_id": "MTI-007",
        "title": "A join is quietly returning fewer rows",
        "symptom": "A customer enrichment table lost about 12 percent of its rows after a routine schema migration. No job failed.",
        "difficulty": "hard",
        "ground_truth": {
            "verdict": "upstream_data_defect",
            "root_cause_key": "collation_join_break",
            "explanation": "The migration changed the character collation on one join key, so keys differing only by case or width stopped matching. The join succeeds and silently drops rows.",
        },
        "seed": [{"op": "change_collation", "target": "customers_dim", "params": {"column": "customer_code"}}],
        "must_cite": ["customers_dim", "customer_enrichment", "schema_migration_log"],
        "must_not_conclude": ["collector_dead", "customer_churn"],
        "distractors": ["A genuine batch of test accounts was purged the same day."],
        "why_this_is_hard": "Nothing errors. An inner join that matches less is indistinguishable from a world with fewer matches, unless you read the schema history.",
    },
    # ---- Controls -------------------------------------------------------
    # Without these, every answer is "instrument_failure" and an agent that
    # never investigates scores 86 percent. The controls are what give the
    # headline accuracy any meaning: they punish the lazy prior, and they are
    # the cases where the correct move is to clear the instruments and say the
    # customers really did leave.
    {
        "case_id": "MTI-008",
        "title": "Signups fell after a price change",
        "symptom": "Signups fell 24 percent the week after a pricing page update and have not recovered.",
        "difficulty": "medium",
        "ground_truth": {
            "verdict": "customer_behavior",
            "root_cause_key": "price_sensitivity",
            "explanation": "Collection is intact end to end. Visits held steady, the funnel's earlier steps are unchanged, and the drop is concentrated at the pricing step. Customers saw the new price and left.",
        },
        "seed": [{"op": "drop_rows", "target": "signups", "params": {"share": 0.24, "from_day": 8, "instruments": "healthy"}}],
        "must_cite": ["signups", "web_sessions", "pricing_page_events"],
        "must_not_conclude": ["collector_dead", "bot_traffic_mixed", "tz_mismatch"],
        "distractors": ["An unrelated schema migration ran the same week.", "One collector logged a transient warning."],
        "why_this_is_hard": "Every instrument-failure signature the agent has learned is absent here. It must be willing to conclude that nothing is broken.",
    },
    {
        "case_id": "MTI-009",
        "title": "Active users jumped overnight with no release",
        "symptom": "The active user count rose 31 percent between two consecutive days. No deploy, no campaign, no traffic change.",
        "difficulty": "medium",
        "ground_truth": {
            "verdict": "definition_change",
            "root_cause_key": "metric_definition_widened",
            "explanation": "The definition of an active user was widened to include a passive event type. Behaviour and collection are both unchanged; the question being asked changed.",
        },
        "seed": [{"op": "rename_field", "target": "active_users_daily", "params": {"definition": "widened", "added_event": "passive_view"}}],
        "must_cite": ["active_users_daily", "metric_definition_history"],
        "must_not_conclude": ["bot_traffic_mixed", "price_sensitivity"],
        "distractors": ["A bot-filtering rule was also edited that month."],
        "why_this_is_hard": "It looks exactly like bot contamination: a denominator-free jump with no behavioural cause. The distinguishing evidence lives in the metric's own change history.",
    },
    {
        "case_id": "MTI-010",
        "title": "Weekend orders down, weekdays normal",
        "symptom": "Weekend order volume has run 18 percent below the trailing average for three consecutive weekends. Weekdays are unaffected.",
        "difficulty": "easy",
        "ground_truth": {
            "verdict": "customer_behavior",
            "root_cause_key": "seasonal_demand_shift",
            "explanation": "A seasonal shift in weekend demand. Pipelines run on the same schedule all week and show no weekend-specific defect.",
        },
        "seed": [{"op": "drop_rows", "target": "orders", "params": {"share": 0.18, "weekends_only": True, "instruments": "healthy"}}],
        "must_cite": ["orders", "orders_ingest_job"],
        "must_not_conclude": ["collector_dead", "freshness_window_too_wide"],
        "distractors": ["The ingest job's weekend runtime is genuinely shorter because volume is lower."],
        "why_this_is_hard": "A day-of-week pattern is the classic fingerprint of a scheduled job defect, so the obvious hypothesis is wrong here.",
    },
    {
        "case_id": "MTI-011",
        "title": "Checkout completion dropped for one browser",
        "symptom": "Checkout completion fell 40 percent among users of one browser version. Other browsers are flat.",
        "difficulty": "hard",
        "ground_truth": {
            "verdict": "customer_behavior",
            "root_cause_key": "browser_specific_product_defect",
            "explanation": "A real defect in the product surface for that browser version stopped customers completing. Measurement is intact; the customers genuinely could not finish.",
        },
        "seed": [{"op": "drop_rows", "target": "checkout_completions", "params": {"segment": "browser_version", "share": 0.40, "instruments": "healthy"}}],
        "must_cite": ["checkout_completions", "checkout_events", "web_sessions"],
        "must_not_conclude": ["collector_dead", "bot_traffic_mixed", "below_fold_unreachable"],
        "distractors": ["That browser version also changed its user agent string, which looks like a tracking break."],
        "why_this_is_hard": "A segment-specific collapse with a coincident user-agent change is the strongest possible false signal for an instrument failure. The evidence that settles it is that the events still arrive, they just end earlier in the funnel.",
    },
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("cases"))
    # provenance names the internal incident a case was abstracted from, so it
    # stays out of the corpus this repository publishes. Opt in when building a
    # private copy for our own traceability.
    ap.add_argument("--with-provenance", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    seen_causes = set()
    for c in CASES:
        key = c["ground_truth"]["root_cause_key"]
        # Two cases sharing a root cause would let the agent pattern-match
        # instead of discriminate, so the corpus enforces distinctness.
        assert key not in seen_causes, f"duplicate root cause: {key}"
        seen_causes.add(key)

        doc = {
            "case_id": c["case_id"],
            "title": c["title"],
            "symptom": c["symptom"],
            "difficulty": c["difficulty"],
            "seed": {"operations": c["seed"]},
            "ground_truth": c["ground_truth"],
            "must_cite": c["must_cite"],
            "must_not_conclude": c["must_not_conclude"],
            "distractors": c["distractors"],
        }
        if args.with_provenance:
            doc["provenance"] = {"derived_from": "internal-incident-anonymized", "anonymized": True}
        path = args.out / f"{c['case_id']}.json"
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path}  [{key}, {c['difficulty']}]")

    verdicts = {}
    for c in CASES:
        verdicts[c["ground_truth"]["verdict"]] = verdicts.get(c["ground_truth"]["verdict"], 0) + 1
    print(f"\n{len(CASES)} cases, {len(seen_causes)} distinct root causes")
    print("verdict distribution:", verdicts)
    print("difficulty:", {d: sum(1 for c in CASES if c['difficulty'] == d) for d in ('easy', 'medium', 'hard')})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
