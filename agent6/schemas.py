"""Pydantic v2 contracts for every boundary in the Session 6 architecture.

Two layers of schemas live here.

1. INTERNAL TYPES carried inside the agent loop:
       MemoryItem, Artifact, Goal, Observation, ToolCall, DecisionOutput.

2. WIRE TYPES used as `response_format` for LLM calls. These are the
   shapes the model is asked to emit; the loop maps them back onto the
   internal types. The split exists because the LLM should never invent
   identifiers (goal ids, artifact handles) — it works with positions
   and integer indices instead, and the loop maps those positions back
   to durable ids on the Python side.

No regex is run on LLM output anywhere in this codebase. The gateway
validates the JSON against these JSON Schemas; we then `model_validate`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MemoryKind = Literal["fact", "preference", "tool_outcome", "scratchpad"]


class MemoryItem(BaseModel):
    """One row of the typed memory store. Persisted to state/memory.json."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: MemoryKind
    keywords: list[str] = Field(default_factory=list)
    descriptor: str
    value: dict[str, Any] = Field(default_factory=dict)
    artifact_id: Optional[str] = None
    source: str
    run_id: str
    goal_id: Optional[str] = None
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("keywords")
    @classmethod
    def _lowercase_keywords(cls, v: list[str]) -> list[str]:
        return [k.lower().strip() for k in v if k and k.strip()]


class Artifact(BaseModel):
    """Metadata for a blob in the content-addressable store. The bytes
    themselves never live in any Pydantic model."""

    model_config = ConfigDict(extra="ignore")

    id: str
    content_type: str
    size_bytes: int
    source: str
    descriptor: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Goal(BaseModel):
    """One bounded sub-task. `id` is durable across iterations; Perception
    never emits it directly (see PerceivedGoal below).

    `attach_artifact_ids` is a list (possibly empty) of artifact handles
    whose raw bytes Decision will see inlined under ATTACHED ARTIFACTS for
    this goal. Most goals attach zero or one artifact; synthesis goals
    (e.g. Query D) attach up to three so Decision can compare sources in
    a single turn.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    done: bool = False
    attach_artifact_ids: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    """Perception's per-iteration output, lifted into internal form."""

    model_config = ConfigDict(extra="ignore")

    goals: list[Goal]

    @property
    def all_done(self) -> bool:
        return bool(self.goals) and all(g.done for g in self.goals)

    def next_unfinished(self) -> Optional[Goal]:
        for g in self.goals:
            if not g.done:
                return g
        return None


class ToolCall(BaseModel):
    """One MCP tool dispatch request."""

    model_config = ConfigDict(extra="ignore")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class DecisionOutput(BaseModel):
    """Exactly one of `answer` or `tool_call` must be populated."""

    model_config = ConfigDict(extra="ignore")

    answer: Optional[str] = None
    tool_call: Optional[ToolCall] = None

    @model_validator(mode="after")
    def _exactly_one(self) -> DecisionOutput:
        has_answer = self.answer is not None and self.answer.strip() != ""
        has_tool = self.tool_call is not None
        if has_answer == has_tool:
            raise ValueError(
                "DecisionOutput must populate exactly one of `answer` or `tool_call`"
            )
        return self

    @property
    def is_answer(self) -> bool:
        return self.answer is not None and self.answer.strip() != ""


class PerceivedGoal(BaseModel):
    """Wire shape Perception emits per goal. No `id` field — the goal's
    identity is its position in the list.

    `artifact_indices` is a list of integer pointers into the MEMORY HITS
    list shown in the prompt; the loop maps each back to a durable `art:`
    handle. An empty list (or omission) means no attachment requested. For
    synthesis goals that need to compare several sources, Perception
    should return multiple indices here.

    `done` is a plain boolean: Perception marks goals done by re-reading
    the history every iteration. Once true, the loop keeps it true
    (sticky-done is enforced in perception.py, not at schema level).
    """

    model_config = ConfigDict(extra="ignore")

    text: str = Field(..., description="Short imperative description of the goal.")
    done: bool = Field(..., description="True iff the history already satisfies this goal.")
    artifact_indices: list[int] = Field(
        default_factory=list,
        description=(
            "Zero or more integer indices into the MEMORY HITS list. Each "
            "index points to an artifact-carrying row whose raw bytes will "
            "be attached to Decision's prompt for this goal. Use only for "
            "the first unfinished goal; for all other goals, leave this "
            "empty. For a synthesis goal that compares multiple sources, "
            "include one index per source you want Decision to see."
        ),
    )


class PerceptionOutput(BaseModel):
    """Top-level wire shape Perception emits. Field name `goals` matches
    Observation so the loop can lift positions onto durable Goal ids."""

    model_config = ConfigDict(extra="ignore")

    goals: list[PerceivedGoal] = Field(
        ...,
        description="The full ordered goal list. Order is stable across iterations.",
    )


class MemoryClassification(BaseModel):
    """Wire shape memory.remember() asks the classifier to emit.

    The classifier returns `kind="none"` when the input contains no
    durable fact or preference worth persisting (a generic question, for
    example, where the user is asking rather than asserting). The loop
    skips writing in that case.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["fact", "preference", "scratchpad", "none"] = Field(
        ...,
        description=(
            "Pick `fact` for an observable truth the user states, `preference` for "
            "a user-stated taste/choice, `scratchpad` for a short-lived working "
            "note, and `none` when there is nothing durable to store."
        ),
    )
    descriptor: str = Field(
        ..., description="One-line human-readable summary of what is being stored."
    )
    keywords: list[str] = Field(
        ...,
        description=(
            "Lowercase tokens (people, dates, entities, topics) future "
            "keyword searches should match. Include any names, months, "
            "years and topical nouns appearing in the input."
        ),
    )
    value: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured payload (e.g. {entity, attribute, value} for a fact).",
    )
    confidence: float = Field(
        default=0.9, ge=0.0, le=1.0, description="0..1 confidence in the classification."
    )
