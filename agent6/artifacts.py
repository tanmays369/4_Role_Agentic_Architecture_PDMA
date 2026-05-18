"""Content-addressable artifact store.

Lives parallel to Memory. Memory carries only the `art:` handle string;
the bytes themselves never enter any Pydantic model in the loop. The
store deduplicates by sha256, so identical fetches share storage.

On-disk layout under state/artifacts/:
    art_<sha-prefix>.bin   raw bytes
    art_<sha-prefix>.json  Artifact metadata (size, source, descriptor)

Handles always have the form `art:<sha-prefix>` with a 16-char prefix
(64 bits of collision space — overkill for one student's run, sufficient
for human readability in trace output).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Union

from .schemas import Artifact

HANDLE_PREFIX = "art:"
HANDLE_HASH_LEN = 16
_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "state" / "artifacts"


def _hash_to_handle(blob: bytes) -> str:
    return HANDLE_PREFIX + hashlib.sha256(blob).hexdigest()[:HANDLE_HASH_LEN]


def _stem_for_handle(handle: str) -> str:
    if not handle.startswith(HANDLE_PREFIX):
        raise ValueError(f"not an artifact handle: {handle!r}")
    return "art_" + handle[len(HANDLE_PREFIX):]


class ArtifactStore:
    """Two-files-per-artifact CAS on the local filesystem.

    Thread-safety: not needed in S6 (single-loop agent), so omitted.
    Eviction: none in S6 — the spec calls it out as a simplification."""

    def __init__(self, root: Union[str, Path, None] = None):
        self.root = Path(root) if root else _DEFAULT_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        blob: bytes,
        *,
        content_type: str,
        source: str,
        descriptor: str,
    ) -> str:
        """Store bytes; return the durable handle. Dedup by sha256."""
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        handle = _hash_to_handle(blob)
        stem = _stem_for_handle(handle)
        bin_path = self.root / f"{stem}.bin"
        meta_path = self.root / f"{stem}.json"
        if not bin_path.exists():
            bin_path.write_bytes(blob)
        if not meta_path.exists():
            meta = Artifact(
                id=handle,
                content_type=content_type,
                size_bytes=len(blob),
                source=source,
                descriptor=descriptor,
            )
            meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        return handle

    def exists(self, handle: str) -> bool:
        if not handle.startswith(HANDLE_PREFIX):
            return False
        stem = _stem_for_handle(handle)
        return (self.root / f"{stem}.bin").exists()

    def get_bytes(self, handle: str) -> bytes:
        stem = _stem_for_handle(handle)
        path = self.root / f"{stem}.bin"
        if not path.exists():
            raise KeyError(f"unknown artifact handle: {handle}")
        return path.read_bytes()

    def get_meta(self, handle: str) -> Artifact:
        stem = _stem_for_handle(handle)
        path = self.root / f"{stem}.json"
        if not path.exists():
            raise KeyError(f"unknown artifact handle: {handle}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Artifact.model_validate(data)

    def get_text(self, handle: str, *, max_bytes: int | None = None) -> str:
        """Decode the artifact as UTF-8 text. `max_bytes` caps how much
        is returned to keep Decision's prompt size predictable."""
        blob = self.get_bytes(handle)
        if max_bytes is not None and len(blob) > max_bytes:
            blob = blob[:max_bytes]
        return blob.decode("utf-8", errors="replace")
