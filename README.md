# Witness Graph

**A metric moved. Did customers change, or did the instrument break?**

Answering that wrong is expensive in both directions. Treat a dead collector as
churn and you launch a retention campaign at nobody. Treat real churn as a
pipeline bug and you spend a week reading logs while customers keep leaving.

Witness Graph is an agent that decides which one happened, and that can be
checked on its work afterwards. It walks the lineage around the affected metric
in DataHub, forms competing hypotheses, gathers evidence for each, and refuses to
commit to a verdict until every hypothesis has been argued against.

Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/),
track: AI agents handling data problems.

**[Read a scored investigation without cloning anything →](https://renechoi.github.io/witness-graph/)**
That page is generated from the committed run ledgers, so every figure on it is
the scorer's output rather than a number we typed.

---

## Why lineage is the whole trick

A metric dashboard shows you a number falling. It cannot tell you whether the
number is measuring less behaviour or measuring less well. That distinction lives
one layer up, in the assets that feed the metric:

```
signup_conversion_daily  →  signups  →  events_daily  →  channel_events_raw
```

If `channel_events_raw` stopped receiving rows on the 14th, the zero at the top
is an instrument reading, not a customer fact. You cannot see that from the
metric. You can see it from the graph above the metric, which is exactly what
DataHub holds.

Every investigation in this project starts by walking that graph, and every
verdict has to cite assets in it.

---

## What the agent does

Eleven nodes. Only five of them are agents. The other six are code, and that is
the point: the checks that keep an investigation honest are not prompting.

| Node | Kind | What it does |
|---|---|---|
| `intake` | transform | Load one case, expose only the symptom. The answer key stays sealed. |
| `map_terrain` | agent | Walk DataHub lineage around the metric before hypothesising. |
| `hypothesize` | agent | Form competing explanations, each with what would refute it. |
| `gather_witnesses` | agent | Collect evidence per hypothesis, in parallel. |
| `witness_gate` | **gate** | Fail the run unless every hypothesis has a witness, every source resolves, at least one hypothesis is refuted, and no claim is unsourced. |
| `adjudicate` | agent | Commit to one verdict. |
| `citation_gate` | **gate** | Fail the run if the verdict cites a witness that is not in the ledger, or rejects an alternative without a refuting witness. |
| `propose_remediation` | agent | Say what to fix. |
| `human_approval` | **human** | Nothing outside the run changes without a person saying yes. |
| `write_back` | tool | Draft the write-back and stop. Write tools are unconnected in this release, so an approved run records what it would have written rather than writing it. |
| `report` | transform | Render the run from the ledger alone. |

The two gates are the reason this is a graph rather than a prompt.

- An investigation that anchored on its first plausible story fails
  `witness_gate`, which asserts `at_least_one_hypothesis_refuted`.
- One that invented a table fails the same gate on
  `every_witness_source_resolves`: each source ref is fetched again, and a
  witness whose source cannot be re-resolved is treated exactly like one that
  was made up.
- One that dismisses an alternative with a confident sentence and no evidence
  fails `citation_gate` on `alternatives_rejected_have_refuting_witnesses`.

Neither check asks the model what it did.

---

## Running it

Needs a DataHub instance and a virtualenv holding the DataHub SDK and
`mcp-server-datahub`.

```bash
export DATAHUB_GMS_URL=http://localhost:8080

.venv/bin/python harness/emit_estate.py                              # publish the estate
.venv/bin/python harness/warehouse.py --all --out warehouse/         # seed 11 warehouses
.venv/bin/python harness/warehouse.py --self-check                   # each fault reproduces
.venv/bin/python harness/audit_citability.py                         # every answer is reachable

./batch.sh runs/                                                     # pilot one case, then the rest, then score

.venv/bin/python harness/run.py --case cases/MTI-003.json --out runs/    # or one case
.venv/bin/python harness/render_run.py --graph graphs/metric-truth.yaml \
    --ledger runs/MTI-003.jsonl --case cases/MTI-003.json --out run.html
```

**Use the venv interpreter, not bare `python3`.** The harness reaches DataHub
through its MCP Server and that import lives only in the venv. Under bare
`python3` every node still completes, every lookup fails with
`No module named 'mcp'`, and a ledger is still written, so the run looks finished
and contains no evidence. `batch.sh` refuses to start rather than let that happen.

Nothing writes to DataHub without `--approve`, and `human_approval` has no
auto-approve path.

To measure what a capability is worth instead of asserting it, revoke it and run
the same benchmark again:

```bash
.venv/bin/python harness/run.py --case cases/MTI-003.json --out runs-no-lineage/ \
    --ablate datahub.lineage
```

A revoked call is refused by the same per-node allowlist as any out-of-scope
call, so the ledger records the attempt and you can see which reasoning step the
agent could no longer take.

---

## The benchmark

Eleven cases with sealed answer keys, in `cases/`. Each one is generated by
seeding a specific fault into the estate, so the ground truth is determined by
construction rather than by our opinion.

| Verdict | Cases |
|---|---|
| `instrument_failure` | MTI-001 to MTI-006 |
| `upstream_data_defect` | MTI-007 |
| `customer_behavior` | MTI-008, MTI-010, MTI-011 |
| `definition_change` | MTI-009 |

**Three cases are controls where nothing is broken and customers really did
change**, and a fourth records a definition change rather than a defect. They are
there because a benchmark made only of broken pipelines measures bias rather than
judgement. Even with them, an agent that always answers "instrument failure"
scores 6 out of 11, which is why verdict accuracy is not the headline number
below. Every case also carries distractors, plus `must_cite`
assets and `must_not_conclude` verdicts.

`harness/audit_citability.py` asserts that every `must_cite` asset is reachable
by some tool the agent actually has, either as a relation in the case warehouse
or as a catalog urn whose logical name survives scoring. A case whose answer key
points at something no lookup can return does not measure investigation quality,
it measures guessing, so the check fails the build rather than the run.

Difficulty spans easy to hard. The hard ones are the ones that look like the
other answer: a browser-specific checkout drop that arrives the same week a user
agent string changes, a weekend-only dip that resembles a batch job defect.

---

## Scoring

`harness/score.py` reads the run ledger and never the agent's own summary. Every
number is recomputed from ledger records and case files, so a run cannot flatter
itself.

```bash
.venv/bin/python harness/score.py --cases cases/ --runs runs/ --out report.json
```

Read these three first.

- **`root_cause_top1`** — did it name the specific fault. This is the headline
  number. The eleven root causes are all distinct, so a lazy agent guessing the
  most common one scores 0.091, while guessing the most common verdict scores
  0.545. Both baselines and both lifts are reported, because quoting only the
  verdict lift would be the deck-stacking the baseline exists to expose.
- **`lucky_guess_rate`** — right answer reached without touching the sources a
  correct investigation cannot avoid. Reported separately so it never inflates
  accuracy.
- **`abstention_rate`** — how often it answered `inconclusive`. Abstaining is a
  real answer here and is not scored as a wrong verdict, so a run that suppresses
  it to look decisive is visible rather than rewarded.

It also reports verdict accuracy, citation precision and recall against the
witness ledger, `confidently_wrong_rate`, unapproved effects, disallowed tool
calls, and whether the gates fired.

`harness/render_run.py` turns a single ledger into a self-contained HTML page, so
a reviewer can read what happened without running anything.

Two ledgers for the same case ship with the repo, and scoring them against each
other is the shortest demonstration that the harness discriminates.

```bash
.venv/bin/python harness/score.py --cases cases/ --runs runs/         # root_cause_top1 1.0
.venv/bin/python harness/score.py --cases cases/ --runs runs-sonnet/  # root_cause_top1 0.0
```

`runs/` names the root cause with citation precision and recall both 1.0 and no
lucky guess. Its verdict label is still wrong: the run predates the verdict
taxonomy being defined in the schema, and the ledger shows the reasoning that
led there. `runs-sonnet/` is the same case on a smaller model, which followed a
distractor. `runs-catalog-only/` holds seven earlier runs from before the
warehouse was wired in, when the agent could read the catalog but not query the
data underneath it.

---

## The rubric

`docs/RUBRIC.md` scores an agent graph on eight dimensions, 0 to 3. Level 3 is
defined the same way throughout: **the claim can be falsified from artifacts
alone, without asking the model what it did.**

It is written to be reusable on graphs that have nothing to do with metrics. This
project is its first subject.

---

## Repository layout

```
cases/                       11 benchmark cases with sealed answer keys, plus the generator
graphs/metric-truth.yaml     the investigation graph, validated against schemas/graph.schema.json
schemas/                     10 JSON Schemas: state, symptom, hypothesis, evidence, verdict, ledger, ...
harness/run.py               the executor: walks the graph, writes the ledger
harness/tools.py             the tool boundary where the per-node allowlist is enforced
harness/warehouse.py         seeds a per-case warehouse and self-checks that each fault reproduces
harness/score.py             the scorer, reading ledgers only
harness/emit_estate.py       publishes the estate to DataHub
harness/render_run.py        renders one ledger to a self-contained HTML page
harness/audit_citability.py  asserts every must_cite asset is reachable
batch.sh                     pilot one case, then run the rest two at a time, then score
docs/RUBRIC.md               the graph engineering rubric
runs/                        a sample ledger from a completed investigation
```

All schemas are JSON Schema draft 2020-12 and cross-reference by relative path.

---

## Licence

Apache 2.0. See `LICENSE`.
