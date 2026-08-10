#!/usr/bin/env python3
"""Execute one Witness Graph investigation and append its ledger.

The runtime owns every side effect. The model is called as a pure function:
it gets a prompt, it returns one JSON object, and it has no tools of its own.
Every lookup it wants goes back through `tools.call()`, which checks the node's
allowlist and writes the outcome to the ledger. That is what lets the scorer
claim "zero disallowed tool calls" without taking the model's word for it.

    python3 harness/run.py --case cases/MTI-003.json --out runs/
    python3 harness/run.py --case cases/MTI-003.json --out runs/ --approve
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jsonschema
import yaml

import tools
import warehouse

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The first full batch ran on sonnet and the ledgers show why that was the
# wrong default. On MTI-003 the run reached the correct hypothesis, cited the
# catalog description that supports it, then designed a test that could not
# bear on it and refuted itself. That is a reasoning failure, not a tooling
# gap: every lookup it needed was already granted.
MODEL = "opus"
MAX_TURNS_PER_NODE = 14
# Two cases in parallel, three witness investigations each, means six
# concurrent calls on one box. Under that load a call can sit well past
# three minutes without being stuck.
LLM_TIMEOUT = 300
# How long the API may stay unreachable before the run is abandoned as an
# infrastructure failure rather than recorded as an agent that did not answer.
# Generous on purpose: this box shares one rate limit with other sessions, and
# waiting fifteen minutes is cheaper than re-running a case that takes an hour.
MAX_TRANSPORT_STALL_SECONDS = 900
TRANSPORT_BACKOFF_CAP = 120


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def digest(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


class LLMFailure(Exception):
    """The model call did not come back usable.

    `transport` separates the two reasons that can happen, because they deserve
    opposite handling. A transport failure means the call never reached a model
    or never came back: a rate limit, a timeout, a non-zero exit. The model did
    not misbehave, so it must not cost a contract turn. Everything else means a
    model did answer and answered badly, which is exactly what the turn budget
    exists to bound.

    Charging transport failures to the turn budget cost three cases on
    2026-08-09: fourteen consecutive rate-limit rejections burned a whole node's
    budget in seconds, and the run was recorded as an agent that could not
    answer.
    """

    def __init__(self, message: str, transport: bool = True):
        super().__init__(message)
        self.transport = transport


class InfrastructureFailure(Exception):
    """The API stayed unreachable long enough that the run cannot continue.

    Distinct from BudgetExceeded on purpose. An exhausted budget is a fact about
    the agent and is scored as an abstention; this is a fact about the box, and
    a ledger that ends this way is not evidence about the agent at all.
    """


class BudgetExceeded(Exception):
    """Raised when a ceiling in the graph is hit.

    Deliberately not an error path: the run ends without a verdict, and the
    scorer reads a missing verdict as an abstention rather than a crash.
    """


class Ledger:
    """Append-only. Written as it goes, so a killed run still leaves a record."""

    def __init__(self, path: pathlib.Path, run_id: str, budget: dict):
        self.path = path
        self.run_id = run_id
        self.seq = 0
        self.budget = budget or {}
        self.tool_calls = 0
        self.started = time.time()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    def write(self, **rec) -> int:
        with self._lock:
            seq = self.seq
            self.seq += 1
            line = {"run_id": self.run_id, "seq": seq, **rec}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return seq

    def count_tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def check_budget(self) -> None:
        b = self.budget
        if "max_steps" in b and self.seq >= b["max_steps"]:
            raise BudgetExceeded(f"max_steps {b['max_steps']}")
        if "max_tool_calls" in b and self.tool_calls >= b["max_tool_calls"]:
            raise BudgetExceeded(f"max_tool_calls {b['max_tool_calls']}")
        if "max_wall_seconds" in b and (time.time() - self.started) >= b["max_wall_seconds"]:
            raise BudgetExceeded(f"max_wall_seconds {b['max_wall_seconds']}")


# --- the model call -------------------------------------------------------

SYSTEM = (
    "You are one node inside an investigation graph. This session has no tools "
    "and no file access, and searching for any would only waste the node's "
    "budget. The runtime does every lookup for you: name one in your reply and "
    "its result comes back in your next prompt. Reply with exactly one JSON "
    "object and nothing else: no prose, no markdown fence, no explanation "
    "before or after."
)


def llm(prompt: str) -> tuple[str, dict]:
    # The allowlist names a tool that does not exist, which is the only form of
    # "no tools" the CLI takes reliably: an empty string is parsed as a value
    # and leaves the built-ins reachable. The model's own tools are not part of
    # this harness's accounting, so letting even one through would make the
    # disallowed-tool-calls metric a claim about luck.
    try:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
            "--model", MODEL,
            "--output-format", "json",
            # Four was not enough: the model spends turns discovering it has no
            # tools of its own, and a node that runs out mid-search wastes a whole
            # turn of the node budget rather than one CLI turn.
            "--max-turns", "8",
                "--append-system-prompt", SYSTEM,
                "--allowed-tools", "__witness_graph_none__",
            ],
            capture_output=True, text=True, timeout=LLM_TIMEOUT, cwd="/tmp",
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMFailure(f"call exceeded {LLM_TIMEOUT}s") from exc
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise LLMFailure(f"unparseable CLI output: {exc}") from exc
    if proc.returncode != 0 or payload.get("subtype") != "success":
        # `error_max_turns` is the one failure here the model earned: it was
        # reached, it ran, and it spent its own CLI turns without concluding.
        # Every other shape means the call did not complete, so the turn budget
        # must not pay for it.
        subtype = payload.get("subtype")
        raise LLMFailure(
            f"rc={proc.returncode} subtype={subtype} stderr={proc.stderr[:200]}",
            transport=(subtype != "error_max_turns"),
        )
    usage = payload.get("usage") or {}
    cost = {
        "tokens_in": usage.get("input_tokens", 0),
        "tokens_out": usage.get("output_tokens", 0),
        "wall_ms": payload.get("duration_ms", 0),
    }
    return payload.get("result") or "", cost


def parse_json(text: str):
    """Pull one JSON object out of a reply, tolerating a stray fence.

    Tolerant here, strict at the contract. Being lenient about the wrapper and
    unforgiving about the shape is what keeps retry-to-valid meaningful.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    start = min((i for i in (t.find("{"), t.find("[")) if i != -1), default=-1)
    if start == -1:
        raise ValueError("no JSON in reply")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(t[start:])
    return obj


# --- gate predicates ------------------------------------------------------
# Unknown names fail closed. A gate that silently passes an assertion nobody
# implemented is worse than no gate, because it reads as a guarantee.


def _hyp_ids(state) -> set[str]:
    return {h["id"] for h in state.get("hypotheses", [])}


def p_every_hypothesis_has_at_least_one_witness(state) -> tuple[bool, str]:
    covered = set()
    for w in state.get("witnesses", []):
        covered |= set(w.get("supports") or []) | set(w.get("refutes") or [])
    missing = _hyp_ids(state) - covered
    return not missing, f"hypotheses with no witness: {sorted(missing)}" if missing else ""


def p_every_witness_source_resolves(state) -> tuple[bool, str]:
    bad = [w["id"] for w in state.get("witnesses", [])
           if not tools.resolves((w.get("source") or {}).get("ref", ""))]
    return not bad, f"unresolvable sources: {bad}" if bad else ""


def p_at_least_one_hypothesis_refuted(state) -> tuple[bool, str]:
    any_ref = any(w.get("refutes") for w in state.get("witnesses", []))
    return any_ref, "" if any_ref else "no witness refutes anything; this is a confirmation-bias run"


def p_no_claim_without_source(state) -> tuple[bool, str]:
    bad = [w.get("id") for w in state.get("witnesses", [])
           if not (w.get("source") or {}).get("ref")]
    return not bad, f"witnesses without a source ref: {bad}" if bad else ""


def p_all_cited_witnesses_exist(state) -> tuple[bool, str]:
    ids = {w["id"] for w in state.get("witnesses", [])}
    cited = set((state.get("verdict") or {}).get("cited_witnesses", []))
    dangling = sorted(cited - ids)
    return not dangling, f"cited but absent from the ledger: {dangling}" if dangling else ""


def p_alternatives_rejected_have_refuting_witnesses(state) -> tuple[bool, str]:
    ids = {w["id"] for w in state.get("witnesses", [])}
    bad = []
    for alt in (state.get("verdict") or {}).get("alternatives_rejected", []):
        refs = [r for r in alt.get("refuted_by", []) if r in ids]
        if not refs:
            bad.append(alt.get("hypothesis"))
    return not bad, f"alternatives dismissed without evidence: {bad}" if bad else ""


PREDICATES = {
    name[2:]: fn for name, fn in list(globals().items()) if name.startswith("p_")
}


# --- node execution -------------------------------------------------------


# Which blackboard key a node's output lands on. Nodes never write to each
# other's keys, which is what keeps a node replaceable.
STATE_KEY = {
    "map_terrain": "terrain",
    "adjudicate": "verdict",
    "propose_remediation": "remediation",
}


def load_schema(node: dict, graph_dir: pathlib.Path) -> dict | None:
    if not node.get("contract"):
        return None
    return json.loads((graph_dir / node["contract"]).resolve().read_text(encoding="utf-8"))


def validate(instance, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
            for e in validator.iter_errors(instance)]


def blackboard_view(node_id: str, state: dict, extra: dict | None = None) -> dict:
    """What this node is allowed to see.

    intake seals the answer key; nothing downstream ever receives ground_truth,
    must_cite or distractors. Keeping that narrowing here rather than in each
    prompt means a new node cannot accidentally widen it.
    """
    view = {"symptom": state.get("symptom")}
    if node_id in ("hypothesize", "gather_witnesses", "adjudicate", "propose_remediation"):
        view["terrain"] = state.get("terrain")
    if node_id in ("gather_witnesses", "adjudicate", "propose_remediation"):
        view["hypotheses"] = state.get("hypotheses")
    if node_id in ("adjudicate", "propose_remediation"):
        view["witnesses"] = state.get("witnesses")
    if node_id == "propose_remediation":
        view["verdict"] = state.get("verdict")
    return {**view, **(extra or {})}


def build_prompt(node: dict, view: dict, schema: dict, transcript: list[str],
                 allowlist: list[str], instruction: str, remaining: int) -> str:
    tool_lines = "\n".join(f"  - {tools.SIGNATURES[t]}" for t in allowlist if t in tools.SIGNATURES)
    parts = [
        f"# Node: {node['id']}",
        node.get("description", "").strip(),
        "",
        "## What you know",
        json.dumps(view, ensure_ascii=False, indent=2, default=str),
        "",
    ]
    if allowlist:
        parts += [
            "## Lookups the runtime performs for you",
            tool_lines or "  (none)",
            "",
            "You do not run these. Ask for one and the runtime runs it, then shows you the",
            "result under `Lookups so far`. Asking for a name not on this list is recorded as",
            "a violation and returns nothing.",
            "",
        ]
    if transcript:
        parts += ["## Lookups so far", "\n\n".join(transcript), ""]
    # A node that cannot see its own budget spends all of it looking. Naming
    # the remaining turns is what turns an open-ended search into an
    # investigation that has to commit.
    if remaining <= 1:
        parts += [
            "## LAST TURN",
            "You have no lookups left. Return `output` now, using what you already have. "
            "Fields you could not establish stay empty, and say so in the record rather "
            "than looking again. An incomplete terrain map is usable; no output is not.",
            "",
        ]
    else:
        parts += [
            f"## Budget: {remaining} turns left",
            "Each lookup costs one. Leave yourself a turn to produce `output`.",
            "",
        ]
    parts += [
        "## Your contract",
        "Your `output` must validate against this JSON Schema:",
        json.dumps(schema, ensure_ascii=False, indent=2),
        "",
        "## Reply format",
        "Exactly one JSON object, no prose, no code fence. Either:",
        '  {"lookup": {"name": "<lookup name>", "args": {...}}}   to have the runtime fetch something, or',
        '  {"output": <value matching the contract>}              when you can satisfy the contract.',
        "",
        instruction.strip(),
    ]
    return "\n".join(parts)


INSTRUCTIONS = {
    "map_terrain": (
        "Walk the lineage around the affected metric before forming any theory. "
        "Find the metric's URN first, then read what feeds it. Record blind spots "
        "honestly: a dataset with no schema recorded is a blind spot, not a clean bill of health, "
        "and an empty `owners` list is a finding worth recording rather than a reason to keep "
        "looking. Never repeat a lookup already shown above."
    ),
    "hypothesize": (
        "Produce ONE hypothesis. It must be falsifiable and `would_be_refuted_by` must "
        "name observations that would kill it, written before you look. Do not reuse a "
        "class already listed under `existing_hypotheses`."
    ),
    "gather_witnesses": (
        "Investigate the ONE hypothesis under `assigned_hypothesis`. Return an array of "
        "witness objects. Every `source.ref` MUST be a DataHub URN that the runtime can "
        "re-resolve; a ref that is not a urn:li:... invalidates the witness. `observation` "
        "is what the tool actually returned, not your reading of it. You are explicitly "
        "asked to look for evidence that REFUTES your assigned hypothesis, and to say so "
        "in `refutes` when you find it. Use the witness ids you are told to use."
    ),
    "adjudicate": (
        "Commit to one verdict and one root_cause_key. Cite witnesses by id; you may only "
        "cite ids present in `witnesses`. Every alternative you reject needs at least one "
        "witness id that argues against it. If the evidence does not separate the "
        "candidates, answer `inconclusive` rather than guessing: an unsupported answer "
        "is scored worse than an abstention."
    ),
    "propose_remediation": (
        "Draft the fix. Nothing here is applied. `blast_radius` comes from lineage you "
        "actually read, not from what seems likely."
    ),
}


def run_agent(node: dict, state: dict, ledger: Ledger, schema: dict,
              view: dict, instance: str | None = None) -> object:
    """One agent node: a loop of mediated lookups ending in a contracted output."""
    allowlist = node.get("tools") or []
    transcript: list[str] = []
    violations: list[str] = []
    tool_records: list[dict] = []
    cost_total = {"tokens_in": 0, "tokens_out": 0, "wall_ms": 0}
    started = now()
    instruction = INSTRUCTIONS.get(node["id"], "")

    # `attempt` counts turns the model actually took. A call that never came
    # back does not advance it, so an outage stalls the node instead of
    # consuming it.
    attempt = 0
    transport_fails = 0
    stalled_since = None
    while attempt < MAX_TURNS_PER_NODE:
        ledger.check_budget()
        prompt = build_prompt(node, view, schema, transcript, allowlist, instruction,
                              MAX_TURNS_PER_NODE - attempt)
        try:
            text, cost = llm(prompt)
        except LLMFailure as exc:
            if not exc.transport:
                attempt += 1
                violations.append(f"turn {attempt}: model call failed ({exc})")
                transcript.append(
                    "### runtime\nYour previous attempt did not return an answer. Do not "
                    "attempt any tool yourself. Reply with one JSON object only."
                )
                continue
            transport_fails += 1
            stalled_since = stalled_since or time.time()
            stalled = time.time() - stalled_since
            if stalled >= MAX_TRANSPORT_STALL_SECONDS:
                raise InfrastructureFailure(
                    f"node {node['id']}: {transport_fails} consecutive transport "
                    f"failures over {int(stalled)}s; last was {exc}"
                )
            # Back off rather than spin. The usual cause is a shared rate limit,
            # and hammering it neither clears it nor tells us anything.
            wait = min(TRANSPORT_BACKOFF_CAP, 5 * 2 ** min(transport_fails - 1, 6))
            print(f"  {node['id']}: transport failure {transport_fails}, "
                  f"retrying in {wait}s ({int(stalled)}s stalled)")
            time.sleep(wait)
            continue
        transport_fails = 0
        stalled_since = None
        attempt += 1
        for k in cost_total:
            cost_total[k] += cost.get(k, 0)

        try:
            reply = parse_json(text)
        except ValueError as exc:
            violations.append(f"turn {attempt}: unparseable reply ({exc})")
            transcript.append(f"### runtime\nYour last reply was not JSON. Reply with one JSON object.")
            continue

        if isinstance(reply, dict) and ("lookup" in reply or "tool_call" in reply):
            tc = reply.get("lookup") or reply.get("tool_call") or {}
            name = tc.get("name") or tc.get("tool") or ""
            payload, rec = tools.call(name, tc.get("args") or {}, allowlist)
            tool_records.append(rec)
            ledger.count_tool_call()
            shown = json.dumps(payload, ensure_ascii=False, default=str)
            transcript.append(
                f"### {name} {json.dumps(tc.get('args') or {}, ensure_ascii=False)}\n"
                # Long observations crowd out the rest of the prompt and slow every
            # later turn in the node. Fifty warehouse rows do not need to be
            # replayed in full to be reasoned about.
            f"{shown[:2500]}"
            )
            continue

        if isinstance(reply, dict) and "output" in reply:
            out = reply["output"]
            errs: list[str] = []
            if isinstance(out, list):
                for i, item in enumerate(out):
                    errs += [f"[{i}] {e}" for e in validate(item, schema)]
            else:
                errs = validate(out, schema)
            if errs:
                violations += [f"turn {attempt}: {e}" for e in errs[:6]]
                transcript.append(
                    "### runtime\nYour output failed its contract:\n"
                    + "\n".join(f"  - {e}" for e in errs[:6])
                    + "\nFix it and reply again."
                )
                continue

            ledger.write(
                node_id=node["id"], instance=instance, attempt=attempt, status="ok",
                started_at=started, ended_at=now(), inputs_digest=digest(view),
                output=out, contract_violations=violations, tool_calls=tool_records,
                effects=[], cost=cost_total,
            )
            return out

        violations.append(f"turn {attempt}: reply had neither lookup nor output")
        transcript.append('### runtime\nReply with {"lookup": ...} or {"output": ...}.')

    ledger.write(
        node_id=node["id"], instance=instance, attempt=MAX_TURNS_PER_NODE, status="failed",
        started_at=started, ended_at=now(), inputs_digest=digest(view),
        contract_violations=violations, tool_calls=tool_records, effects=[],
        cost=cost_total, error="node exhausted its turns without a valid output",
    )
    raise BudgetExceeded(f"node {node['id']} exhausted its turns")


def renumber(witnesses: list[dict], taken: set[str]) -> list[dict]:
    """Give every witness a run-unique id.

    Parallel instances each start numbering from their own base, but a model
    that ignores the base would otherwise produce two `e1`s and make citation
    precision meaningless. The runtime owns identity.
    """
    out = []
    nxt = 1
    for w in witnesses:
        wid = w.get("id")
        if not wid or wid in taken:
            while f"e{nxt}" in taken:
                nxt += 1
            wid = f"e{nxt}"
        taken.add(wid)
        out.append({**w, "id": wid})
    return out


def run_gate(node: dict, state: dict, ledger: Ledger) -> tuple[bool, list[str]]:
    started = now()
    failures = []
    for name in node.get("assert", []):
        fn = PREDICATES.get(name)
        if fn is None:
            failures.append(f"{name}: no such predicate (failing closed)")
            continue
        ok, why = fn(state)
        if not ok:
            failures.append(f"{name}: {why}")
    ledger.write(
        node_id=node["id"], status="ok" if not failures else "gate_failed",
        started_at=started, ended_at=now(), effects=[],
        contract_violations=failures, cost={"wall_ms": 0},
    )
    return (not failures), failures


def main() -> int:
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=pathlib.Path, required=True)
    ap.add_argument("--graph", type=pathlib.Path, default=ROOT / "graphs/metric-truth.yaml")
    ap.add_argument("--out", type=pathlib.Path, default=ROOT / "runs")
    ap.add_argument("--approve", action="store_true",
                    help="Stand in for the human approval node. Without it, nothing downstream of "
                         "human_approval runs, which is the default posture on purpose.")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--ablate", default="", metavar="TOOL[,TOOL...]",
                    help="Revoke these tools from every node before the run. The point is to "
                         "measure what a capability is worth instead of asserting it: "
                         "--ablate datahub.lineage answers 'how much worse is this "
                         "investigation without lineage' with a score difference. Revoked "
                         "calls are refused by the same allowlist check as any other "
                         "out-of-scope call, so the ledger records the attempt.")
    args = ap.parse_args()
    MODEL = args.model

    case = json.loads(args.case.read_text(encoding="utf-8"))
    graph = yaml.safe_load(args.graph.read_text(encoding="utf-8"))
    graph_dir = args.graph.resolve().parent
    nodes = {n["id"]: n for n in graph["nodes"]}

    ablated = [t.strip() for t in args.ablate.split(",") if t.strip()]
    if ablated:
        unknown = [t for t in ablated if t not in tools.REGISTRY]
        if unknown:
            ap.error(f"--ablate names tools that do not exist: {unknown}. "
                     f"Known: {sorted(tools.REGISTRY)}")
        for node in nodes.values():
            if node.get("tools"):
                node["tools"] = [t for t in node["tools"] if t not in ablated]
        print(f"  ablated: {', '.join(ablated)}")
    order = [n["id"] for n in graph["nodes"]]  # authored in dependency order

    # Rebuilt per run from a fixed seed, so the rows an investigation sees are
    # reproducible and no case can contaminate another.
    tools.use_warehouse(warehouse.build(case, ROOT / "warehouse"))

    run_id = f"{case['case_id']}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
    ledger = Ledger(args.out / f"{case['case_id']}.jsonl", run_id, graph.get("budget", {}))
    state: dict = {"run_id": run_id}
    witness_ids: set[str] = set()
    loops: dict[str, int] = {}

    print(f"{case['case_id']}  {case['title']}")
    idx = 0
    try:
        while idx < len(order):
            node = nodes[order[idx]]
            nid = node["id"]
            ledger.check_budget()
            schema = load_schema(node, graph_dir)

            if nid == "intake":
                started = now()
                # The answer key never enters the blackboard. Everything below
                # this line sees only what a real reporter would have said.
                state["symptom"] = {
                    "case_id": case["case_id"],
                    "metric": case["title"],
                    "observed_change": case["symptom"],
                    "window": {"from": "day 1", "to": "day 36"},
                    "notes": " ".join(case.get("distractors", [])) or None,
                }
                state["symptom"] = {k: v for k, v in state["symptom"].items() if v is not None}
                errs = validate(state["symptom"], schema) if schema else []
                ledger.write(node_id=nid, status="ok" if not errs else "failed",
                             started_at=started, ended_at=now(),
                             inputs_digest=digest(case["symptom"]), output=state["symptom"],
                             contract_violations=errs, effects=[], cost={"wall_ms": 0})
                print(f"  intake ok")

            elif nid == "hypothesize":
                hyps = []
                for i in range(node.get("fanout", 1)):
                    view = blackboard_view(nid, state, {
                        "existing_hypotheses": [{"id": h["id"], "class": h["class"],
                                                 "statement": h["statement"]} for h in hyps],
                        "your_hypothesis_id": f"h{i + 1}",
                    })
                    h = run_agent(node, state, ledger, schema, view, instance=f"h{i + 1}")
                    h["id"] = f"h{i + 1}"
                    hyps.append(h)
                state["hypotheses"] = hyps
                print(f"  hypothesize ok  {[h['class'] for h in hyps]}")

            elif nid == "gather_witnesses":
                collected = list(state.get("witnesses") or [])
                witness_ids = {w["id"] for w in collected}
                hyps = state["hypotheses"]
                views = [
                    blackboard_view(nid, state, {
                        "assigned_hypothesis": h,
                        "use_witness_ids_starting_at": f"e{i * 10 + 1}",
                    })
                    for i, h in enumerate(hyps)
                ]
                with ThreadPoolExecutor(max_workers=len(hyps)) as pool:
                    futures = [
                        pool.submit(run_agent, node, state, ledger, schema, v, h["id"])
                        for v, h in zip(views, hyps)
                    ]
                    outs = []
                    for f in futures:
                        try:
                            outs.append(f.result())
                        except BudgetExceeded:
                            # One investigation running dry does not invalidate
                            # its siblings; the gate decides whether what came
                            # back is enough.
                            outs.append([])
                # Ids are assigned here, single-threaded, so parallel instances
                # cannot collide on e1.
                for out in outs:
                    got = out if isinstance(out, list) else [out]
                    collected += renumber(got, witness_ids)
                state["witnesses"] = collected
                print(f"  gather_witnesses ok  {len(collected)} witnesses")

            elif node["type"] == "gate":
                ok, failures = run_gate(node, state, ledger)
                if ok:
                    print(f"  {nid} pass")
                else:
                    on_fail = node.get("on_fail") or {}
                    route, max_loops = on_fail.get("route"), on_fail.get("max_loops", 1)
                    loops[nid] = loops.get(nid, 0) + 1
                    print(f"  {nid} FAIL ({loops[nid]}/{max_loops}): {failures}")
                    if route and loops[nid] <= max_loops:
                        # Re-entering the routed node is the whole point of a
                        # gate: the run gets another look, not a free pass.
                        if route == "gather_witnesses":
                            state["witnesses"] = list(state.get("witnesses") or [])
                        idx = order.index(route)
                        continue
                    if on_fail.get("otherwise", "abort") == "abort":
                        ledger.write(node_id=nid, status="aborted", started_at=now(),
                                     ended_at=now(), effects=[],
                                     error="gate failed and its retry budget is spent")
                        print("  aborted")
                        return 2

            elif node["type"] == "agent":
                # map_terrain, adjudicate, propose_remediation: one call, one
                # contracted value, parked on the blackboard under its own key.
                key = STATE_KEY.get(nid, nid)
                state[key] = run_agent(node, state, ledger, schema,
                                       blackboard_view(nid, state))
                if nid == "adjudicate":
                    v = state["verdict"]
                    print(f"  adjudicate ok  {v['verdict']} / {v['root_cause_key']}")
                else:
                    print(f"  {nid} ok")

            elif node["type"] == "human":
                started = now()
                if args.approve:
                    seq = ledger.write(node_id=nid, status="ok", started_at=started,
                                       ended_at=now(), effects=[],
                                       output={"approved": True, "by": "operator"},
                                       cost={"wall_ms": 0})
                    state["approvals"] = [{"node": nid, "seq": seq}]
                    print(f"  human_approval approved (seq {seq})")
                else:
                    ledger.write(node_id=nid, status="skipped", started_at=started,
                                 ended_at=now(), effects=[],
                                 error="no approval given; the run stops before any external change")
                    print("  human_approval not given, stopping before write_back")

            elif node["type"] == "tool":
                # write_back. Reached only with an approval upstream, and the
                # write tools are not connected yet, so it records honestly
                # rather than pretending.
                if not state.get("approvals"):
                    ledger.write(node_id=nid, status="skipped", started_at=now(),
                                 ended_at=now(), effects=[], error="no upstream approval")
                else:
                    started = now()
                    verdict = state.get("verdict") or {}
                    # Targets come from the witnesses the verdict cited, never from a
                    # field the agent fills in. Those refs already passed
                    # `every_witness_source_resolves`, so a write cannot land on an
                    # asset the investigation never opened. Asking the agent for a urn
                    # instead would let it invent one, and the write would follow.
                    cited = set(verdict.get("cited_witnesses") or [])
                    by_id = {w.get("id"): w for w in state.get("witnesses", [])}

                    def datasets_for(ids) -> list[str]:
                        out = []
                        for wid in sorted(ids):
                            ref = ((by_id.get(wid) or {}).get("source") or {}).get("ref", "")
                            if ref.startswith("urn:li:dataset:") and ref not in out:
                                out.append(ref)
                        return out

                    # Cited is the set that was opened. It is not the set the fault is
                    # in. On MTI-002 the verdict cites twelve witnesses resolving to
                    # five datasets, and three of those five are the deploy log, the
                    # definition history and the migration log: assets read precisely
                    # to rule those explanations out. Tagging all five with
                    # `bot_traffic_mixed` brands the exonerating evidence as the
                    # culprit, which leaves the catalog worse than it found it.
                    #
                    # `implicated_witnesses` narrows it, and is ids only so the agent
                    # picks from what it already cited instead of naming a urn it
                    # could invent. Anything cited and not implicated is still marked,
                    # but as examined rather than as the cause.
                    implicated_ids = set(verdict.get("implicated_witnesses") or []) & cited
                    implicated = datasets_for(implicated_ids)
                    examined = [d for d in datasets_for(cited) if d not in implicated]
                    targets = implicated + examined
                    if not targets:
                        ledger.write(node_id=nid, status="skipped", started_at=started,
                                     ended_at=now(), effects=[],
                                     error="no cited witness resolved to a dataset urn")
                        print("  write_back skipped (no cited dataset witness)")
                    else:
                        # Each write is recorded separately so a partial write-back
                        # is visible as a partial write-back. Collapsing them into
                        # one record would let a failure halfway through read as a
                        # clean skip.
                        applied, failed = [], []
                        # `narrative` already exists in the verdict schema and the agent
                        # already fills it, so the finding is written rather than a
                        # placeholder. save_document is left out of this release: the
                        # report node writes no artefact, so there is no document to
                        # attach and a link to nothing is worse than no link.
                        note = (verdict.get("narrative") or "").strip() or "Investigated by witness-graph."
                        cause = verdict.get("root_cause_key", "unclassified")
                        plan = []
                        # Two write kinds, deliberately unequal. An implicated asset
                        # gets the accusation and the finding. An examined asset gets
                        # a note that it was looked at, and nothing else: overwriting
                        # its description with a narrative about a fault it does not
                        # have is the same error as the tag, one field over.
                        #
                        # Prefixes so a catalog reader can tell the two apart, and so
                        # both are attributable to this system rather than appearing
                        # as bare vocabulary someone on the data team chose.
                        for target in implicated:
                            plan.append(("datahub.add_tags",
                                         {"urn": target, "tags": [f"root-cause:{cause}"]}))
                            plan.append(("datahub.update_description",
                                         {"urn": target, "description": note}))
                        for target in examined:
                            plan.append(("datahub.add_tags",
                                         {"urn": target,
                                          "tags": ["witness-graph:examined"]}))
                        # The node's own allowlist governs the writes, exactly as it
                        # governs reads elsewhere. A write the graph did not declare is
                        # refused and recorded, not executed.
                        allowlist = node.get("tools") or []
                        for tool_name, tool_args in plan:
                            payload, record = tools.call(tool_name, tool_args, allowlist)
                            if payload.get("error"):
                                failed.append({"tool": tool_name, "error": payload["error"]})
                            else:
                                applied.append({"tool": tool_name,
                                                "ref": record.get("ref"),
                                                "result": payload})
                        ledger.write(node_id=nid,
                                     status="ok" if applied and not failed else
                                            ("failed" if not applied else "partial"),
                                     started_at=started, ended_at=now(),
                                     effects=["write"],
                                     # Both lists, not just the union. A run that
                                     # implicates everything it opened has not
                                     # localised anything, and that has to be
                                     # readable off the ledger rather than inferred
                                     # from the catalog afterwards.
                                     output={"targets": targets,
                                             "implicated": implicated,
                                             "examined": examined,
                                             "applied": applied, "failed": failed},
                                     error=None if not failed else f"{len(failed)} write(s) failed")
                        print(f"  write_back applied {len(applied)}, failed {len(failed)} "
                              f"across {len(implicated)} implicated and "
                              f"{len(examined)} examined dataset(s)")

            elif nid == "report":
                # Two things at once. The renderer already existed as a separate
                # script and nothing called it, so a reviewer who did not run the
                # code had nothing to open. And paths here are recorded relative
                # to the repo: the ledger is a published artefact, and an absolute
                # path bakes in whatever directory the run executed from, which is
                # how a batch put a private path into three ledgers about to ship.
                started = now()

                def _rel(path: pathlib.Path) -> str:
                    try:
                        return str(path.resolve().relative_to(ROOT))
                    except ValueError:
                        return str(pathlib.Path(path.parent.name) / path.name)

                html_path = ledger.path.with_suffix(".html")
                try:
                    import render_run
                    html = render_run.build(graph, render_run.load_jsonl(ledger.path), case)
                    html_path.write_text(html, encoding="utf-8")
                    ledger.write(node_id=nid, status="ok", started_at=started, ended_at=now(),
                                 effects=[], cost={"wall_ms": 0},
                                 output={"ledger": _rel(ledger.path),
                                         "report": _rel(html_path),
                                         "bytes": html_path.stat().st_size})
                    print(f"  report: {_rel(html_path)} ({html_path.stat().st_size} bytes)")
                except Exception as exc:  # noqa: BLE001 - a failed render is not a failed run
                    ledger.write(node_id=nid, status="partial", started_at=started, ended_at=now(),
                                 effects=[], cost={"wall_ms": 0},
                                 output={"ledger": _rel(ledger.path)},
                                 error=f"render failed: {exc}")
                    print(f"  report: ledger written, render failed ({exc})")

            idx += 1

    except BudgetExceeded as exc:
        # An exhausted run ends without a verdict on purpose. The scorer counts
        # that as an abstention, which is the honest reading: we ran out of
        # room, we did not answer.
        ledger.write(node_id="budget", status="aborted", started_at=now(), ended_at=now(),
                     effects=[], error=f"budget exceeded: {exc}")
        print(f"  ABORTED: {exc}")
        return 3
    except InfrastructureFailure as exc:
        # Separate exit code so a caller can refuse to publish this ledger. It
        # records that the API was down, and scoring it as an abstention would
        # attribute an outage to the agent.
        ledger.write(node_id="infrastructure", status="aborted", started_at=now(),
                     ended_at=now(), effects=[],
                     error=f"infrastructure_failure: {exc}")
        print(f"  INFRASTRUCTURE FAILURE: {exc}")
        return 4

    print(f"  ledger: {ledger.path}  ({ledger.seq} records, {ledger.tool_calls} tool calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
