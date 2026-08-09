#!/usr/bin/env python3
"""Build the static site a judge can open without cloning anything.

The hackathon rules ask for "a URL to your Project that will provide easy
access for the judges to test it out". Our DataHub instance is local, so the
thing a judge can actually be handed is the evidence: the run pages, and the
numbers the scorer computed from them.

Every number on the index comes out of `harness/score.py`. None of it is
typed by hand. A submission whose whole argument is "do not take the agent's
word for it, read the ledger" cannot ship a page of hand-copied metrics.

    python3 docs/site/build.py --out _site

Output is dependency-free HTML that opens from a file:// URL, so the same
bytes serve as the demo-video source and as the deployed site.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

import yaml  # noqa: E402
import render_run  # noqa: E402

REPO = "https://github.com/HDPark95/witness-graph"


def esc(x) -> str:
    return html.escape(str(x))


def score(cases: pathlib.Path, runs: pathlib.Path) -> dict:
    """Shell out to the scorer rather than reimplementing it.

    Importing would let this file drift from what `score.py` actually does the
    moment someone edits one and not the other. Running it means the site is
    literally reporting the scorer's output.
    """
    out = subprocess.run(
        [sys.executable, str(ROOT / "harness" / "score.py"),
         "--cases", str(cases), "--runs", str(runs)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


# Only the metrics a judge should read first. The full object is published as
# summary.json next to the index, so nothing is hidden by this shortlist.
HEADLINE = [
    ("verdict_top1", "Verdict accuracy", "Did it name the right kind of failure"),
    ("root_cause_top1", "Root cause accuracy", "Did it name the specific fault"),
    ("root_cause_lift_over_baseline", "Root cause lift", "Over the majority-class baseline"),
    ("citation_precision", "Citation precision", "Cited evidence that exists in the ledger"),
    ("citation_recall", "Citation recall", "Of the evidence the answer key requires"),
    ("lucky_guess_rate", "Lucky guess rate", "Right answer, evidence never looked at"),
    ("unapproved_effects_total", "Unapproved effects", "Writes that skipped the approval gate"),
    ("disallowed_tool_calls_total", "Disallowed tool calls", "Calls the runtime refused and logged"),
]


def fmt(key: str, v) -> str:
    """Signed for lifts, three places for rates, bare for counts."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:+.3f}" if "lift" in key else f"{v:.3f}"
    return str(v)


def index_html(summary: dict, rows: list[dict]) -> str:
    cards = []
    for key, label, note in HEADLINE:
        if key not in summary:
            continue
        v = fmt(key, summary[key])
        cards.append(
            f'<div class="card"><div class="k">{esc(label)}</div>'
            f'<div class="v">{esc(v)}</div><div class="n">{esc(note)}</div></div>'
        )

    trs = []
    for r in rows:
        ok = "ok" if r["cause_correct"] else "bad"
        trs.append(f"""<tr>
<td class="num"><a href="{esc(r['case_id'])}.html">{esc(r['case_id'])}</a></td>
<td class="t">{esc(r['difficulty'])}</td>
<td class="{'ok' if r['verdict_correct'] else 'bad'}">{'correct' if r['verdict_correct'] else 'wrong'}</td>
<td class="{ok}">{'correct' if r['cause_correct'] else 'wrong'}</td>
<td class="num">{r['citation_recall']:.2f}</td>
<td class="num">{'yes' if r['lucky_guess'] else 'no'}</td>
<td class="num">{r['tool_calls']}</td>
</tr>""")

    n = summary.get("cases", len(rows))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Witness Graph &middot; run evidence</title>
<meta name="description" content="A falsifiable agent harness: every claim must cite an evidence node in an append-only ledger.">
<style>
:root{{--bg:#fff;--fg:#16181d;--mut:#666;--line:#e4e7eb;--soft:#f7f8fa;
--ok:#1a7048;--bad:#b3253a;--warn:#a8541b;--mono:ui-monospace,Menlo,Consolas,monospace}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1115;--fg:#e6e8ec;--mut:#98a0ad;--line:#262a32;
--soft:#161920;--ok:#5cc98e;--bad:#f0798c;--warn:#e0a06a}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.w{{max-width:1000px;margin:0 auto;padding:40px 20px 80px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}}
h2{{font-size:17px;margin:38px 0 10px;padding-top:18px;border-top:1px solid var(--line)}}
.sub{{color:var(--mut);font-size:13.5px;margin-bottom:8px}}
.lede{{font-size:16px;margin:18px 0 0;max-width:74ch}}
p{{max-width:74ch}}
a{{color:inherit}}
table{{border-collapse:collapse;width:100%;font-size:13.5px}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:9px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:var(--soft);font-weight:700;white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
.num{{font-family:var(--mono);white-space:nowrap}}
.t{{color:var(--mut);font-size:12.5px}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.warn{{color:var(--warn)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:16px 0}}
.card{{border:1px solid var(--line);border-radius:9px;padding:13px 15px;background:var(--soft)}}
.card .k{{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}}
.card .v{{font-family:var(--mono);font-size:21px;margin:3px 0 2px}}
.card .n{{font-size:12px;color:var(--mut);line-height:1.5}}
pre{{background:var(--soft);border:1px solid var(--line);border-radius:9px;padding:14px 16px;
overflow-x:auto;font-family:var(--mono);font-size:12.5px;line-height:1.65}}
.foot{{margin-top:44px;padding-top:18px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px}}
</style></head><body><div class="w">

<h1>Witness Graph</h1>
<div class="sub">An agent harness where a claim without a citation is not an answer</div>

<p class="lede">A metric moved. The agent has to say whether the business changed or the
instrument broke, name the specific fault, and cite the evidence for it. The scorer
never reads the agent's summary of its own work. It reads the append-only ledger the
runtime wrote while the agent ran.</p>

<h2>What the scorer found</h2>
<p>Every number below is the output of <code>harness/score.py</code> over the
{n} committed run ledger(s), regenerated when this page was built. Nothing here is
typed by hand.</p>
<div class="grid">{''.join(cards)}</div>

<h2>The runs themselves</h2>
<p>Each row opens the full run page: which of the eleven nodes fired, every tool call,
every witness the agent gathered, and the verdict set against the answer key. These are
projections of the ledger, so a page cannot flatter a run that went badly.</p>
<div class="tw"><table>
<tr><th>Case</th><th>Difficulty</th><th>Verdict</th><th>Root cause</th>
<th>Citation recall</th><th>Lucky guess</th><th>Tool calls</th></tr>
{''.join(trs)}
</table></div>

<h2>Reproduce it</h2>
<p>The faults are seeded, so the answer key is decided by construction rather than by
our opinion. Scoring runs against the committed ledgers with no API key and no
DataHub instance.</p>
<pre>git clone {REPO}
cd witness-graph
python3 harness/warehouse.py --all --out warehouse/
python3 harness/score.py --cases cases/ --runs runs/
python3 harness/check_submission.py --warehouses warehouse/</pre>
<p>The last command is the one worth running. It fails the build when the repository
is not in a state a judge could evaluate: a node declaring a tool that is not
registered, a committed ledger that answers its own case wrongly, a case corpus that
no longer regenerates identically, an absolute path leaking into a ledger.</p>

<h2>What is deliberately visible</h2>
<p>Lucky guesses are reported, not hidden: a run that names the right fault without
ever citing the evidence for it is marked, because the difference between knowing and
guessing is the entire point. Refused tool calls are reported the same way. A run that
reaches for a tool its node never declared gets stopped by the runtime and logged, and
that log is what lets the scorer claim zero unapproved effects without taking the
model's word for it.</p>

<div class="foot">
Source, benchmark and scorer: <a href="{REPO}">{REPO}</a> &middot; Apache-2.0 &middot;
Full scorer output: <a href="summary.json">summary.json</a>
</div>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "_site")
    ap.add_argument("--runs", type=pathlib.Path, default=ROOT / "runs")
    ap.add_argument("--cases", type=pathlib.Path, default=ROOT / "cases")
    ap.add_argument("--graph", type=pathlib.Path,
                    default=ROOT / "graphs" / "metric-truth.yaml")
    a = ap.parse_args()

    a.out.mkdir(parents=True, exist_ok=True)
    graph = yaml.safe_load(a.graph.read_text(encoding="utf-8"))

    result = score(a.cases, a.runs)
    rows = sorted(result["per_case"], key=lambda r: r["case_id"])

    for r in rows:
        cid = r["case_id"]
        ledger = render_run.load_jsonl(a.runs / f"{cid}.jsonl")
        case = json.loads((a.cases / f"{cid}.json").read_text(encoding="utf-8"))
        page = a.out / f"{cid}.html"
        page.write_text(render_run.build(graph, ledger, case), encoding="utf-8")
        print(f"  {cid}.html  {page.stat().st_size} bytes")

    (a.out / "summary.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8")
    idx = a.out / "index.html"
    idx.write_text(index_html(result["summary"], rows), encoding="utf-8")
    print(f"  index.html  {idx.stat().st_size} bytes  ({len(rows)} run(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
