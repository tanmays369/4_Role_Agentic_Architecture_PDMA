"""Action — pure MCP dispatcher.

Receives a ToolCall and a live MCP ClientSession. Dispatches the call,
collapses the result's content blocks into a single string, and:

  * if the result is larger than ARTIFACT_THRESHOLD_BYTES (4 KB),
    persists the bytes to the ArtifactStore and returns a short
    descriptor of the form
        "[artifact art:abc..., 263507 bytes] preview: ..."
    plus the new artifact's handle.

  * otherwise returns the raw text and a None artifact_id.

Two guards:

  1. If any tool argument value starts with `art:`, refuse the
     dispatch and return a clear error string. This blocks the
     well-known failure mode where a small Decision model treats an
     artifact handle as a path or URL.

  2. If the MCP server reports an error (CallToolResult.isError=True),
     return the error text with a `[mcp_error]` prefix so the history
     records it and the next Perception turn can react.

No LLM call in this module. ~50 lines of dispatch + guards.
"""
from __future__ import annotations

import logging
from typing import Optional

from mcp import ClientSession

from .artifacts import ArtifactStore
from .schemas import ToolCall

log = logging.getLogger(__name__)

ARTIFACT_THRESHOLD_BYTES = 4096
PATHISH_ARG_KEYS = frozenset({"path", "url", "file", "filepath", "source", "src"})
PREVIEW_CHARS = 240


def _collapse_content(blocks) -> tuple[str, str]:
    """Collapse MCP content blocks to (text, primary_content_type).

    MCP TextContent blocks have `.text`. Image / Resource blocks are
    serialized as a JSON-ish placeholder so the descriptor still says
    something meaningful."""
    parts: list[str] = []
    content_type = "text/plain"
    for blk in blocks or []:
        text = getattr(blk, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        type_name = getattr(blk, "type", "unknown")
        parts.append(f"[non-text block: {type_name}]")
        content_type = "application/octet-stream"
    return "\n".join(parts), content_type


async def execute(
    session: ClientSession,
    tool_call: ToolCall,
    *,
    artifacts: Optional[ArtifactStore] = None,
) -> tuple[str, Optional[str]]:
    """Dispatch one MCP tool call. Returns (descriptor, artifact_id?).

    The descriptor is what goes into the run history; it must always be
    short (<= ~300 chars). The artifact_id is set only when the result
    crossed the threshold and was persisted to the store.
    """
    art_store = artifacts if artifacts is not None else ArtifactStore()

    for k, v in tool_call.arguments.items():
        if isinstance(v, str) and v.startswith("art:") and (
            k.lower() in PATHISH_ARG_KEYS
            or any(needle in k.lower() for needle in ("path", "url", "file"))
        ):
            return (
                f"[guard] refused {tool_call.name}: argument {k!r}={v!r} is an "
                f"artifact handle, not a path/URL. Decision must read attached "
                f"bytes from ATTACHED ARTIFACTS instead."
            ), None

    try:
        result = await session.call_tool(tool_call.name, arguments=tool_call.arguments)
    except Exception as e:
        msg = f"[mcp_error] {tool_call.name} raised: {e}"
        log.warning(msg)
        return msg, None

    text, content_type = _collapse_content(getattr(result, "content", []))

    if getattr(result, "isError", False):
        snippet = text[:PREVIEW_CHARS].replace("\n", " ")
        return f"[mcp_error] {tool_call.name}: {snippet}", None

    blob = text.encode("utf-8")
    if len(blob) > ARTIFACT_THRESHOLD_BYTES:
        handle = art_store.put(
            blob,
            content_type=content_type,
            source=tool_call.name,
            descriptor=f"{tool_call.name}({tool_call.arguments})",
        )
        preview = text[:PREVIEW_CHARS].replace("\n", " ").strip()
        descriptor = (
            f"[artifact {handle}, {len(blob)} bytes] preview: {preview}"
        )
        return descriptor, handle

    return text, None
