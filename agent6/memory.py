"""Memory as a typed service.

Backed by a single JSON file at state/memory.json. Other roles call:

    memory.read(query, history)         -> list[MemoryItem]   (keyword, no LLM)
    memory.filter(kind=..., goal_id=..) -> list[MemoryItem]   (structured, no LLM)
    memory.relevant(query, kinds=...)   -> list[MemoryItem]   (LLM-scored, optional)
    memory.remember(text, ...)          -> MemoryItem | None  (one LLM classify call)
    memory.record_outcome(...)          -> MemoryItem         (deterministic, no LLM)

The keyword search is pure Python: lowercase tokenisation, stopword
filtering, intersection over (descriptor tokens ∪ keywords). It scales
to hundreds of items and is fast enough to run before every Perception
call. Session 7 swaps the backend (vector + BM25 + RRF) without
changing this interface.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from .gateway import Gateway, GatewayError, get_gateway
from .schemas import MemoryClassification, MemoryItem, MemoryKind, ToolCall

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "state" / "memory.json"

_STOPWORDS = frozenset(
    """
    a an the of to in on at for from with by and or but is are was were be been
    being have has had do does did will would should can could may might must
    this that these those it its as into about than then so if not no yes i you
    he she they we me my your our their his her them us
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


CLASSIFIER_SYSTEM = """\
You are the Memory classifier for an agentic system. Your job is to look
at one piece of raw input text and emit a typed memory record.

PICK A KIND:
  - fact:        a durable observable truth the speaker is asserting
                 (e.g. "John's office is in HSR Layout").
  - preference:  a stated taste, choice, or constraint of a user
                 (e.g. "I prefer morning meetings", "no spicy food").
  - scratchpad:  a short-lived working note useful only this run
                 (intermediate planner state, partial deductions).
  - none:        the input contains no durable content worth storing.
                 Use this for generic questions ("when is mom's
                 birthday?"), thanks/greetings, command-only inputs,
                 or anything purely interrogative.

KEYWORDS RULES:
  Emit a list of lowercase tokens covering every named entity, date
  fragment (month names, years, weekdays), topical noun, and other word
  that a future keyword search might use to find this record.
  Do NOT include stopwords (a, the, is, of, ...).
  Examples:
    "Mom's birthday is 15 May 2026"
      -> ["mom","mother","birthday","may","2026","15"]
    "I prefer dark roast coffee in the morning"
      -> ["coffee","dark","roast","morning","preference"]

VALUE RULES:
  Put a structured representation here. For facts, prefer
  {"entity": ..., "attribute": ..., "value": ...}. For preferences,
  prefer {"subject": ..., "preference": ...}. Keep it minimal.

DESCRIPTOR RULES:
  One short human-readable sentence summarising what is being stored.

Output strictly valid JSON conforming to the schema. Do not add
commentary, markdown fences, or any text outside the JSON object."""


class MemoryService:
    """JSON-backed typed memory store."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        *,
        gateway: Optional[Gateway] = None,
    ):
        self.path = Path(path) if path else _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: list[MemoryItem] = []
        self._gateway = gateway
        self._load()

    @property
    def gateway(self) -> Gateway:
        if self._gateway is None:
            self._gateway = get_gateway()
        return self._gateway

    def _load(self) -> None:
        if not self.path.exists():
            self._items = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("memory.json unreadable (%s); starting empty", e)
            self._items = []
            return
        items: list[MemoryItem] = []
        for row in raw:
            try:
                items.append(MemoryItem.model_validate(row))
            except Exception as e:
                log.warning("dropping malformed memory row: %s", e)
        self._items = items

    def _save(self) -> None:
        payload = [json.loads(it.model_dump_json()) for it in self._items]
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def all_items(self) -> list[MemoryItem]:
        return list(self._items)

    def read(
        self,
        query: str,
        history: Optional[list[dict]] = None,
        *,
        kinds: Optional[Iterable[MemoryKind]] = None,
        top_k: int = 8,
    ) -> list[MemoryItem]:
        """Keyword-overlap ranked top-k. No LLM call."""
        kinds_set = set(kinds) if kinds else None
        q_tokens: set[str] = set(_tokens(query))
        if history:
            for ev in history[-6:]:
                if ev.get("kind") == "action":
                    q_tokens.update(_tokens(str(ev.get("tool", ""))))
                    for v in (ev.get("arguments") or {}).values():
                        q_tokens.update(_tokens(str(v)))
                elif ev.get("kind") == "answer":
                    q_tokens.update(_tokens(str(ev.get("text", ""))))
        if not q_tokens:
            return []

        scored: list[tuple[float, MemoryItem]] = []
        for it in self._items:
            if kinds_set is not None and it.kind not in kinds_set:
                continue
            haystack = set(it.keywords) | set(_tokens(it.descriptor))
            if not haystack:
                continue
            hits = q_tokens & haystack
            if not hits:
                continue
            score = float(len(hits))
            scored.append((score, it))

        scored.sort(
            key=lambda p: (p[0], p[1].created_at.timestamp()),
            reverse=True,
        )
        return [it for _, it in scored[:top_k]]

    def filter(
        self,
        *,
        kinds: Optional[Iterable[MemoryKind]] = None,
        goal_id: Optional[str] = None,
        run_id: Optional[str] = None,
        recent: Optional[int] = None,
    ) -> list[MemoryItem]:
        """Structured filter. No LLM call."""
        kinds_set = set(kinds) if kinds else None
        out = []
        for it in self._items:
            if kinds_set is not None and it.kind not in kinds_set:
                continue
            if goal_id is not None and it.goal_id != goal_id:
                continue
            if run_id is not None and it.run_id != run_id:
                continue
            out.append(it)
        out.sort(key=lambda i: i.created_at, reverse=True)
        if recent is not None:
            out = out[:recent]
        return out

    def relevant(
        self,
        query: str,
        *,
        kinds: Optional[Iterable[MemoryKind]] = None,
        top_k: int = 5,
    ) -> list[MemoryItem]:
        """LLM-scored fallback when keyword recall is weak. Spends one
        gateway call routed `auto_route="memory"`. Used sparingly."""
        candidates = self.filter(kinds=kinds) or self.all_items()
        if not candidates:
            return []
        if len(candidates) <= top_k:
            return candidates
        listing = "\n".join(
            f"[{i}] kind={c.kind} desc={c.descriptor!r}" for i, c in enumerate(candidates)
        )
        schema = {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                }
            },
            "required": ["indices"],
            "additionalProperties": False,
        }
        try:
            parsed = self.gateway.structured(
                system=(
                    "You score memory rows for relevance to a user query. "
                    "Return the indices of the top-k most relevant rows, "
                    "best first."
                ),
                user=f"Query: {query}\nReturn at most {top_k} indices.\n\nRows:\n{listing}",
                schema=schema,
                schema_name="relevant_indices",
                auto_route="memory",
                temperature=0.2,
            )
            picks = [candidates[i] for i in parsed.get("indices", []) if 0 <= i < len(candidates)]
            return picks[:top_k]
        except GatewayError as e:
            log.warning("memory.relevant gateway failure (%s); returning recent", e)
            return candidates[:top_k]

    def remember(
        self,
        raw_text: str,
        *,
        source: str,
        run_id: str,
        goal_id: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Classify free-form text and persist, unless classifier says
        there is nothing to store. One LLM call (Gemini first, then
        Groq / Cerebras as fallback so the durable-memory write at the
        top of every run does not silently no-op when Gemini is
        rate-limited)."""
        text = (raw_text or "").strip()
        if not text:
            return None
        schema = MemoryClassification.model_json_schema()
        parsed = None
        last_err: Optional[GatewayError] = None
        for prov in ("g", "gr", "c"):
            try:
                parsed = self.gateway.structured(
                    system=CLASSIFIER_SYSTEM,
                    user=text,
                    schema=schema,
                    schema_name="memory_classification",
                    provider=prov,
                    auto_route="memory",
                    temperature=1.0,
                    max_tokens=512,
                )
                if prov != "g":
                    log.info("memory.remember fell back to provider=%s", prov)
                break
            except GatewayError as e:
                last_err = e
                continue
        if parsed is None:
            log.warning("memory.remember classifier failed on all providers (%s); skipping write", last_err)
            return None
        try:
            classification = MemoryClassification.model_validate(parsed)
        except Exception as e:
            log.warning("memory.remember classifier output did not validate (%s); skipping", e)
            return None
        if classification.kind == "none":
            return None
        item = MemoryItem(
            id=uuid.uuid4().hex[:12],
            kind=classification.kind,
            keywords=classification.keywords,
            descriptor=classification.descriptor,
            value=classification.value,
            artifact_id=None,
            source=source,
            run_id=run_id,
            goal_id=goal_id,
            confidence=classification.confidence,
            created_at=datetime.now(timezone.utc),
        )
        self._items.append(item)
        self._save()
        return item

    def record_outcome(
        self,
        *,
        tool_call: ToolCall,
        result_text: str,
        artifact_id: Optional[str],
        run_id: str,
        goal_id: Optional[str] = None,
    ) -> MemoryItem:
        """Record an MCP dispatch outcome. Kind is tool_outcome by
        construction. Keywords are derived from the tool name and
        argument string tokens — no LLM call."""
        keywords = set(_tokens(tool_call.name))
        for v in tool_call.arguments.values():
            keywords.update(_tokens(str(v)))
        keywords.update(_tokens(result_text[:200]))
        descriptor = f"{tool_call.name}({', '.join(f'{k}={v!r}' for k, v in tool_call.arguments.items())[:120]}) -> "
        if artifact_id:
            descriptor += f"{artifact_id} ({len(result_text)} chars descriptor)"
        else:
            preview = result_text[:120].replace("\n", " ")
            descriptor += preview
        item = MemoryItem(
            id=uuid.uuid4().hex[:12],
            kind="tool_outcome",
            keywords=sorted(keywords),
            descriptor=descriptor,
            value={
                "tool": tool_call.name,
                "arguments": tool_call.arguments,
                "result_preview": result_text[:500],
            },
            artifact_id=artifact_id,
            source="action",
            run_id=run_id,
            goal_id=goal_id,
            confidence=1.0,
            created_at=datetime.now(timezone.utc),
        )
        self._items.append(item)
        self._save()
        return item

    def latest_artifact_handle(self) -> Optional[str]:
        """Most recently written tool_outcome that carries an artifact.
        Used by Perception's force-attach safety net for synthesis goals
        when the LLM does not pick an artifact_index itself."""
        for it in sorted(self._items, key=lambda i: i.created_at, reverse=True):
            if it.kind == "tool_outcome" and it.artifact_id:
                return it.artifact_id
        return None
