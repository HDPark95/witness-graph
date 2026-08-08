#!/usr/bin/env python3
"""Render a run into a single self-contained HTML page.

The ledger already holds everything a viewer needs: node, status, tool calls,
effects, cost, contract violations. So visibility is not a missing capability,
it is a missing projection. This is that projection.

Deliberately dependency-free and single-file: it has to open from a file:// URL
on a judge's laptop and it has to screen-record cleanly.

    python3 harness/render_run.py --graph graphs/metric-truth.yaml \
        --ledger runs/MTI-004.jsonl --case cases/MTI-004.json --out run.html
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib

import yaml

STATUS_COLOR = {
    "ok": "ok",
    "failed": "bad",
    "gate_failed": "bad",
    "retried": "warn",
    "skipped": "muted",
    "aborted": "bad",
}


def load_jsonl(p: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def esc(x) -> str:
    return html.escape(str(x))


def build(graph: dict, ledger: list[dict], case: dict | None) -> str:
    by_node: dict[str, list[dict]] = {}
    for r in ledger:
        by_node.setdefault(r["node_id"], []).append(r)

    # --- findings the viewer should not have to hunt for --------------------
    witnesses = []
    for r in ledger:
        out = r.get("output")
        if r["node_id"] == "gather_witnesses" and r.get("status") == "ok" and out:
            witnesses.extend(out if isinstance(out, list) else [out])
    witness_ids = {w["id"] for w in witnesses if isinstance(w, dict) and "id" in w}

    verdict = None
    for r in ledger:
        if r["node_id"] == "adjudicate" and r.get("status") == "ok":
            verdict = r.get("output")

    cited = set((verdict or {}).get("cited_witnesses", []))
    dangling = sorted(cited - witness_ids)
    unapproved = [e for r in ledger for e in r.get("effects", []) if e.get("approved_by") is None]
    disallowed = [c for r in ledger for c in r.get("tool_calls", []) if c.get("allowed") is False]

    alerts = []
    if dangling:
        alerts.append(("bad", "Fabricated citation",
                       f"The verdict cites {', '.join(dangling)}, absent from the witness ledger"))
    if unapproved:
        alerts.append(("bad", "Unapproved effect",
                       f"{len(unapproved)} action(s) changed something outside the run without approval"))
    if disallowed:
        alerts.append(("bad", "Allowlist violation",
                       f"{len(disallowed)} call(s) to a tool this node never declared"))
    if not any(w.get("refutes") for w in witnesses if isinstance(w, dict)):
        alerts.append(("warn", "Nothing refuted",
                       "No hypothesis was argued against. That is a confirmation-bias signal"))
    if not alerts:
        alerts.append(("ok", "No violations",
                       "Fabricated citations, unapproved effects and tool violations are all zero"))

    # --- nodes -------------------------------------------------------------
    rows = []
    for n in graph["nodes"]:
        nid = n["id"]
        recs = by_node.get(nid, [])
        if not recs:
            state, cls = "not reached", "muted"
        else:
            statuses = [r.get("status") for r in recs]
            state = "gate_failed" if "gate_failed" in statuses else ("failed" if "failed" in statuses else "ok")
            cls = STATUS_COLOR.get(state, "muted")
            state = {"ok": "passed", "failed": "failed", "gate_failed": "blocked by gate"}[state]
        eff = n.get("effects", ["read"])
        writes = "write" in eff or "notify" in eff
        ms = sum(r.get("cost", {}).get("wall_ms", 0) for r in recs)
        tools = sum(len(r.get("tool_calls", [])) for r in recs)
        rows.append(f"""<tr>
<td><span class="dot {cls}"></span><b>{esc(nid)}</b>{' <span class="w">write</span>' if writes else ''}</td>
<td class="t">{esc(n['type'])}</td>
<td class="{cls}">{esc(state)}</td>
<td class="num">{len(recs)}</td>
<td class="num">{tools}</td>
<td class="num">{ms or ''}</td>
</tr>""")

    wrows = []
    for w in witnesses:
        if not isinstance(w, dict):
            continue
        src = w.get("source", {})
        ref = src.get("ref", "")
        wrows.append(f"""<tr>
<td class="num">{esc(w.get('id',''))}</td>
<td>{esc(w.get('claim',''))}</td>
<td class="t">{esc(src.get('kind',''))}<br><span class="ref">{esc(ref[:78])}</span></td>
<td class="num">{esc(', '.join(w.get('supports',[])) or '-')}</td>
<td class="num">{esc(', '.join(w.get('refutes',[])) or '-')}</td>
</tr>""")

    alert_html = "".join(
        f'<div class="alert {c}"><b>{esc(t)}</b><span>{esc(m)}</span></div>' for c, t, m in alerts
    )

    v = verdict or {}
    truth = (case or {}).get("ground_truth", {})
    correct = v.get("root_cause_key") == truth.get("root_cause_key") if (v and truth) else None
    verdict_html = f"""
<div class="grid">
  <div class="card"><div class="k">Agent verdict</div><div class="v">{esc(v.get('verdict','-'))}</div>
    <div class="n">{esc(v.get('root_cause_key','-'))}</div></div>
  <div class="card"><div class="k">Answer key</div><div class="v">{esc(truth.get('verdict','-'))}</div>
    <div class="n">{esc(truth.get('root_cause_key','-'))}</div></div>
  <div class="card"><div class="k">Outcome</div>
    <div class="v {'ok' if correct else 'bad' if correct is False else ''}">{'correct' if correct else 'wrong' if correct is False else '-'}</div>
    <div class="n">{len(cited & witness_ids)} of {len(cited)} citations resolve</div></div>
</div>""" if verdict or case else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Witness Graph run · {esc((case or {}).get('case_id','run'))}</title>
<style>
:root{{--bg:#fff;--fg:#16181d;--mut:#666;--line:#e4e7eb;--soft:#f7f8fa;
--ok:#1a7048;--bad:#b3253a;--warn:#a8541b;--mono:ui-monospace,Menlo,Consolas,monospace}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1115;--fg:#e6e8ec;--mut:#98a0ad;--line:#262a32;
--soft:#161920;--ok:#5cc98e;--bad:#f0798c;--warn:#e0a06a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.w{{max-width:1000px;margin:0 auto;padding:40px 20px 80px}}
h1{{font-size:24px;margin:0 0 4px;letter-spacing:-.02em}}
h2{{font-size:17px;margin:38px 0 10px;padding-top:18px;border-top:1px solid var(--line)}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:24px}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:9px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:var(--soft);font-weight:700;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
.num{{font-family:var(--mono);white-space:nowrap}}
.t{{color:var(--mut);font-size:12.5px}}
.ref{{font-family:var(--mono);font-size:11px;color:var(--mut);word-break:break-all}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;background:var(--mut)}}
.dot.ok{{background:var(--ok)}}.dot.bad{{background:var(--bad)}}.dot.warn{{background:var(--warn)}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.warn{{color:var(--warn)}}.muted{{color:var(--mut)}}
span.w{{font-size:10.5px;border:1px solid var(--warn);color:var(--warn);border-radius:99px;padding:1px 6px;margin-left:6px}}
.alert{{display:flex;gap:12px;align-items:baseline;border:1px solid var(--line);border-left-width:4px;
border-radius:0 8px 8px 0;padding:11px 15px;margin:9px 0;background:var(--soft);font-size:14px}}
.alert.bad{{border-left-color:var(--bad)}}.alert.ok{{border-left-color:var(--ok)}}.alert.warn{{border-left-color:var(--warn)}}
.alert b{{min-width:110px}} .alert span{{color:var(--mut)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:11px;margin:16px 0}}
.card{{border:1px solid var(--line);border-radius:9px;padding:13px 15px;background:var(--soft)}}
.card .k{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:700}}
.card .v{{font-size:18px;font-weight:700;font-family:var(--mono);margin-top:3px}}
.card .n{{font-size:12px;color:var(--mut);font-family:var(--mono)}}
</style></head><body><div class="w">
<h1>{esc((case or {}).get('title','Run'))}</h1>
<div class="sub">{esc((case or {}).get('case_id',''))} · graph {esc(graph.get('name',''))} · {len(ledger)} ledger records</div>

<h2>What this run caught</h2>
{alert_html}

<h2>Adjudication</h2>
{verdict_html}

<h2>Graph execution</h2>
<div class="tw"><table>
<thead><tr><th>Node</th><th>Kind</th><th>State</th><th>Records</th><th>Tools</th><th>ms</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>

<h2>Witness ledger</h2>
<div class="tw"><table>
<thead><tr><th>id</th><th>Claim</th><th>Source</th><th>Supports</th><th>Refutes</th></tr></thead>
<tbody>{''.join(wrows) or '<tr><td colspan=5 class="muted">No witnesses</td></tr>'}</tbody></table></div>

<p class="sub" style="margin-top:30px">This page is built from the run ledger alone. It never reads the agent's own summary of what it did.</p>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph", type=pathlib.Path, required=True)
    ap.add_argument("--ledger", type=pathlib.Path, required=True)
    ap.add_argument("--case", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, required=True)
    a = ap.parse_args()

    graph = yaml.safe_load(a.graph.read_text(encoding="utf-8"))
    ledger = load_jsonl(a.ledger)
    case = json.loads(a.case.read_text(encoding="utf-8")) if a.case else None

    a.out.write_text(build(graph, ledger, case), encoding="utf-8")
    print(f"wrote {a.out}  ({a.out.stat().st_size} bytes, {len(ledger)} ledger records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
