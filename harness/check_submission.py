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


SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".db", ".sqlite"}


def tracked_files() -> list[pathlib.Path]:
    """Every file git would publish, so no directory is checked by accident."""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git/" not in str(p)]
    return [ROOT / n for n in result.stdout.split("\0") if n]


def read_text(path: pathlib.Path) -> str | None:
    if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def check_language() -> None:
    """The submission is judged in English.

    Checked across every tracked file rather than the documents, because the
    Korean that actually shipped was in a source file: render_run.py carried
    Korean headings and lang="ko", so the page it generated was Korean while
    every document in the repository was already English. Reading docs alone
    passes that, and it passed it for three days.
    """
    korean = re.compile("[\\uac00-\\ud7a3]")  # Hangul, escaped so this file does not match itself
    for path in tracked_files():
        text = read_text(path)
        if text and korean.search(text):
            fail(f"{path.relative_to(ROOT)} contains Korean; judges read English")
    for path in sorted((ROOT / "runs").glob("*.html")):
        if korean.search(path.read_text(encoding="utf-8")):
            fail(f"{path.relative_to(ROOT)} renders Korean")


def check_no_local_paths() -> None:
    """A published artefact must not carry the directory it was produced in.

    The report node recorded an absolute ledger path, so a batch executed from a
    scratch directory wrote that directory into three ledgers that were about to
    ship, with a home directory and an organisation name inside it. The path is
    useless to a reader anyway: it names a machine they do not have.
    """
    absolute = re.compile(r'"(/(?:home|Users|tmp|var|opt|private)/[^"]{4,})"')
    for path in tracked_files():
        if path.suffix not in {".jsonl", ".json", ".html", ".md", ".yaml", ".yml"}:
            continue
        text = read_text(path)
        if not text:
            continue
        for hit in sorted(set(absolute.findall(text)))[:3]:
            fail(f"{path.relative_to(ROOT)} records the absolute path {hit}")


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
        # The root cause is the headline metric, and it is the harder half: the
        # eleven causes are all distinct, so guessing the most common one scores
        # 0.091 against 0.545 for the most common verdict. A shipped ledger that
        # gets the label right and the cause wrong still reports a failure on the
        # number the README asks a reader to look at first.
        if got.get("root_cause_key") != truth["root_cause_key"]:
            fail(
                f"{path.relative_to(ROOT)} names root cause `{got.get('root_cause_key')}` "
                f"where the key says `{truth['root_cause_key']}`; that is the headline metric"
            )


def check_cases_regenerate_identically() -> None:
    """The committed corpus has to be what the generator produces.

    These two had drifted. MTI-003's committed symptom named the affected week
    and the right magnitude, measured against the warehouse at 1194 daily
    against 895 weekly; the generator still said 9 percent and named no week.
    Regenerating would have replaced a correct answer key with a wrong one, and
    nothing would have reported it. A corpus whose generator disagrees with it is
    not reproducible, which is the property that lets a reader trust the ground
    truth was constructed rather than chosen.
    """
    import shutil
    import tempfile

    cases = ROOT / "cases"
    with tempfile.TemporaryDirectory() as tmp:
        backup = pathlib.Path(tmp) / "cases"
        shutil.copytree(cases, backup)
        result = subprocess.run([sys.executable, str(cases / "build_cases.py")],
                                cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"cases/build_cases.py exits {result.returncode}; the corpus cannot be rebuilt")
        drifted = []
        for path in sorted(cases.glob("MTI-*.json")):
            before = (backup / path.name).read_text(encoding="utf-8")
            if path.read_text(encoding="utf-8") != before:
                drifted.append(path.name)
            # Restore regardless, so running the checker never edits the corpus.
            path.write_text(before, encoding="utf-8")
        if drifted:
            fail("cases/build_cases.py does not reproduce the committed corpus: "
                 + ", ".join(drifted))


def check_identifiers() -> None:
    """The repository is published under a personal account on purpose.

    Scoped to tracked files. Walking the whole tree read .venv and the built
    warehouses on every invocation, which is both slow and wrong: nothing git
    ignores is published, so a hit there is not a leak, and a leak that only
    exists in an ignored file cannot reach a judge.
    """
    # Assembled rather than written out, so this file does not match itself.
    banned = re.compile("|".join(["async" + "site", "team" + "grit", "gyup" + "gyup"]), re.IGNORECASE)
    me = pathlib.Path(__file__).resolve()
    for path in tracked_files():
        if path.resolve() == me:
            continue
        text = read_text(path)
        if text and banned.search(text):
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
    check_no_local_paths()
    check_declared_tools_exist()
    check_evidence_is_current()
    check_cases_regenerate_identically()
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
