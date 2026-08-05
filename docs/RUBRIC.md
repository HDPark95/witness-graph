# Graph engineering rubric

A graph is not better than a loop because it has more boxes. It is better when
coordination that used to live in a person's head is written down somewhere a
machine can check. This rubric is how we decide whether a given agent graph has
actually done that.

Eight dimensions, scored 0 to 3. Level 3 is not aspirational; it is the level at
which a claim about the graph can be **falsified from artifacts alone**, without
asking the model what it did.

Use it twice: once when designing a graph, once when reviewing a finished run.

---

## 1. Node contract

*Is each node's output constrained to a shape the runtime can check?*

| | |
|---|---|
| 0 | Free text flows between nodes. Downstream parses prose. |
| 1 | A conventional shape exists by habit. Nothing enforces it. |
| 2 | Each node declares a schema. Violations trigger a retry. |
| 3 | Violations are recorded, not just retried. Retry-to-valid rate is itself reported. |

**Why it matters.** Without contracts, every node is coupled to the exact
phrasing of the node before it, and "refactoring" means re-reading transcripts.
With them, a node is replaceable.

---

## 2. Evidence discipline

*Can a factual claim be traced to something outside the model?*

| | |
|---|---|
| 0 | What the model asserts is treated as true. |
| 1 | Sources are encouraged in the prompt. |
| 2 | Every claim must carry a source. |
| 3 | Sources are re-resolvable, and a citation absent from the ledger is detected mechanically. |

**Why it matters.** This is the dimension that converts hallucination from a
judgement call into a test failure. At level 3 you stop arguing about whether the
agent made something up and start reading a number.

---

## 3. Refutation

*Does the graph look for evidence that would kill its own hypothesis?*

| | |
|---|---|
| 0 | The first plausible story is confirmed. |
| 1 | Several hypotheses are generated. |
| 2 | Each hypothesis declares, in advance, what would refute it. |
| 3 | A run where nothing was ever refuted fails a gate. |

**Why it matters.** Agents are extremely good at accumulating support. Requiring
the refutation condition *before* looking is what stops the investigation from
becoming a search for confirmation.

---

## 4. Effect isolation

*How narrow is the surface through which the graph changes the outside world?*

| | |
|---|---|
| 0 | Any node may write, call, or notify. |
| 1 | Convention says only some nodes should. |
| 2 | Effects are declared per node and enforced by an allowlist. |
| 3 | Effects require an approving node upstream, and "zero unapproved effects" is verified from the ledger. |

**Why it matters.** Autonomy is only safe when the blast radius is a property of
the graph rather than a property of the prompt.

---

## 5. Termination

*Does the graph know when to stop, and is stopping distinguishable from failing?*

| | |
|---|---|
| 0 | It runs until someone kills it. |
| 1 | A maximum step count exists. |
| 2 | Steps, tokens, wall time, and tool calls all have ceilings. |
| 3 | Exhausting the budget is scored as *inconclusive*, never as a crash and never as an answer. |

**Why it matters.** A graph that cannot abstain will always produce an answer,
and an answer produced by exhaustion is indistinguishable from one produced by
reasoning unless you separate the two at scoring time.

---

## 6. Observability

*Can the run be reconstructed after the fact by someone who was not there?*

| | |
|---|---|
| 0 | Application logs only. |
| 1 | Steps are recorded. |
| 2 | Append-only ledger, one record per node execution. |
| 3 | Inputs are digested so nondeterminism is visible, and the run replays from the ledger. |

**Why it matters.** Everything else in this rubric is checked against the
ledger. If the ledger is thin, the other seven scores are opinions.

---

## 7. Scoring independence

*Who decides whether the run was good?*

| | |
|---|---|
| 0 | The model evaluates itself. |
| 1 | A human eyeballs the output. |
| 2 | An external scorer grades the final artifact. |
| 3 | The scorer reads only the ledger, and publishes a majority-class baseline next to the headline number. |

**Why it matters.** Accuracy without a baseline describes the corpus, not the
agent. A benchmark where one answer is correct 86 percent of the time makes a
do-nothing agent look excellent.

---

## 8. Portability

*What has to change to point this graph at a different problem?*

| | |
|---|---|
| 0 | The problem is welded into the code. |
| 1 | Some helpers are reusable. |
| 2 | The graph is declarative; swapping problems means swapping tools and cases. |
| 3 | A new domain needs no change to the runtime or the core schemas. |

**Why it matters.** This is the whole reason to build a harness instead of a
project. If the second hackathon requires rewriting the first one, there was no
harness.

---

## Self-assessment: Witness Graph, metric-truth

Scored honestly, including where we fall short. Updated as the build proceeds.

| Dimension | Score | Evidence / gap |
|---|---|---|
| Node contract | 3 | Every `agent` node requires a `contract`; the schema enforces it. Ledger keeps `contract_violations` and the scorer reports `mean_contract_retries`. |
| Evidence discipline | 3 | `evidence.schema.json` requires `source.ref`. `citation_precision` catches ids cited but absent from the ledger; verified against a deliberately bad run. |
| Refutation | 3 | `hypothesis.schema.json` requires `would_be_refuted_by`. `witness_gate` asserts `at_least_one_hypothesis_refuted`. |
| Effect isolation | 3 | Exactly two nodes carry write effects, and `write_back` depends on `human_approval`. Scorer asserts `unapproved_effects_total == 0`. |
| Termination | 2 | Budget ceilings are declared in the graph. **Gap: the runtime does not yet score an exhausted budget as `inconclusive`.** |
| Observability | 2 | `ledger.schema.json` defines `inputs_digest` and per-node records. **Gap: replay from ledger is not implemented.** |
| Scoring independence | 3 | `score.py` reads only ledger records, and reports `baseline_accuracy` and `lift_over_baseline` alongside accuracy. |
| Portability | 2 | Graph, schemas, and scorer are domain-agnostic; only tools and cases are metric-specific. **Gap: unproven until a second domain runs on it, which is what the CockroachDB entry tests.** |

**Total 21 / 24.** The three gaps are deliberate: each is a runtime feature, and
runtime work is scheduled after the benchmark produces its first numbers. Any of
them closing before the deadline is a bonus, not a dependency.

The honest read is that our strengths are in the *design* dimensions, which are
cheap to get right early, and our gaps are in the *runtime* dimensions, which
cost implementation time. That is the correct order under a five-day deadline.
