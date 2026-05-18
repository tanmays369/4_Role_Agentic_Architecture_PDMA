"""CLI for the agent6 package.

Run with uv:
    uv run python -m agent6 --query A
    uv run python -m agent6 --query B
    uv run python -m agent6 --query C1
    uv run python -m agent6 --query C2
    uv run python -m agent6 --query D
    uv run python -m agent6 --text "What time is it in Tokyo?"
    uv run python -m agent6 --query C1 --clean-state

The four canonical assignment queries are baked in below. Use
--clean-state to wipe state/memory.json and state/artifacts/ before
running (useful for capturing the README's clean-state traces).
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from .agent import WORKSPACE_ROOT, run_sync

QUERIES: dict[str, str] = {
    "A": (
        "Fetch https://en.wikipedia.org/wiki/Claude_Shannon and tell me his "
        "birth date, death date, and three key contributions to information "
        "theory."
    ),
    "B": (
        "Find 3 family-friendly things to do in Tokyo this weekend. Check "
        "Saturday's weather forecast there and tell me which one is most "
        "appropriate."
    ),
    "C1": (
        "My mom's birthday is 15 May 2026. Remember that and give me a "
        "calendar reminder for two weeks before and on the day."
    ),
    "C2": "When is mom's birthday?",
    "D": (
        "Search for 'Python asyncio best practices', read the top 3 results, "
        "and give me a short numbered list of the advice they agree on."
    ),
}


def _clean_state() -> None:
    state_dir = WORKSPACE_ROOT / "state"
    mem = state_dir / "memory.json"
    arts = state_dir / "artifacts"
    if mem.exists():
        mem.unlink()
        print(f"[clean-state] removed {mem}", file=sys.stderr)
    if arts.exists():
        shutil.rmtree(arts)
        print(f"[clean-state] removed {arts}", file=sys.stderr)
    state_dir.mkdir(exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent6",
        description="EAG V3 Session 6 — four-role agentic architecture.",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--query",
        choices=sorted(QUERIES.keys()),
        help="One of the four canonical assignment queries (A | B | C1 | C2 | D).",
    )
    grp.add_argument(
        "--text",
        type=str,
        help="Free-form query text to run instead of one of A/B/C1/C2/D.",
    )
    parser.add_argument(
        "--clean-state",
        action="store_true",
        help="Wipe state/memory.json and state/artifacts/ before running.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Pin the run_id (default: random 8-hex). Useful for log correlation.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level for agent modules.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.clean_state:
        _clean_state()

    query = QUERIES[args.query] if args.query else args.text
    run_sync(query, run_id=args.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
