#!/usr/bin/env python3
"""The lookups an investigation may request, and the boundary that makes the
allowlist claim checkable.

Everything reaches DataHub through its MCP Server. The runtime is the MCP
client, not the model: the model names a lookup, `call()` checks it against the
node's allowlist, and only then does the request leave this process. Handing
the MCP session to the model instead would be less code and would also destroy
the thing this harness is for, because "zero disallowed tool calls" would
become a statement about the model's restraint rather than about the runtime.

One stdio session is opened lazily and reused for the whole run, driven from a
private event loop thread so the synchronous node loop can call into it.

    python3 harness/tools.py     # self-check against a live DataHub
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import time

import warehouse

GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080")
UVX = os.environ.get("WITNESS_UVX", os.path.expanduser("~/.local/bin/uvx"))
MCP_PACKAGE = os.environ.get("WITNESS_MCP_PACKAGE", "mcp-server-datahub@latest")
CALL_TIMEOUT = 90
START_TIMEOUT = 180


class ToolError(Exception):
    """A call the runtime mediated but could not satisfy.

    Distinct from a refusal: a refusal never reaches DataHub at all.
    """


class _Bridge:
    """A long-lived MCP stdio session usable from ordinary threaded code."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._stop: asyncio.Event | None = None

    def _start(self) -> None:
        with self._lock:
            if self._ready.is_set() or self._loop is not None:
                return
            threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(START_TIMEOUT):
            raise ToolError(f"MCP server did not start within {START_TIMEOUT}s")
        if self._error is not None:
            raise ToolError(f"MCP server failed to start: {self._error}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            self._error = exc
            self._ready.set()

    async def _serve(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        env = dict(os.environ)
        env["DATAHUB_GMS_URL"] = GMS_URL
        env.setdefault("DATAHUB_GMS_TOKEN", "")
        params = StdioServerParameters(command=UVX, args=[MCP_PACKAGE], env=env)

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._stop = asyncio.Event()
                self._ready.set()
                await self._stop.wait()

    def call(self, name: str, args: dict) -> object:
        self._start()
        assert self._loop is not None and self._session is not None
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, args), self._loop
        )
        try:
            result = future.result(timeout=CALL_TIMEOUT)
        except TimeoutError as exc:
            raise ToolError(f"MCP call `{name}` exceeded {CALL_TIMEOUT}s") from exc
        except Exception as exc:  # noqa: BLE001 - the MCP layer raises broadly
            raise ToolError(f"MCP call `{name}` failed: {exc}") from exc

        if getattr(result, "isError", False):
            raise ToolError(f"MCP call `{name}` returned an error")

        text = "".join(
            c.text for c in (result.content or []) if getattr(c, "text", None)
        )
        if not text.strip():
            raise ToolError(f"MCP call `{name}` returned nothing")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}


BRIDGE = _Bridge()

# The warehouse under investigation. Set per run, because each case gets its
# own database and an investigation must never see another case's rows.
WAREHOUSE_DB: pathlib.Path | None = None


def use_warehouse(path: pathlib.Path) -> None:
    global WAREHOUSE_DB
    WAREHOUSE_DB = path


# --- lookup implementations ----------------------------------------------
# Each returns (observation, ref). `ref` is what a witness must carry as its
# source and what the gate re-resolves. A lookup that cannot name a resolvable
# ref has no business producing evidence.


def _search(args: dict) -> tuple[object, str]:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("search requires a non-empty `query`")
    count = min(int(args.get("count", 10)), 25)
    return BRIDGE.call("search", {"query": query, "num_results": count}), f"search:{query}"


def _lineage(args: dict) -> tuple[object, str]:
    urn = str(args.get("urn", "")).strip()
    if not urn:
        raise ToolError("lineage requires `urn`")
    direction = str(args.get("direction", "UPSTREAM")).upper()
    if direction not in ("UPSTREAM", "DOWNSTREAM"):
        raise ToolError("direction must be UPSTREAM or DOWNSTREAM")
    payload = BRIDGE.call("get_lineage", {
        "urn": urn,
        "upstream": direction == "UPSTREAM",
        "max_hops": int(args.get("max_hops", 1)),
    })
    return payload, urn


def _schema(args: dict) -> tuple[object, str]:
    urn = str(args.get("urn", "")).strip()
    if not urn:
        raise ToolError("schema requires `urn`")
    fields = BRIDGE.call("list_schema_fields", {"urn": urn})
    entity = BRIDGE.call("get_entities", {"urns": [urn]})
    # Both halves are returned. An agent must be able to tell "no schema is
    # recorded" from "the dataset has no fields": the first is a blind spot,
    # the second is a fact, and conflating them is how an investigation
    # mistakes missing metadata for a clean bill of health.
    return {"fields": fields, "entity": entity}, urn


def _entity(args: dict) -> tuple[object, str]:
    urn = str(args.get("urn", "")).strip()
    if not urn:
        raise ToolError("entity requires `urn`")
    return BRIDGE.call("get_entities", {"urns": [urn]}), urn


def _queries(args: dict) -> tuple[object, str]:
    urn = str(args.get("urn", "")).strip()
    if not urn:
        raise ToolError("queries requires `urn`")
    return BRIDGE.call("get_dataset_queries", {"urn": urn}), urn


def _warehouse_query(args: dict) -> tuple[object, str]:
    """Read-only SQL against the case's warehouse.

    The catalog says what an asset is; this says what its rows did. Without it
    an investigation can describe a suspicion but never confirm one — the first
    full run dropped the correct hypothesis for exactly that reason, writing a
    witness that said the row-level audit could not be performed.
    """
    if WAREHOUSE_DB is None:
        raise ToolError("no warehouse is attached to this run")
    sql = str(args.get("sql", "")).strip()
    if not sql:
        raise ToolError("warehouse.query requires `sql`")
    try:
        result = warehouse.query(WAREHOUSE_DB, sql)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # sqlite errors carry the useful detail
        raise ToolError(f"query failed: {exc}") from exc
    # The ref names the table the observation came from, so the gate can check
    # it exists rather than taking the claim on faith.
    relation = _first_relation(sql)
    # sqlite keeps view text in sqlite_master, so a SELECT against it really
    # does hand back the definition — and the witness built on it is then
    # thrown out, because the gate resolves a ref against the case's relations
    # and sqlite_master is not one of them. Reading it is fine; citing it is
    # not. Say so at the point of contact rather than letting the run find out
    # at a gate that reports it as an unresolvable source, which is the same
    # verdict an invented one gets.
    if relation.startswith("sqlite_"):
        result["note"] = (
            f"`warehouse://{WAREHOUSE_DB.stem}/{relation}` does not resolve at the "
            "witness gate, so a witness sourced here is discarded as unresolvable. "
            "Read the same definition with warehouse.schema instead: it returns the "
            "view text under a ref that names the relation itself."
        )
    return result, f"warehouse://{WAREHOUSE_DB.stem}/{relation}"


def _warehouse_schema(args: dict) -> tuple[object, str]:
    """The definition of one relation, including the SQL a view is built from.

    Derived models are views, and the view text is where a bucketing or
    filtering defect is actually written down. Without this an investigation can
    read a description saying a table buckets on local time and still have no
    way to see the expression that does it, so it tests the theory against the
    raw table instead of against the view and refutes a hypothesis that was
    correct. That happened on MTI-003: the run held the right theory, checked it
    in the wrong place, and threw it away.
    """
    if WAREHOUSE_DB is None:
        raise ToolError("no warehouse is attached to this run")
    relations = warehouse.schema_of(WAREHOUSE_DB)
    want = str(args.get("relation", "")).strip()
    if not want:
        raise ToolError(
            "warehouse.schema requires `relation`. Available: "
            + ", ".join(f"{r['name']} ({r['kind']})" for r in relations)
        )
    for rel in relations:
        if rel["name"] == want:
            return rel, f"warehouse://{WAREHOUSE_DB.stem}/{rel['name']}"
    raise ToolError(
        f"no relation named `{want}`. Available: "
        + ", ".join(r["name"] for r in relations)
    )


def _first_relation(sql: str) -> str:
    """The first table or view named after FROM or JOIN. Good enough to cite."""
    words = sql.replace("(", " ( ").replace(")", " ) ").split()
    for i, w in enumerate(words[:-1]):
        if w.lower() in ("from", "join"):
            return words[i + 1].strip('",;')
    return "?"


REGISTRY = {
    "datahub.search": _search,
    "datahub.lineage": _lineage,
    "datahub.schema": _schema,
    "datahub.ownership": _entity,
    "datahub.assertion": _entity,
    "datahub.queries": _queries,
    "warehouse.query": _warehouse_query,
    "warehouse.schema": _warehouse_schema,
}

# What the agent is told it can ask for. Kept next to the registry so the two
# cannot drift apart.
SIGNATURES = {
    "datahub.search": 'datahub.search {"query": "<free text>", "count": 10} '
                      "-> matching datasets and jobs. Start here when you need a URN.",
    "datahub.lineage": 'datahub.lineage {"urn": "<urn>", "direction": "UPSTREAM|DOWNSTREAM", "max_hops": 1} '
                       "-> what feeds it, or what it feeds.",
    "datahub.schema": 'datahub.schema {"urn": "<dataset urn>"} '
                      "-> schema fields plus the entity record. Fields may be absent; that is a blind spot, not a clean result.",
    "datahub.ownership": 'datahub.ownership {"urn": "<urn>"} -> the entity record including owners.',
    "datahub.assertion": 'datahub.assertion {"urn": "<urn>"} -> the entity record including any assertions.',
    "datahub.queries": 'datahub.queries {"urn": "<dataset urn>"} -> SQL seen against this dataset.',
    "warehouse.query": 'warehouse.query {"sql": "<single SELECT>"} -> up to 50 rows from the '
                       "warehouse this metric is built on. SQLite dialect, read-only. Use it to "
                       "confirm or kill a hypothesis with actual numbers. Cite the result with "
                       "source.ref \"warehouse://<case>/<table>\" exactly as the tool returns it. "
                       "Rows say what happened, not how a number was computed; when the dispute is "
                       "about the computation, read the definition with warehouse.schema before you "
                       "design a test, or you will test the wrong relation.",
    "warehouse.schema": 'warehouse.schema {"relation": "<table or view>"} -> its columns and, '
                        "for a view, the SQL it is defined by. Read this before testing a theory "
                        "about how a number is computed: the daily and weekly models are views, "
                        "and a bucketing or filtering rule lives in the view text, not in the raw "
                        "table underneath it. Omit `relation` to be told which ones exist.",
}

SOURCE_KIND = {
    "datahub.search": "datahub_lineage",
    "datahub.lineage": "datahub_lineage",
    "datahub.schema": "datahub_schema",
    "datahub.ownership": "datahub_ownership",
    "datahub.assertion": "datahub_assertion",
    "datahub.queries": "warehouse_query",
    "warehouse.query": "warehouse_query",
    "warehouse.schema": "warehouse_query",
}


def call(tool: str, args: dict, allowlist: list[str] | None) -> tuple[dict, dict]:
    """Mediate one lookup.

    Returns (payload, ledger_record). The record is written whether or not the
    call was permitted, which is the point: a refused call is evidence about
    the run, not an event to swallow.
    """
    started = time.time()
    allowed = tool in (allowlist or []) and tool in REGISTRY
    if not allowed:
        reason = (
            f"`{tool}` is not in this node's allowlist {sorted(allowlist or [])}"
            if tool in REGISTRY
            else f"unknown lookup `{tool}`"
        )
        return (
            {"error": reason, "refused": True},
            {"tool": tool, "allowed": False, "wall_ms": int((time.time() - started) * 1000)},
        )

    try:
        payload, ref = REGISTRY[tool](args)
        return payload if isinstance(payload, dict) else {"result": payload}, {
            "tool": tool, "allowed": True, "ref": ref,
            "wall_ms": int((time.time() - started) * 1000),
        }
    except ToolError as exc:
        return (
            {"error": str(exc)},
            {"tool": tool, "allowed": True, "wall_ms": int((time.time() - started) * 1000)},
        )


def resolves(ref: str) -> bool:
    """Can this source ref be re-resolved right now?

    Used by the witness gate. A witness whose source cannot be fetched again is
    indistinguishable from one that was invented, so the gate treats them alike.
    """
    if not ref:
        return False
    if ref.startswith("warehouse://"):
        # Same standard as a urn: the thing cited has to still be there.
        if WAREHOUSE_DB is None:
            return False
        relation = ref.rsplit("/", 1)[-1]
        return any(t["name"] == relation for t in warehouse.schema_of(WAREHOUSE_DB))
    if not ref.startswith("urn:li:"):
        return False
    try:
        payload = BRIDGE.call("get_entities", {"urns": [ref]})
    except ToolError:
        return False
    # A miss still echoes the urn back, inside an `error` record. Searching the
    # response text for the urn therefore passes fabricated ones, which would
    # leave the witness gate asserting nothing. The record has to be a hit.
    items = payload if isinstance(payload, list) else [payload]
    return any(
        isinstance(item, dict) and item.get("urn") == ref and "error" not in item
        for item in items
    )


if __name__ == "__main__":
    # Smallest thing that fails if the boundary breaks.
    payload, rec = call("shell.exec", {"cmd": "rm -rf /"}, ["datahub.search"])
    assert rec["allowed"] is False, "unknown lookup must be refused"
    assert payload["refused"] is True

    payload, rec = call("datahub.search", {"query": "x"}, ["datahub.lineage"])
    assert rec["allowed"] is False, "known lookup outside the allowlist must be refused"

    payload, rec = call("warehouse.query", {"sql": "select 1"}, ["warehouse.query"])
    assert rec["allowed"] is True, "an allowed lookup that fails is still an allowed call"
    assert "error" in payload, "an unwired lookup must fail loudly, not return data"

    # Live half: proves the MCP session actually carries traffic.
    payload, rec = call("datahub.search", {"query": "channel_events_raw"}, ["datahub.search"])
    assert rec["allowed"] is True and "error" not in payload, f"MCP search failed: {payload}"
    urns = [r["entity"]["urn"] for r in payload.get("searchResults", [])]
    assert any("channel_events_raw" in u for u in urns), f"estate not found via MCP: {urns[:3]}"

    assert resolves(urns[0]), "a urn returned by search must re-resolve"
    assert not resolves("urn:li:dataset:(urn:li:dataPlatform:postgres,nope.nope,PROD)"), \
        "a fabricated urn must not resolve"

    print(f"MCP boundary ok — {len(urns)} hits, session via {MCP_PACKAGE}")
