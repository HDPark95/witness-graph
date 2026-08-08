#!/usr/bin/env python3
"""Build the synthetic warehouse a case is investigated against, and reproduce
that case's incident inside it.

The catalog says what an asset is; this says what the rows actually did. Both
are needed, because the distinction the whole project turns on — customers
changed versus the instrument broke — is only decidable when you can look at
the numbers and at the thing that produced them.

Every case gets its own database file, built deterministically from a fixed
seed, so a run is reproducible and two cases cannot contaminate each other.
Derived tables are SQL views: an incident seeded in a base table propagates the
way it would in a real warehouse, and the view definition itself becomes
evidence an investigator can read.

    python3 harness/warehouse.py --case cases/MTI-003.json --out warehouse/
    python3 harness/warehouse.py --self-check
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sqlite3

DAYS = 36
CHANNELS = ["organic", "affiliate", "paid_social", "referral"]
BROWSERS = ["chrome-126", "chrome-127", "safari-17", "firefox-128"]
FEATURES = ["quick_reorder", "saved_carts", "price_alerts"]
SEED = 20260806

# Local time is UTC+9. The daily and weekly models disagree about this in
# exactly one case, and that disagreement is the incident.
LOCAL_OFFSET_HOURS = 9

BASE_DDL = """
CREATE TABLE web_sessions (
  session_id    TEXT PRIMARY KEY,
  day           INTEGER NOT NULL,
  channel       TEXT NOT NULL,
  is_automated  INTEGER NOT NULL DEFAULT 0,
  browser_version TEXT NOT NULL,
  customer_code TEXT NOT NULL
);
CREATE TABLE channel_events_raw (
  event_id       TEXT PRIMARY KEY,
  occurred_at    TEXT NOT NULL,          -- ISO8601 in UTC
  day            INTEGER NOT NULL,       -- UTC day, kept for convenience
  channel        TEXT NOT NULL,
  customer_code  TEXT NOT NULL
);
CREATE TABLE orders (
  order_id      TEXT PRIMARY KEY,
  day           INTEGER NOT NULL,
  is_weekend    INTEGER NOT NULL,
  amount        REAL NOT NULL,
  customer_code TEXT NOT NULL
);
CREATE TABLE orders_legacy_deprecated (
  order_id TEXT PRIMARY KEY,
  day      INTEGER NOT NULL
);
CREATE TABLE customers_dim (
  customer_code TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  segment       TEXT NOT NULL
);
CREATE TABLE checkout_events (
  event_id        TEXT PRIMARY KEY,
  day             INTEGER NOT NULL,
  step            TEXT NOT NULL,          -- started | payment | completed
  browser_version TEXT NOT NULL,
  customer_code   TEXT NOT NULL
);
CREATE TABLE pricing_page_events (
  event_id TEXT PRIMARY KEY,
  day      INTEGER NOT NULL,
  action   TEXT NOT NULL                  -- view | compare | start_signup
);
CREATE TABLE feature_usage_events (
  event_id TEXT PRIMARY KEY,
  day      INTEGER NOT NULL,
  feature  TEXT NOT NULL,
  source   TEXT NOT NULL                  -- client_event | deploy_marker
);
CREATE TABLE release_deploy_log (
  deploy_id TEXT PRIMARY KEY,
  day       INTEGER NOT NULL,
  feature   TEXT NOT NULL,
  shipped   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE schema_migration_log (
  migration_id TEXT PRIMARY KEY,
  day          INTEGER NOT NULL,
  statement    TEXT NOT NULL
);
CREATE TABLE metric_definition_history (
  version    INTEGER PRIMARY KEY,
  day        INTEGER NOT NULL,
  metric     TEXT NOT NULL,
  definition TEXT NOT NULL
);
CREATE TABLE nightly_orders_snapshot (
  day     INTEGER PRIMARY KEY,
  orders  INTEGER NOT NULL,
  revenue REAL NOT NULL,
  written_at_day INTEGER NOT NULL      -- which run wrote this row
);
CREATE TABLE ingest_job_runs (
  run_id     TEXT PRIMARY KEY,
  job        TEXT NOT NULL,
  day        INTEGER NOT NULL,
  status     TEXT NOT NULL,               -- ok | missing
  rows_written INTEGER NOT NULL
);
"""


def _utc_iso(day: int, hour: int) -> str:
    """Day 1 is 2026-06-01. Hours are UTC."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(days=day - 1, hours=hour)
    return base.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_weekend(day: int) -> int:
    # Day 1 is a Monday, so days 6 and 7 of each week are the weekend.
    return 1 if (day - 1) % 7 >= 5 else 0


def seed_base_rows(conn: sqlite3.Connection) -> None:
    """Deterministic baseline traffic. No incident yet."""
    rng = random.Random(SEED)
    cur = conn.cursor()

    customers = [f"CUST-{i:04d}" for i in range(1, 401)]
    cur.executemany(
        "INSERT INTO customers_dim VALUES (?,?,?)",
        [(c, f"Account {c[-4:]}", rng.choice(["smb", "mid", "enterprise"])) for c in customers],
    )

    sessions, events, orders, checkouts = [], [], [], []
    pricing, usage, deploys = [], [], []

    for day in range(1, DAYS + 1):
        weekend = _is_weekend(day)
        for channel in CHANNELS:
            # Sessions per channel per day, steady with mild variation.
            n_sessions = rng.randint(55, 75) if not weekend else rng.randint(40, 55)
            for i in range(n_sessions):
                cust = rng.choice(customers)
                sessions.append((
                    f"S-{day:02d}-{channel}-{i:04d}", day, channel, 0,
                    rng.choice(BROWSERS), cust,
                ))
            # Signups grow slowly over the window. Without a trend, a bucketing
            # disagreement just shuffles events between adjacent buckets and
            # cancels out; with one, the shifted window consistently catches
            # more traffic, which is what makes the discrepancy legible.
            growth = 1.0 + 0.025 * day
            base = rng.randint(18, 26) if not weekend else rng.randint(12, 18)
            n_events = int(base * growth)
            for i in range(n_events):
                # Most signups land in the local evening, which in UTC is late
                # in the day. That is the traffic a nine-hour shift relocates.
                hour = rng.choices(
                    population=list(range(24)),
                    weights=[14 if h >= 18 else 1 for h in range(24)],
                )[0]
                events.append((
                    f"E-{day:02d}-{channel}-{i:04d}", _utc_iso(day, hour), day, channel,
                    rng.choice(customers),
                ))

        n_orders = rng.randint(70, 90) if not weekend else rng.randint(45, 60)
        for i in range(n_orders):
            orders.append((
                f"O-{day:02d}-{i:04d}", day, weekend,
                round(rng.uniform(18.0, 240.0), 2), rng.choice(customers),
            ))

        for i in range(rng.randint(90, 120)):
            browser = rng.choice(BROWSERS)
            cust = rng.choice(customers)
            checkouts.append((f"C-{day:02d}-{i:04d}-s", day, "started", browser, cust))
            if rng.random() < 0.78:
                checkouts.append((f"C-{day:02d}-{i:04d}-p", day, "payment", browser, cust))
                if rng.random() < 0.85:
                    checkouts.append((f"C-{day:02d}-{i:04d}-c", day, "completed", browser, cust))

        for i in range(rng.randint(40, 60)):
            pricing.append((f"P-{day:02d}-{i:04d}", day,
                            rng.choices(["view", "compare", "start_signup"], [6, 3, 1])[0]))

    # One feature ships on day 12 and is genuinely used afterwards. A second
    # ships on day 20. Cases that need a shipped-but-unused feature remove the
    # client events and leave the deploy marker.
    deploys.append(("D-0012", 12, "quick_reorder", 1))
    deploys.append(("D-0020", 20, "price_alerts", 1))
    for day in range(12, DAYS + 1):
        for i in range(rng.randint(25, 40)):
            usage.append((f"U-{day:02d}-{i:04d}", day, "quick_reorder", "client_event"))
    for day in range(20, DAYS + 1):
        for i in range(rng.randint(15, 25)):
            usage.append((f"UP-{day:02d}-{i:04d}", day, "price_alerts", "client_event"))

    cur.executemany("INSERT INTO web_sessions VALUES (?,?,?,?,?,?)", sessions)
    cur.executemany("INSERT INTO channel_events_raw VALUES (?,?,?,?,?)", events)
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO checkout_events VALUES (?,?,?,?,?)", checkouts)
    cur.executemany("INSERT INTO pricing_page_events VALUES (?,?,?)", pricing)
    cur.executemany("INSERT INTO feature_usage_events VALUES (?,?,?,?)", usage)
    cur.executemany("INSERT INTO release_deploy_log VALUES (?,?,?,?)", deploys)

    # The legacy orders table stopped being written to on day 3. A monitor
    # still pointed at it will look healthy and see nothing.
    cur.executemany(
        "INSERT INTO orders_legacy_deprecated VALUES (?,?)",
        [(f"OL-{d:02d}-{i:03d}", d) for d in range(1, 4) for i in range(20)],
    )

    cur.execute(
        "INSERT INTO metric_definition_history VALUES (1, 1, 'active_users_daily',"
        " 'distinct customers with at least one non-automated session')"
    )
    cur.execute(
        "INSERT INTO schema_migration_log VALUES ('M-0001', 2,"
        " 'CREATE INDEX idx_orders_customer ON orders(customer_code)')"
    )

    # Every ingest job reports a healthy run every day, until a case says
    # otherwise. This is the signal that lets `instruments: healthy` cases be
    # decided as customer behaviour rather than breakage.
    runs = []
    for day in range(1, DAYS + 1):
        for job, table in (("channel_ingest_job", "channel_events_raw"),
                           ("orders_ingest_job", "orders"),
                           ("nightly_snapshot_job", "orders")):
            runs.append((f"R-{job}-{day:02d}", job, day, "ok", 0))
    cur.executemany("INSERT INTO ingest_job_runs VALUES (?,?,?,?,?)", runs)
    conn.commit()


def build_views(conn: sqlite3.Connection, *, daily_uses_local_time: bool,
                active_users_definition: str) -> None:
    """Derived models. The view text is itself evidence, so it stays readable."""
    bucket = (
        f"date(occurred_at, '+{LOCAL_OFFSET_HOURS} hours')"
        if daily_uses_local_time else "date(occurred_at)"
    )
    cur = conn.cursor()
    cur.executescript(f"""
    DROP VIEW IF EXISTS events_daily;
    CREATE VIEW events_daily AS
      SELECT {bucket} AS bucket_date, channel, COUNT(*) AS events
      FROM channel_events_raw GROUP BY bucket_date, channel;

    -- The weekly model always buckets on UTC. When the daily model does not,
    -- summing seven days does not equal the week.
    DROP VIEW IF EXISTS events_weekly_rollup;
    CREATE VIEW events_weekly_rollup AS
      SELECT strftime('%Y-W%W', occurred_at) AS bucket_week, channel, COUNT(*) AS events
      FROM channel_events_raw GROUP BY bucket_week, channel;

    DROP VIEW IF EXISTS signups;
    CREATE VIEW signups AS
      SELECT bucket_date AS day, channel, events AS signups FROM events_daily;

    DROP VIEW IF EXISTS orders_current;
    CREATE VIEW orders_current AS SELECT * FROM orders;


    DROP VIEW IF EXISTS customer_enrichment;
    CREATE VIEW customer_enrichment AS
      SELECT c.customer_code, c.display_name, c.segment, COUNT(o.order_id) AS orders
      FROM customers_dim c JOIN orders_current o ON o.customer_code = c.customer_code
      GROUP BY c.customer_code;

    DROP VIEW IF EXISTS checkout_completions;
    CREATE VIEW checkout_completions AS
      SELECT day, browser_version, COUNT(*) AS completions
      FROM checkout_events WHERE step = 'completed'
      GROUP BY day, browser_version;

    DROP VIEW IF EXISTS feature_adoption;
    CREATE VIEW feature_adoption AS
      SELECT d.feature, d.day AS shipped_day,
             (SELECT COUNT(*) FROM feature_usage_events u
               WHERE u.feature = d.feature AND u.source = 'client_event') AS client_events
      FROM release_deploy_log d;

    DROP VIEW IF EXISTS signup_conversion_daily;
    CREATE VIEW signup_conversion_daily AS
      SELECT s.day AS bucket_date,
             CAST(julianday(s.day) - julianday('2026-06-01') + 1 AS INTEGER) AS day,
             SUM(s.signups) AS signups,
             (SELECT COUNT(*) FROM web_sessions w
               WHERE w.day = CAST(julianday(s.day) - julianday('2026-06-01') + 1 AS INTEGER)
             ) AS sessions
      FROM signups s GROUP BY s.day;
    """)

    if active_users_definition == "widened":
        cur.executescript("""
        DROP VIEW IF EXISTS active_users_daily;
        CREATE VIEW active_users_daily AS
          SELECT day, COUNT(DISTINCT customer_code) AS active_users
          FROM web_sessions GROUP BY day;
        """)
    else:
        cur.executescript("""
        DROP VIEW IF EXISTS active_users_daily;
        CREATE VIEW active_users_daily AS
          SELECT day, COUNT(DISTINCT customer_code) AS active_users
          FROM web_sessions WHERE is_automated = 0 GROUP BY day;
        """)
    conn.commit()


def apply_operations(conn: sqlite3.Connection, ops: list[dict]) -> dict:
    """Reproduce the incident. Returns the view flags the case implies."""
    rng = random.Random(SEED + 1)
    cur = conn.cursor()
    flags = {"daily_uses_local_time": False, "active_users_definition": "original",
             "snapshot_stall_from": None}

    for op in ops:
        kind, params = op["op"], op.get("params", {})

        if kind == "stop_collector":
            channel, from_day = params["channel"], params["from_day"]
            cur.execute("DELETE FROM channel_events_raw WHERE channel = ? AND day >= ?",
                        (channel, from_day))
            # The job stops producing rows without raising. Silence, not an error,
            # is what makes this hard to see from the metric alone.
            cur.execute(
                "UPDATE ingest_job_runs SET status = 'missing' "
                "WHERE job = 'channel_ingest_job' AND day >= ?", (from_day,))

        elif kind == "inject_bot_traffic":
            share, unflagged = params["share"], params.get("unflagged", True)
            cur.execute("SELECT COUNT(*) FROM web_sessions")
            existing = cur.fetchone()[0]
            n = int(existing * share / (1 - share))
            rows = []
            # Weighted towards the recent end. Spread evenly, the injected
            # traffic gets outgrown by real signups and the conversion rate
            # recovers on its own, which is not what the incident looked like.
            span = list(range(DAYS // 2, DAYS + 1))
            weights = [i + 1 for i in range(len(span))]
            for i in range(n):
                day = rng.choices(span, weights=weights)[0]
                rows.append((f"S-BOT-{i:05d}", day, rng.choice(CHANNELS),
                             0 if unflagged else 1, "chrome-126", "CUST-0001"))
            cur.executemany("INSERT INTO web_sessions VALUES (?,?,?,?,?,?)", rows)

        elif kind == "shift_timezone":
            # Only the daily model moves. The rollup keeps bucketing on UTC.
            flags["daily_uses_local_time"] = params.get("applied_to") == "daily_only"
            # A shift alone is not visible: it moves each bucket's edge traffic
            # into its neighbour, and across steady weeks those trades cancel.
            # What makes a real timezone bug surface is an evening spike sitting
            # on a week boundary — a Sunday-night promotion whose events the
            # rollup counts in one week and the daily model in the next. Day 21
            # is a Sunday, so this lands the spike squarely on the seam.
            spike = []
            for i in range(320):
                hour = 20 + (i % 4)
                spike.append((f"E-SPIKE-{i:04d}", _utc_iso(21, hour), 21,
                              CHANNELS[i % len(CHANNELS)], f"CUST-{(i % 400) + 1:04d}"))
            cur.executemany("INSERT INTO channel_events_raw VALUES (?,?,?,?,?)", spike)

        elif kind == "change_collation":
            column = params["column"]
            cur.execute(
                "INSERT INTO schema_migration_log VALUES ('M-0042', 16,"
                f" 'ALTER TABLE customers_dim ALTER COLUMN {column}"
                " SET COLLATE utf8mb4_0900_as_cs')")
            # The dimension key gains case variance; the fact table keeps the
            # old form, so the join quietly matches fewer rows.
            cur.execute(
                "UPDATE customers_dim SET customer_code = 'cust-' || substr(customer_code, 6) "
                "WHERE CAST(substr(customer_code, 6) AS INTEGER) % 3 = 0")

        elif kind == "rename_field":
            if params.get("definition") == "widened":
                flags["active_users_definition"] = "widened"
                added = params.get("added_event", "passive_view")
                cur.execute(
                    "INSERT INTO metric_definition_history VALUES (2, 24, 'active_users_daily',"
                    f" 'distinct customers with any session, including {added}')")
                # Widening admits passive viewers, who are people the old
                # definition never counted. Reusing existing customer codes
                # would leave a distinct-count metric unmoved.
                cur.execute(
                    "INSERT INTO web_sessions "
                    "SELECT 'S-PV-' || rowid, day, channel, 1, browser_version, "
                    "'PASV-' || printf('%04d', rowid % 400) "
                    "FROM web_sessions WHERE day >= 24 AND rowid % 2 = 0")

        elif kind == "repoint_detector":
            # Catalog-side incidents. The warehouse records the consequence so
            # an investigator can see the absence, not just be told about it.
            if params.get("records") == "deploy_marker_only":
                cur.execute("DELETE FROM feature_usage_events WHERE feature = 'price_alerts'")
                cur.execute("INSERT INTO feature_usage_events VALUES "
                            "('U-MARK-0001', 20, 'price_alerts', 'deploy_marker')")

        elif kind == "drop_rows":
            share = params["share"]
            pct = int(share * 100)
            if params.get("weekends_only"):
                cur.execute(
                    "DELETE FROM orders WHERE is_weekend = 1 AND rowid % 100 < ?", (pct,))
            elif params.get("segment") == "browser_version":
                cur.execute(
                    "DELETE FROM checkout_events WHERE step = 'completed' "
                    "AND browser_version = 'safari-17' AND rowid % 100 < ?", (pct,))
            else:
                from_day = params.get("from_day", 1)
                cur.execute(
                    "DELETE FROM channel_events_raw WHERE day >= ? AND rowid % 100 < ?",
                    (from_day, pct))

        elif kind == "widen_freshness_window":
            # The window itself lives in the catalog. What lives here is the
            # cadence it fails to police: from day 18 the nightly job only
            # lands every other day, so the snapshot is stale half the time
            # while a 36-hour freshness window still reports healthy.
            stalled = params.get("stall_from_day", 18)
            flags["snapshot_stall_from"] = stalled
            cur.execute(
                "UPDATE ingest_job_runs SET status = 'missing' "
                "WHERE job = 'nightly_snapshot_job' AND day >= ? AND day % 2 = 1",
                (stalled,))

        else:
            raise ValueError(f"unknown seed operation: {kind}")

    conn.commit()
    return flags


def populate_snapshot(conn: sqlite3.Connection, stall_from: int | None) -> None:
    """Materialise the nightly snapshot, then let a stalled job leave gaps.

    Written after the incident is applied, because the snapshot is downstream
    of whatever happened to orders.
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM nightly_orders_snapshot")
    cur.execute(
        "INSERT INTO nightly_orders_snapshot "
        "SELECT day, COUNT(*), ROUND(SUM(amount), 2), day FROM orders_current GROUP BY day")
    if stall_from is not None:
        # Every other night the job does not land, so the snapshot keeps
        # yesterday's row. A freshness window wider than the cadence reports
        # this as healthy.
        cur.execute("DELETE FROM nightly_orders_snapshot WHERE day >= ? AND day % 2 = 1",
                    (stall_from,))
    conn.commit()


def build(case: dict, out_dir: pathlib.Path) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{case['case_id']}.db"
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(BASE_DDL)
    seed_base_rows(conn)
    flags = apply_operations(conn, case["seed"]["operations"])
    build_views(conn, daily_uses_local_time=flags["daily_uses_local_time"],
                active_users_definition=flags["active_users_definition"])
    populate_snapshot(conn, flags["snapshot_stall_from"])
    # Row counts change after seeding; keep the job log honest about them.
    conn.execute(
        "UPDATE ingest_job_runs SET rows_written = "
        "(SELECT COUNT(*) FROM channel_events_raw c WHERE c.day = ingest_job_runs.day) "
        "WHERE job = 'channel_ingest_job'")
    conn.commit()
    conn.close()
    return path


FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "attach", "pragma")


def query(db_path: pathlib.Path, sql: str, limit: int = 50) -> dict:
    """Read-only. The investigation observes the warehouse, never edits it."""
    lowered = sql.strip().lower()
    if not lowered.startswith(("select", "with")):
        raise ValueError("only SELECT/WITH statements are allowed")
    if any(f" {word} " in f" {lowered} " for word in FORBIDDEN):
        raise ValueError("write statements are not allowed against the warehouse")
    if ";" in lowered.rstrip(";"):
        raise ValueError("one statement per query")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchmany(limit)
        return {
            "columns": [d[0] for d in (conn.execute(sql).description or [])],
            "rows": [dict(r) for r in rows],
            "truncated_at": limit if len(rows) == limit else None,
        }
    finally:
        conn.close()


def schema_of(db_path: pathlib.Path) -> list[dict]:
    """Tables, views and columns — what the catalog should be told about."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = []
        for name, kind, ddl in conn.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ):
            cols = [
                {"name": r[1], "type": r[2] or "TEXT"}
                for r in conn.execute(f"PRAGMA table_info('{name}')")
            ]
            out.append({"name": name, "kind": kind, "columns": cols, "ddl": ddl})
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("warehouse"))
    ap.add_argument("--all", action="store_true", help="Build every case in cases/")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    cases = []
    if args.all:
        cases = [json.loads(p.read_text(encoding="utf-8"))
                 for p in sorted(pathlib.Path("cases").glob("MTI-*.json"))]
    elif args.case:
        cases = [json.loads(args.case.read_text(encoding="utf-8"))]
    else:
        ap.error("pass --case, --all or --self-check")

    for case in cases:
        path = build(case, args.out)
        counts = query(path, "SELECT COUNT(*) AS n FROM channel_events_raw")["rows"][0]["n"]
        print(f"{case['case_id']}: {path}  channel_events_raw={counts}")
    return 0


def self_check() -> int:
    """Every assertion here is one case's incident actually reproducing.

    A benchmark whose symptom cannot be observed in its own data has nothing
    to find, and an agent that fails it has been failed by the fixture rather
    than by its reasoning. This is the check that keeps that honest.
    """
    out = pathlib.Path("/tmp/wg-warehouse-check")

    def db_for(cid: str) -> pathlib.Path:
        case = json.loads(pathlib.Path(f"cases/{cid}.json").read_text(encoding="utf-8"))
        return build(case, out)

    def one(db: pathlib.Path, sql: str) -> dict:
        return query(db, sql)["rows"][0]

    # MTI-001 collector_dead — one channel goes silent, the others do not, and
    # the job log shows absence rather than failure.
    r = one(db_for("MTI-001"),
            "SELECT (SELECT COUNT(*) FROM channel_events_raw WHERE channel='affiliate' AND day>=14) aff,"
            " (SELECT COUNT(*) FROM channel_events_raw WHERE channel='organic' AND day>=14) org,"
            " (SELECT COUNT(*) FROM ingest_job_runs WHERE job='channel_ingest_job' AND status='missing') miss")
    assert r["aff"] == 0 and r["org"] > 0 and r["miss"] > 0, r

    # MTI-002 bot_traffic_mixed — sessions climb while conversion sinks, and
    # the injected traffic is not flagged as automated.
    db = db_for("MTI-002")
    early = one(db, "SELECT ROUND(100.0*signups/sessions,2) c FROM signup_conversion_daily WHERE day=12")["c"]
    late = one(db, "SELECT ROUND(100.0*signups/sessions,2) c FROM signup_conversion_daily WHERE day=34")["c"]
    vol = one(db, "SELECT (SELECT COUNT(*) FROM web_sessions WHERE day<18) a,"
                  " (SELECT COUNT(*) FROM web_sessions WHERE day>=18) b,"
                  " (SELECT COUNT(*) FROM web_sessions WHERE is_automated=1) flagged")
    assert late < early * 0.7, f"conversion should collapse: {early} -> {late}"
    assert vol["b"] > vol["a"], "traffic should be up, not down"
    assert vol["flagged"] == 0, "the injected traffic must be unflagged, or it is not a trap"

    # MTI-003 tz_mismatch — totals match, but one week disagrees sharply.
    db = db_for("MTI-003")
    tot = one(db, "SELECT (SELECT SUM(events) FROM events_daily) d,"
                  " (SELECT SUM(events) FROM events_weekly_rollup) w")
    assert tot["d"] == tot["w"], "no rows are lost; only their bucket moves"
    weeks = query(db, """
      SELECT wk.bucket_week, ROUND(100.0*(dl.events - wk.events)/wk.events, 1) AS pct
      FROM (SELECT bucket_week, SUM(events) AS events FROM events_weekly_rollup GROUP BY 1) wk
      JOIN (SELECT strftime('%Y-W%W', bucket_date) AS bw, SUM(events) AS events
              FROM events_daily GROUP BY 1) dl ON dl.bw = wk.bucket_week
    """)["rows"]
    assert max(r["pct"] for r in weeks) >= 8.0, weeks

    # MTI-004 freshness_window_too_wide — the snapshot has gaps a 36-hour
    # window cannot see, while the orders underneath it are intact.
    db = db_for("MTI-004")
    r = one(db, "SELECT (SELECT COUNT(*) FROM nightly_orders_snapshot) snap_days,"
                " (SELECT COUNT(DISTINCT day) FROM orders_current) order_days")
    assert r["snap_days"] < r["order_days"], f"the snapshot must stall: {r}"

    # MTI-005 below_fold_unreachable — shipped, and no client ever emitted.
    r = one(db_for("MTI-005"),
            "SELECT (SELECT COUNT(*) FROM release_deploy_log WHERE feature='price_alerts') shipped,"
            " (SELECT COUNT(*) FROM feature_usage_events WHERE feature='price_alerts'"
            "   AND source='client_event') used")
    assert r["shipped"] == 1 and r["used"] == 0, r

    # MTI-006 detector_misaimed — the monitored table stopped being written
    # long before the metric moved.
    r = one(db_for("MTI-006"),
            "SELECT (SELECT MAX(day) FROM orders_legacy_deprecated) legacy_last,"
            " (SELECT MAX(day) FROM orders_current) current_last")
    assert r["legacy_last"] < r["current_last"] - 20, r

    # MTI-007 collation_join_break — the join quietly loses rows.
    r = one(db_for("MTI-007"),
            "SELECT (SELECT COUNT(*) FROM customers_dim) dim,"
            " (SELECT COUNT(*) FROM customer_enrichment) joined,"
            " (SELECT COUNT(*) FROM schema_migration_log WHERE statement LIKE '%COLLATE%') mig")
    assert r["joined"] < r["dim"] * 0.9 and r["mig"] == 1, r

    # MTI-008 price_sensitivity — signups fall while every instrument is fine.
    db = db_for("MTI-008")
    r = one(db, "SELECT (SELECT COUNT(*) FROM channel_events_raw WHERE day BETWEEN 1 AND 7) before,"
                " (SELECT COUNT(*) FROM channel_events_raw WHERE day BETWEEN 8 AND 14) after,"
                " (SELECT COUNT(*) FROM ingest_job_runs WHERE job='channel_ingest_job'"
                "   AND status='ok') ok_runs")
    assert r["after"] < r["before"], f"signups must fall: {r}"
    assert r["ok_runs"] == DAYS, "a behaviour case needs the instrument to look healthy"

    # MTI-009 metric_definition_widened — the count jumps where the definition
    # changed, and the history records the change.
    db = db_for("MTI-009")
    r = one(db, "SELECT (SELECT active_users FROM active_users_daily WHERE day=23) before,"
                " (SELECT active_users FROM active_users_daily WHERE day=25) after,"
                " (SELECT COUNT(*) FROM metric_definition_history) versions")
    assert r["after"] > r["before"] * 1.3 and r["versions"] == 2, r

    # MTI-010 seasonal_demand_shift — weekends down, weekdays untouched,
    # ingestion healthy throughout.
    db = db_for("MTI-010")
    r = one(db, "SELECT (SELECT COUNT(*) FROM orders WHERE is_weekend=1) wknd,"
                " (SELECT COUNT(*) FROM orders WHERE is_weekend=0) wkday,"
                " (SELECT COUNT(*) FROM ingest_job_runs WHERE job='orders_ingest_job'"
                "   AND status='ok') ok_runs")
    # Ten weekend days against twenty-six weekdays; scale before comparing.
    assert r["wknd"] / 10 < (r["wkday"] / 26) * 0.75, r
    assert r["ok_runs"] == DAYS, "a behaviour case needs the instrument to look healthy"

    # MTI-011 browser_specific_product_defect — one browser segment drops.
    r = one(db_for("MTI-011"),
            "SELECT (SELECT SUM(completions) FROM checkout_completions"
            "   WHERE browser_version='safari-17') safari,"
            " (SELECT SUM(completions) FROM checkout_completions"
            "   WHERE browser_version='chrome-126') chrome")
    assert r["safari"] < r["chrome"] * 0.8, r

    # And the warehouse is observable but never writable.
    try:
        query(db_for("MTI-001"), "DELETE FROM orders")
        raise AssertionError("a write must be refused")
    except ValueError:
        pass

    print("warehouse self-check ok — 11 incidents reproduce, warehouse is read-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
