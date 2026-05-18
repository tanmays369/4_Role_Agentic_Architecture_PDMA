"""The agent6 loop. Wires the four cognitive roles together.

This file is small and dependency-light: it owns the loop ordering, the
MCP stdio session lifecycle, and the human-readable trace printer.
Every cognitive step delegates to the role module.

Per the spec, the loop on each iteration runs:
    Memory.read  ->  Perception.observe  ->  (attach?)  ->
        Decision.next_step  ->  (Action.execute, Memory.record_outcome)

It terminates when Perception marks every goal as done, or when the
hard iteration budget MAX_ITERATIONS is exceeded.

Run directly via:

    uv run python -m agent6 --query A
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time as _time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import action, decision, perception
from .artifacts import ArtifactStore
from .gateway import GatewayError, ensure_gateway
from .memory import MemoryService
from .schemas import Goal

log = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(WORKSPACE_ROOT / ".env")

MCP_SERVER_PATH = WORKSPACE_ROOT / "mcp_server.py"

MAX_ITERATIONS = int(os.getenv("AGENT6_MAX_ITERATIONS", "14"))


class Trace:
    """Print-only — never used for parsing or control flow."""

    def __init__(self, *, stream=sys.stdout):
        self.stream = stream

    def _w(self, s: str = "") -> None:
        self.stream.write(s + "\n")
        self.stream.flush()

    def banner(self, query: str, run_id: str) -> None:
        self._w("=" * 72)
        self._w(f"agent6  run_id={run_id}")
        self._w(f"query: {query}")
        self._w("=" * 72)

    def remember(self, item) -> None:
        if item is None:
            self._w("[memory.remember]  classifier returned `none`; nothing persisted")
        else:
            kws = ", ".join(item.keywords)
            self._w(
                f"[memory.remember]  classified as {item.kind}: {item.descriptor}\n"
                f"                   keywords: [{kws}]"
            )

    def iter_header(self, n: int) -> None:
        self._w(f"\n─── iter {n} ───")

    def mem_read(self, hits) -> None:
        self._w(f"[memory.read]   {len(hits)} hits")
        for h in hits[:6]:
            tag = f" art={h.artifact_id}" if h.artifact_id else ""
            self._w(f"                  - [{h.kind}] {h.descriptor[:120]}{tag}")

    def perception(self, goals) -> None:
        for i, g in enumerate(goals):
            label = "done" if g.done else "open"
            self._w(f"{'[perception]' if i == 0 else '            '}    [{label}] {g.text}")
            if g.attach_artifact_ids and not g.done:
                handles = ", ".join(g.attach_artifact_ids)
                self._w(f"                  attach=[{handles}]")

    def attach(self, handle: str, size: int) -> None:
        self._w(f"[attach]        {handle} ({size} bytes)")

    def decision_answer(self, text: str) -> None:
        preview = text.strip().replace("\n", " ")
        if len(preview) > 240:
            preview = preview[:240] + "..."
        self._w(f"[decision]      ANSWER: {preview}")

    def decision_tool(self, name: str, args: dict) -> None:
        self._w(f"[decision]      TOOL_CALL: {name}({args})")

    def action(self, descriptor: str) -> None:
        preview = descriptor.replace("\n", " ")
        if len(preview) > 220:
            preview = preview[:220] + "..."
        self._w(f"[action]        -> {preview}")

    def done(self, n: int) -> None:
        self._w(f"\n[done] all {n} goals satisfied")

    def budget_exceeded(self, used: int) -> None:
        self._w(f"\n[budget] iteration budget {used}/{MAX_ITERATIONS} exhausted; stopping")

    def final(self, text: str) -> None:
        self._w(f"\nFINAL: {text}")


@asynccontextmanager
async def mcp_session():
    """Spawn mcp_server.py in the same Python env this agent runs in."""
    if not MCP_SERVER_PATH.exists():
        raise FileNotFoundError(
            f"MCP server not found at {MCP_SERVER_PATH}. The workspace must "
            f"contain mcp_server.py."
        )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER_PATH)],
        cwd=str(WORKSPACE_ROOT),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


def _mcp_tools_for_decision(mcp_tools) -> list[dict]:
    """Translate MCP Tool objects into the gateway's ToolDef shape."""
    out = []
    for t in mcp_tools:
        out.append(
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            }
        )
    return out


def final_answer_from(history: list[dict], goals: list[Goal]) -> str:
    """Build the FINAL string from the history.

    Strategy:
      1. If there are any answer events, take the LAST answer event per
         goal (in goal order) and join them. The last goal's answer is
         almost always the "final" one for the user.
      2. If there are no answer events (e.g. Query C1, which is all
         create_file calls), summarise the action outcomes briefly.
    """
    answers_by_goal: dict[str, str] = {}
    for ev in history:
        if ev.get("kind") == "answer":
            answers_by_goal[ev.get("goal_id", "?")] = ev.get("text", "")
    if answers_by_goal:
        ordered = []
        for g in goals:
            if g.id in answers_by_goal:
                ordered.append(answers_by_goal[g.id])
        return "\n\n".join(ordered).strip() or "(no answer)"

    action_lines = []
    for ev in history:
        if ev.get("kind") == "action":
            tool = ev.get("tool", "?")
            args = ev.get("arguments") or {}
            desc = ev.get("result_descriptor", "")
            action_lines.append(f"{tool}({args}): {desc[:180]}")
    if action_lines:
        return "Completed actions:\n" + "\n".join(f"  - {l}" for l in action_lines)
    return "(no answer; no actions taken)"


async def run(query: str, *, run_id: Optional[str] = None) -> str:
    """Run one query end-to-end. Returns the final answer string."""
    ensure_gateway()
    trace = Trace()
    run_id = run_id or uuid.uuid4().hex[:8]
    memory = MemoryService()
    artifacts = ArtifactStore()
    history: list[dict] = []
    prior_goals: list[Goal] = []

    trace.banner(query, run_id)

    item = memory.remember(query, source="user_query", run_id=run_id)
    trace.remember(item)

    async with mcp_session() as session:
        tools_result = await session.list_tools()
        mcp_tools = tools_result.tools
        gw_tools = _mcp_tools_for_decision(mcp_tools)

        last_goals: list[Goal] = []
        consecutive_failures = 0
        it = 0
        attempts = 0
        attempt_budget = MAX_ITERATIONS * 2
        while it < MAX_ITERATIONS and attempts < attempt_budget:
            attempts += 1
            it += 1
            trace.iter_header(it)
            hits = memory.read(query, history)
            trace.mem_read(hits)

            try:
                obs = perception.observe(query, hits, history, prior_goals, run_id)
            except GatewayError as e:
                trace._w(f"[perception]    gateway error after retries: {e}")
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    trace._w("[loop]          three consecutive gateway failures; aborting")
                    break
                sleep_s = 65.0
                trace._w(f"[loop]          sleeping {sleep_s:.0f}s to let rate-limit window clear")
                _time.sleep(sleep_s)
                it -= 1
                continue

            prior_goals = obs.goals
            last_goals = obs.goals
            trace.perception(obs.goals)

            if obs.all_done:
                trace.done(len(obs.goals))
                break

            goal = obs.next_unfinished()
            if goal is None:
                trace.done(len(obs.goals))
                break

            attached: list[tuple[str, bytes]] = []
            for handle in goal.attach_artifact_ids:
                if artifacts.exists(handle):
                    blob = artifacts.get_bytes(handle)
                    trace.attach(handle, len(blob))
                    attached.append((handle, blob))

            try:
                out = decision.next_step(
                    query, goal, hits, attached, history, gw_tools
                )
            except GatewayError as e:
                trace._w(f"[decision]      gateway error after retries: {e}")
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    trace._w("[loop]          three consecutive gateway failures; aborting")
                    break
                sleep_s = 65.0
                trace._w(f"[loop]          sleeping {sleep_s:.0f}s to let rate-limit window clear")
                _time.sleep(sleep_s)
                it -= 1
                continue

            consecutive_failures = 0

            if out.is_answer:
                trace.decision_answer(out.answer)
                history.append(
                    {
                        "iter": it,
                        "kind": "answer",
                        "goal_id": goal.id,
                        "text": out.answer,
                    }
                )
                continue

            tool_call = out.tool_call
            trace.decision_tool(tool_call.name, tool_call.arguments)
            descriptor, art_id = await action.execute(
                session, tool_call, artifacts=artifacts
            )
            trace.action(descriptor)
            memory.record_outcome(
                tool_call=tool_call,
                result_text=descriptor,
                artifact_id=art_id,
                run_id=run_id,
                goal_id=goal.id,
            )
            history.append(
                {
                    "iter": it,
                    "kind": "action",
                    "goal_id": goal.id,
                    "tool": tool_call.name,
                    "arguments": tool_call.arguments,
                    "result_descriptor": descriptor[:300],
                    "artifact_id": art_id,
                }
            )

        if it >= MAX_ITERATIONS or attempts >= attempt_budget:
            trace.budget_exceeded(MAX_ITERATIONS)

    final = final_answer_from(history, last_goals)
    trace.final(final)
    return final


def run_sync(query: str, *, run_id: Optional[str] = None) -> str:
    try:
        return asyncio.run(run(query, run_id=run_id))
    except GatewayError as e:
        print(f"\n[fatal] {e}", file=sys.stderr)
        raise SystemExit(2) from e
