#!/usr/bin/env python3
"""Fail if the repository is not in a state a judge can evaluate.

Every check here exists because the thing it checks was actually wrong at some
point during the build, and each one was found by hand. Judging happens against
whatever is on the default branch at the deadline, so the checks that matter are
the ones a reviewer performs without asking us anything: clone, read, run.

    python3 harness/check_submission.py [--warehouses DIR]

Exits non-zero on any failure. Warnings do not fail the run.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def check_readable_without_running() -> None:
    """A judge is not required to run anything, so the repo must read on its own."""
    readme = ROOT / "README.md"
    if not readme.exists():
        fail("README.md is missing; Submission Quality is judged on what can be read")
        return
    text = readme.read_text(encoding="utf-8")
    if len(text) < 1500:
        warn(f"README.md is {len(text)} characters; it may not carry the submission")
    if not (ROOT / "LICENSE").exists():
        fail("LICENSE is missing; the hackathon requires an OSI licence")


def check_language() -> None:
    """The submission is judged in English."""
    korean = re.compile(r"[가-힣]")
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        if path.exists() and korean.search(path.read_text(encoding="utf-8")):
            fail(f"{path.relative_to(ROOT)} contains Korean; judges read English")
    for path in sorted((ROOT / "runs").glob("*.html")):
        if korean.search(path.read_text(encoding="utf-8")):
            fail(f"{path.relative_to(ROOT)} renders Korean")


def check_declared_tools_exist() -> None:
    """A node that names a tool the registry lacks skips silently at runtime."""
    import yaml

    import tools

    graph = yaml.safe_load((ROOT / "graphs" / "metric-truth.yaml").read_text(encoding="utf-8"))
    for node in graph.get("nodes", []):
        for name in node.get("tools", []) or []:
            if name not in tools.REGISTRY:
                fail(f"node `{node['id']}` declares `{name}`, which is not registered")
    drift = set(tools.REGISTRY) ^ set(tools.SIGNATURES)
    if drift:
        fail(f"registry and signatures disagree on: {sorted(drift)}")


def check_evidence_is_current() -> None:
    """A stale ledger is the only evidence a judge who runs the scorer will see."""
    runs = sorted((ROOT / "runs").glob("*.jsonl"))
    if not runs:
        fail("runs/ has no ledger; the scorer has nothing to score and the claims are unevidenced")
        return
    for path in runs:
        case_id = path.stem
        case_file = ROOT / "cases" / f"{case_id}.json"
        if not case_file.exists():
            continue
        truth = json.loads(case_file.read_text(encoding="utf-8"))["ground_truth"]
        verdicts = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        adjudications = [
            r for r in verdicts if r.get("node_id") == "adjudicate" and r.get("status") == "ok"
        ]
        if not adjudications:
            warn(f"{path.relative_to(ROOT)} has no adjudication; it reads as an abstention")
            continue
        got = adjudications[-1].get("output", {})
        if got.get("verdict") != truth["verdict"]:
            fail(
                f"{path.relative_to(ROOT)} answers `{got.get('verdict')}` where the key says "
                f"`{truth['verdict']}`; a reviewer running score.py sees this run fail"
            )


def check_identifiers() -> None:
    """The repository is published under a personal account on purpose."""
    # Assembled rather than written out, so this file does not match itself.
    banned = re.compile("|".join(["async" + "site", "team" + "grit", "gyup" + "gyup"]), re.IGNORECASE)
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git/" in str(path):
            continue
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".gif", ".db", ".sqlite"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if banned.search(text):
            fail(f"{path.relative_to(ROOT)} names the organisation")


def check_citability(warehouses: pathlib.Path) -> None:
    """Delegates to the existing audit so the two cannot disagree."""
    result = subprocess.run(
        [sys.executable, str(HERE / "audit_citability.py"), str(warehouses)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        fail("audit_citability reports unreachable answer keys:\n    "
             + "\n    ".join(result.stdout.strip().splitlines()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warehouses", type=pathlib.Path, default=ROOT / "warehouse")
    args = parser.parse_args()

    check_readable_without_running()
    check_language()
    check_declared_tools_exist()
    check_evidence_is_current()
    check_identifiers()
    if args.warehouses.exists():
        check_citability(args.warehouses)
    else:
        warn(f"{args.warehouses} not built; citability not checked")

    for w in WARNINGS:
        print(f"WARN  {w}")
    for f in FAILURES:
        print(f"FAIL  {f}")
    print(f"{len(FAILURES)} failure(s), {len(WARNINGS)} warning(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
