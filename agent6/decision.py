"""Decision — picks the next action for one bounded goal.

Receives one Goal, the relevant MemoryItems, optionally the raw bytes of
artifacts Perception attached, the run history, and the list of MCP
tools (already translated to the gateway's ToolDef shape). Returns a
DecisionOutput populated with exactly one of:

    answer     plain text final answer for this goal, OR
    tool_call  a single typed ToolCall the loop will dispatch via MCP.

There is no parsing of LLM text with regex. We rely on the gateway's
native tool-use machinery: when the worker emits a tool_calls[] block,
we lift the first entry into a ToolCall; otherwise we treat the model's
text reply as the answer.

Routing:
  - With NO attached artifacts: auto_route="decision". The router pool
    classifies and picks a worker.
  - WITH attached artifacts: provider chain g → c → gr (Gemini → Cerebras
    → Groq). Large attachments would otherwise short-circuit to HUGE at
    the gateway's 8000-token ceiling, which 503s. The chain keeps the
    agent productive when any single worker is rate-limited.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .gateway import Gateway, GatewayError, get_gateway
from .schemas import DecisionOutput, Goal, MemoryItem, ToolCall

log = logging.getLogger(__name__)

ARTIFACT_ATTACH_MAX_BYTES = 6_000
ATTACHMENT_BYPASS_BYTES = 6_000

DECISION_SYSTEM = """\
You are DECISION, the action-picking role of a four-role agentic system.

You are given ONE GOAL to act on. You may also see:
  RELEVANT MEMORY  — typed memory rows that may contain useful facts,
                     preferences, or descriptors of prior tool outcomes.
  RECENT HISTORY   — what the loop has done so far in this run.
  ATTACHED ARTIFACTS — the raw bytes of one or more artifacts that
                     Perception decided are needed for this goal. They
                     are shown inline below as plain text.
  AVAILABLE TOOLS  — the MCP tools you may call.

YOUR OUTPUT IS EXACTLY ONE OF TWO THINGS:

  1. A FINAL ANSWER for this goal, in plain text. Use this when you have
     enough information to satisfy the goal directly.

  2. A SINGLE TOOL CALL. Use this when you need to fetch, look up, or
     write something to advance the goal. Call at most one tool per
     turn.

DO NOT do both. DO NOT narrate before a tool call. DO NOT ask
clarifying questions — act on the best interpretation.

RULES:

A. ARTIFACT HANDLES ARE INTERNAL. Strings beginning with `art:` are
   handles into the agent's artifact store. They are NOT URLs and they
   are NOT file paths. Never pass an `art:...` string as a tool
   argument. The raw bytes of any artifact you need are already shown
   under ATTACHED ARTIFACTS above. If a goal requires reading an
   artifact's content, read it from the ATTACHED ARTIFACTS section, do
   NOT call read_file or fetch_url on the handle.

B. SUBSTANTIVE ANSWERS. When the goal asks for an extraction, list,
   comparison, selection, recommendation, or synthesis, your answer
   must be substantive: AT LEAST three sentences, or a clear list of
   items with brief explanation. Do not return meta-answers like "the
   page has been fetched, how would you like to proceed?" — perform the
   actual task the goal describes.

C. ONE TOOL CALL PER TURN. If you genuinely need two tool calls to
   make progress, pick the one that unblocks the most work and call it.
   The loop will iterate.

D. PREFER FACTS ALREADY IN MEMORY/HISTORY OVER RE-FETCHING. If the
   answer is visible in RELEVANT MEMORY or RECENT HISTORY, answer
   directly instead of calling a tool.

E. NO REDUNDANT TOOL CALLS. If RECENT HISTORY shows you have already
   called the same tool with similar arguments (especially repeated
   web_search variants on the same topic, or repeated fetch_url calls on
   the same URL), DO NOT call it again. Either pick a meaningfully
   different tool/URL or commit to a FINAL ANSWER using what you have,
   noting any caveats inline. Two failed attempts to find a piece of
   information is enough — answer with what you have plus a brief
   "based on available sources" caveat.

F. SANDBOX PATHS. File tools (read_file, create_file, list_dir, etc.)
   operate inside a sandbox. Use simple relative paths like
   "reminders/note.txt" — no leading slashes, no `../`."""


def _format_memory(hits: list[MemoryItem], max_items: int = 8) -> str:
    if not hits:
        return "(none)"
    lines = []
    for h in hits[:max_items]:
        line = f"- [{h.kind}] {h.descriptor}"
        if h.artifact_id:
            line += f" (artifact={h.artifact_id})"
        if h.value:
            value_summary = {
                k: v for k, v in h.value.items()
                if k != "result_preview"
            }
            if value_summary:
                line += f" value={value_summary}"
        lines.append(line)
    return "\n".join(lines)


def _format_history(history: list[dict], max_events: int = 12) -> str:
    if not history:
        return "(none)"
    lines = []
    for ev in history[-max_events:]:
        it = ev.get("iter")
        kind = ev.get("kind")
        if kind == "action":
            tool = ev.get("tool", "?")
            args = ev.get("arguments") or {}
            desc = ev.get("result_descriptor", "")
            art = ev.get("artifact_id")
            tail = f" art={art}" if art else ""
            lines.append(
                f"iter {it} CALLED {tool}({args}) -> {desc[:160]!r}{tail}"
            )
        elif kind == "answer":
            txt = ev.get("text", "")
            lines.append(f"iter {it} ANSWERED: {txt[:200]!r}")
    return "\n".join(lines)


def _format_attachments(attached: list[tuple[str, bytes]]) -> str:
    """Render attached artifact bytes as inline UTF-8 text blocks, capped."""
    if not attached:
        return "(none attached this turn)"
    blocks = []
    for handle, blob in attached:
        if len(blob) > ARTIFACT_ATTACH_MAX_BYTES:
            truncated = blob[:ARTIFACT_ATTACH_MAX_BYTES]
            tail_note = (
                f"\n\n... [truncated; full artifact is {len(blob)} bytes, "
                f"showing first {ARTIFACT_ATTACH_MAX_BYTES}]"
            )
        else:
            truncated = blob
            tail_note = ""
        try:
            text = truncated.decode("utf-8", errors="replace")
        except Exception:
            text = repr(truncated)
        blocks.append(
            f"--- ARTIFACT {handle} ({len(blob)} bytes) ---\n{text}{tail_note}"
        )
    return "\n\n".join(blocks)


_WS_RE = re.compile(r"\s+")


def _looks_like_art_handle(value) -> bool:
    return isinstance(value, str) and value.startswith("art:")


def next_step(
    query: str,
    goal: Goal,
    hits: list[MemoryItem],
    attached: list[tuple[str, bytes]],
    history: list[dict],
    mcp_tools: list[dict],
    *,
    gateway: Optional[Gateway] = None,
) -> DecisionOutput:
    """One Decision call. Returns a validated DecisionOutput.

    On transient gateway errors (e.g. all providers in cooldown), this
    function re-raises GatewayError. The agent loop is responsible for
    deciding whether to sleep and retry. We deliberately do NOT
    fabricate an `answer` like "(decision failed: ...)" because that
    pollutes history and may mislead Perception into marking a goal as
    done when it wasn't.
    """
    gw = gateway or get_gateway()

    attach_summary = (
        ", ".join(goal.attach_artifact_ids) if goal.attach_artifact_ids else "none"
    )
    user = (
        f"ORIGINAL USER QUERY:\n{query}\n\n"
        f"CURRENT GOAL: {goal.text}\n"
        f"(goal_id={goal.id}, attach_artifact_ids=[{attach_summary}])\n\n"
        f"RELEVANT MEMORY:\n{_format_memory(hits)}\n\n"
        f"RECENT HISTORY:\n{_format_history(history)}\n\n"
        f"ATTACHED ARTIFACTS:\n{_format_attachments(attached)}"
    )

    total_attached = sum(len(b) for _, b in attached)
    if total_attached >= ATTACHMENT_BYPASS_BYTES:
        provider_chain = ["g", "c", "gr"]
        common = {"temperature": 1.0, "max_tokens": 1500}
        last_err: Optional[GatewayError] = None
        resp = None
        for prov in provider_chain:
            try:
                resp = gw.chat(
                    messages=[{"role": "user", "content": user}],
                    system=DECISION_SYSTEM,
                    tools=mcp_tools,
                    tool_choice="auto",
                    provider=prov,
                    retries=1,
                    **common,
                )
                if prov != "g":
                    log.info("decision fell back to provider=%s", prov)
                break
            except GatewayError as e:
                last_err = e
                continue
        if resp is None:
            raise last_err if last_err else GatewayError(
                "decision.next_step: no long-context provider available"
            )
    else:
        resp = gw.chat(
            messages=[{"role": "user", "content": user}],
            system=DECISION_SYSTEM,
            tools=mcp_tools,
            tool_choice="auto",
            auto_route="decision",
            temperature=0.3,
            max_tokens=1024,
        )

    tool_calls = resp.get("tool_calls") or []
    text = (resp.get("text") or "").strip()

    if tool_calls:
        tc = tool_calls[0]
        name = tc.get("name", "")
        args = tc.get("arguments", {}) or {}
        if not isinstance(args, dict):
            args = {}
        for k, v in args.items():
            if _looks_like_art_handle(v):
                return DecisionOutput(
                    answer=(
                        f"(refused: tried to pass artifact handle {v!r} as "
                        f"argument {k!r} to {name}; will re-plan)"
                    )
                )
        return DecisionOutput(tool_call=ToolCall(name=name, arguments=args))

    if not text:
        return DecisionOutput(
            answer="(no answer produced; will re-plan)"
        )
    return DecisionOutput(answer=text)
