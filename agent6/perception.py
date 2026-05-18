"""Perception — the orchestrator role.

Runs every iteration. Inputs:
    query        the original user request
    hits         current MemoryItem matches (keyword-ranked)
    history      list of action/answer events
    prior_goals  the Goal list returned on the previous iteration
    run_id

Output: Observation (typed). The wire schema asks the LLM for positional
goals (no id field) and a list of integer artifact_indices pointing into
MEMORY HITS; this module maps both back to durable Goal ids and `art:`
handles.

Safety nets layered on top of the LLM:
  - Sticky done: a goal that was once done stays done.
  - Position preservation: if the model returns fewer goals than prior,
    we treat the missing tail as still-open instead of dropping them.
  - Force-attach for synthesis goals: when the next-unfinished goal text
    contains a synthesis verb (synthesise / extract / list / compare /
    decide / choose / select / recommend / tell me / give me) and the
    model did not pick any artifact_indices, attach up to MAX_FORCE_ATTACH
    of the most recent artifacts in memory.

Provider fallback: Gemini → Groq → Cerebras. Spec pins Perception to
Gemini, but on free-tier Gemini the rolling 60-second window will trip
during a multi-iteration run; falling back to Groq's 120B model is a
pragmatic deviation that keeps the agent productive without changing the
contract.
"""
from __future__ import annotations

import logging
import re
import uuid
from typing import Optional

from .gateway import Gateway, GatewayError, get_gateway
from .schemas import (
    Goal,
    MemoryItem,
    Observation,
    PerceivedGoal,
    PerceptionOutput,
)

log = logging.getLogger(__name__)

SYNTHESIS_VERBS = re.compile(
    r"\b(synthesi[sz]e|summari[sz]e|extract|list|compare|decide|choose|select|"
    r"recommend|tell\s+me|give\s+me|answer|report)\b",
    re.IGNORECASE,
)

MAX_FORCE_ATTACH = 3

PERCEPTION_SYSTEM = """\
You are PERCEPTION, the orchestrator of a four-role agentic system.

You will be given:
  USER QUERY      — the original request.
  MEMORY HITS     — relevant rows from the agent's memory. Each row is
                    shown with an index `[i]`; rows that carry an
                    artifact also show `artifact_index: i` and a
                    short descriptor of the bytes.
  RUN HISTORY     — past iterations' tool calls and answers.
  PRIOR GOALS     — the goal list you emitted on the last iteration,
                    if any.

Your job each iteration is to emit an updated goal list as JSON.

OBLIGATIONS (in order):

1. If PRIOR GOALS is empty, decompose the USER QUERY into ONE OR MORE
   bounded goals. Each goal is a short imperative sentence the rest of
   the system can act on independently. Most queries decompose into 1-4
   goals. Do not over-decompose.

2. If PRIOR GOALS is non-empty:
     a) Preserve the order and the count. Do NOT reorder, drop, insert,
        or rename a goal. Position is the goal's identity.
     b) For each goal, examine RUN HISTORY since it was opened. A goal
        becomes `done: true` the moment the history contains an action
        whose result satisfies it (an answer event for it, or a tool
        outcome that provides what the goal asked for). Once `done` is
        true, leave it true forever.

3. Find the FIRST goal that is still `done: false` (the "next unfinished
   goal"). Decide whether it needs the raw bytes of one or more
   artifacts from MEMORY HITS.
     - If the goal needs the contents of artifacts, set
       `artifact_indices` to the list of integer indices of the
       artifact-carrying rows whose bytes Decision must see. For a
       synthesis/comparison/summary goal that draws from N sources,
       include up to N indices (most queries: 1-3 indices).
     - If the goal does not need any artifact bytes, leave
       `artifact_indices` as the empty list `[]`.
     - For all OTHER goals (whether done or not), `artifact_indices`
       MUST be `[]`.

4. Mark a goal `done: true` only when the history fully satisfies it.
   When a goal is for an answer/extraction/synthesis, it is satisfied
   only by an `answer` event in history for that goal — not by a tool
   call that merely fetched the input. Stay conservative: when in doubt,
   leave the goal open and let Decision act on it next turn.

Output strictly valid JSON conforming to the schema. Do not narrate,
do not include the goal id, do not include any commentary."""


def _format_hits(hits: list[MemoryItem]) -> str:
    if not hits:
        return "(none)"
    lines = []
    for i, h in enumerate(hits):
        line = f"[{i}] kind={h.kind} desc={h.descriptor!r}"
        if h.artifact_id:
            line += f" artifact_index={i}"
        lines.append(line)
    return "\n".join(lines)


def _format_history(history: list[dict], max_events: int = 30) -> str:
    if not history:
        return "(none)"
    events = history[-max_events:]
    lines = []
    for ev in events:
        it = ev.get("iter")
        kind = ev.get("kind")
        goal = ev.get("goal_id", "?")
        if kind == "action":
            tool = ev.get("tool", "?")
            args = ev.get("arguments") or {}
            desc = ev.get("result_descriptor", "")
            art = ev.get("artifact_id")
            tail = f" art={art}" if art else ""
            lines.append(
                f"iter {it} ACTION goal={goal} tool={tool} args={args} -> {desc[:140]!r}{tail}"
            )
        elif kind == "answer":
            txt = ev.get("text", "")
            lines.append(f"iter {it} ANSWER goal={goal} text={txt[:200]!r}")
        else:
            lines.append(f"iter {it} {kind} {ev}")
    return "\n".join(lines)


def _format_prior(prior_goals: list[Goal]) -> str:
    if not prior_goals:
        return "(empty — this is iteration 1; decompose the query into goals)"
    return "\n".join(
        f"[{i}] done={g.done} text={g.text!r}" for i, g in enumerate(prior_goals)
    )


def _lift_goals(
    perceived: list[PerceivedGoal],
    prior_goals: list[Goal],
    hits: list[MemoryItem],
) -> list[Goal]:
    """Map positional LLM output back to durable Goal ids, applying
    sticky-done and indexed-artifact safety."""
    index_to_handle: dict[int, str] = {
        i: h.artifact_id for i, h in enumerate(hits) if h.artifact_id
    }

    lifted: list[Goal] = []
    for pos, p in enumerate(perceived):
        if pos < len(prior_goals):
            prior = prior_goals[pos]
            goal_id = prior.id
            text = p.text or prior.text
            done = prior.done or p.done
        else:
            goal_id = uuid.uuid4().hex[:8]
            text = p.text
            done = p.done

        handles: list[str] = []
        seen: set[str] = set()
        for idx in p.artifact_indices:
            if idx in index_to_handle:
                h = index_to_handle[idx]
                if h not in seen:
                    handles.append(h)
                    seen.add(h)
        lifted.append(
            Goal(id=goal_id, text=text, done=done, attach_artifact_ids=handles)
        )

    for i in range(len(perceived), len(prior_goals)):
        prior = prior_goals[i]
        lifted.append(
            Goal(id=prior.id, text=prior.text, done=prior.done, attach_artifact_ids=[])
        )
    return lifted


def _apply_force_attach(goals: list[Goal], hits: list[MemoryItem]) -> list[Goal]:
    """If the next unfinished goal is a synthesis goal and the LLM did
    not pick any attachments, attach the most recent artifacts in MEMORY
    HITS (up to MAX_FORCE_ATTACH). This is the safety net that lets
    Query D and Query B's synthesis turn see multiple sources without
    depending on Perception's reasoning about indices."""
    first_open = next((i for i, g in enumerate(goals) if not g.done), None)
    if first_open is None:
        return goals
    g = goals[first_open]
    if g.attach_artifact_ids:
        return goals
    if not SYNTHESIS_VERBS.search(g.text):
        return goals
    artifact_hits = [h for h in hits if h.artifact_id]
    if not artifact_hits:
        return goals
    ordered = sorted(artifact_hits, key=lambda h: h.created_at, reverse=True)
    handles: list[str] = []
    seen: set[str] = set()
    for h in ordered:
        if h.artifact_id not in seen:
            handles.append(h.artifact_id)
            seen.add(h.artifact_id)
        if len(handles) >= MAX_FORCE_ATTACH:
            break
    goals[first_open] = Goal(
        id=g.id,
        text=g.text,
        done=g.done,
        attach_artifact_ids=handles,
    )
    return goals


def observe(
    query: str,
    hits: list[MemoryItem],
    history: list[dict],
    prior_goals: list[Goal],
    run_id: str,
    *,
    gateway: Optional[Gateway] = None,
) -> Observation:
    """One Perception call. Returns a validated Observation.

    On gateway failure:
      - If we have prior goals, return them unchanged (preserve loop
        state and let the next iteration retry). This is safe because
        sticky-done means no `done` flag will regress.
      - If we have NO prior goals (typically iter 1), re-raise the
        GatewayError so the agent loop can sleep through the actual
        rate-limit window. Synthesising a fake single goal here would
        misdirect Decision into doing the wrong work for many turns.
    """
    gw = gateway or get_gateway()
    schema = PerceptionOutput.model_json_schema()
    user = (
        f"USER QUERY:\n{query}\n\n"
        f"MEMORY HITS:\n{_format_hits(hits)}\n\n"
        f"RUN HISTORY:\n{_format_history(history)}\n\n"
        f"PRIOR GOALS:\n{_format_prior(prior_goals)}\n"
    )

    provider_chain = ["g", "gr", "c"]
    last_err: Optional[GatewayError] = None
    parsed: Optional[dict] = None
    for prov in provider_chain:
        try:
            parsed = gw.structured(
                system=PERCEPTION_SYSTEM,
                user=user,
                schema=schema,
                schema_name="perception_output",
                provider=prov,
                auto_route="perception",
                temperature=1.0,
                max_tokens=1024,
                retries=1,
            )
            if prov != "g":
                log.info("perception fell back to provider=%s", prov)
            break
        except GatewayError as e:
            last_err = e
            continue

    if parsed is None:
        if prior_goals:
            log.warning(
                "perception.observe all providers failed (%s); reusing prior %d goals",
                last_err, len(prior_goals),
            )
            return Observation(goals=prior_goals)
        raise last_err if last_err else GatewayError("perception.observe: no provider available")

    try:
        wire = PerceptionOutput.model_validate(parsed)
    except Exception as e:
        log.warning("perception.observe schema validation failed (%s); reusing prior goals", e)
        if prior_goals:
            return Observation(goals=prior_goals)
        fallback_goal = Goal(id=uuid.uuid4().hex[:8], text=query.strip()[:200], done=False)
        return Observation(goals=[fallback_goal])

    lifted = _lift_goals(wire.goals, prior_goals, hits)
    lifted = _apply_force_attach(lifted, hits)
    return Observation(goals=lifted)
