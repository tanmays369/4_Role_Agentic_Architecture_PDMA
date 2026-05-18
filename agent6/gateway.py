"""Thin client around the LLM Gateway V3 on http://localhost:8101.

Every LLM call in the four cognitive roles goes through here. There are
no direct provider SDK imports anywhere else in the package. This module
is intentionally small: it wraps `POST /v1/chat`, exposes a JSON-schema
helper, and gives a single ensure_gateway() health probe the main loop
runs once at startup.

The shape of the response is documented in llm_gatewayV3/README.md. The
fields we read are:
    text                       (str, may be empty when tool_calls is set)
    tool_calls                 (list of {id,name,arguments,provider_meta})
    parsed                     (dict, set when response_format used and
                                JSON Schema validation passed gateway-side)
    router_decision            (dict | None, when auto_route was used)
    stop_reason                (informational)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Literal, Optional

import httpx

DEFAULT_BASE_URL = os.getenv("LLM_GATEWAY_V3_URL", "http://localhost:8101")
DEFAULT_TIMEOUT = float(os.getenv("AGENT6_GATEWAY_TIMEOUT", "180"))

DEFAULT_RETRIES = int(os.getenv("AGENT6_GATEWAY_RETRIES", "4"))
DEFAULT_MAX_BACKOFF = float(os.getenv("AGENT6_GATEWAY_MAX_BACKOFF", "80"))

_COOLDOWN_RE = re.compile(
    r"cooldown\s*\(([\d.]+)s\)|backoff[^()]*\((\d+)s\s+left\)",
    re.IGNORECASE,
)

log = logging.getLogger(__name__)

AutoRoute = Literal["perception", "memory", "decision"]


def _parse_cooldown_seconds(detail: Any) -> float:
    """Pull the cooldown hint out of a gateway error body. The gateway
    surfaces these strings (see llm_gatewayV3/main.py):
        'cooldown (1.2s)'
        'backoff: RPM quota burned (60s left)'
    """
    if not detail:
        return 0.0
    text = detail if isinstance(detail, str) else json.dumps(detail)
    m = _COOLDOWN_RE.search(text)
    if not m:
        return 0.0
    raw = m.group(1) or m.group(2)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


class GatewayError(RuntimeError):
    """Raised when /v1/chat returns a non-2xx response, or when the gateway
    is unreachable. Carries the gateway's body so the caller can show a
    useful error."""

    def __init__(self, message: str, *, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class Gateway:
    """Wrapper around POST /v1/chat. One instance is shared by all roles."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(
        self,
        *,
        prompt: Optional[str] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        system: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        cache_system: Optional[bool] = None,
        reasoning: Optional[str] = None,
        response_format: Optional[dict[str, Any]] = None,
        auto_route: Optional[AutoRoute] = None,
        retries: int = DEFAULT_RETRIES,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> dict[str, Any]:
        body = {
            "prompt": prompt,
            "messages": messages,
            "system": system,
            "provider": provider,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
            "cache_system": cache_system,
            "reasoning": reasoning,
            "response_format": response_format,
            "auto_route": auto_route,
        }
        body = {k: v for k, v in body.items() if v is not None}

        attempt = 0
        last_detail: Any = None
        last_status = 0
        while attempt < max(1, retries):
            attempt += 1
            try:
                r = httpx.post(
                    f"{self.base_url}/v1/chat", json=body, timeout=self.timeout
                )
            except httpx.HTTPError as e:
                if attempt < retries:
                    log.warning("gateway transport error (%s); retrying", e)
                    time.sleep(2.0)
                    continue
                raise GatewayError(
                    f"could not reach LLM Gateway V3 at {self.base_url}: {e}. "
                    f"Start it with `cd llm_gatewayV3 && ./run.sh`."
                ) from e

            if r.status_code < 400:
                return r.json()

            try:
                last_detail = r.json()
            except json.JSONDecodeError:
                last_detail = r.text
            last_status = r.status_code

            if r.status_code in (502, 503) and attempt < retries:
                wait = _parse_cooldown_seconds(last_detail)
                if wait <= 0:
                    wait = 2.0 * attempt
                wait = min(wait + 0.4, max_backoff)
                log.info(
                    "gateway %s on attempt %d (sleeping %.1fs before retry)",
                    r.status_code, attempt, wait,
                )
                time.sleep(wait)
                continue

            raise GatewayError(
                f"gateway returned HTTP {r.status_code}: {last_detail}",
                status=r.status_code,
                body=last_detail,
            )

        raise GatewayError(
            f"gateway returned HTTP {last_status} after {retries} attempts: {last_detail}",
            status=last_status,
            body=last_detail,
        )

    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "out",
        provider: Optional[str] = None,
        auto_route: Optional[AutoRoute] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        cache_system: Optional[bool] = None,
        retries: int = DEFAULT_RETRIES,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
    ) -> dict[str, Any]:
        """Call /v1/chat with a JSON-Schema response_format. Returns the
        parsed dict (already validated by the gateway against `schema`).
        Raises GatewayError if the gateway couldn't get the model to
        produce schema-conformant JSON after its built-in single retry.
        """
        resp = self.chat(
            messages=[{"role": "user", "content": user}],
            system=system,
            provider=provider,
            auto_route=auto_route,
            temperature=temperature,
            max_tokens=max_tokens,
            cache_system=cache_system,
            response_format={
                "type": "json_schema",
                "schema": schema,
                "name": schema_name,
                "strict": True,
            },
            retries=retries,
            max_backoff=max_backoff,
        )
        parsed = resp.get("parsed")
        if parsed is None:
            text = resp.get("text", "")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise GatewayError(
                    f"structured-output call returned no parsed payload "
                    f"and unparseable text: {text[:300]!r}"
                ) from e
        return parsed


_GATEWAY: Optional[Gateway] = None


def get_gateway() -> Gateway:
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = Gateway()
    return _GATEWAY


def ensure_gateway(*, retries: int = 3, delay: float = 1.0) -> Gateway:
    """Probe /v1/routers to confirm V3 is up before the agent loop runs.
    Raises GatewayError with operator-facing guidance if it isn't."""
    gw = get_gateway()
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = httpx.get(f"{gw.base_url}/v1/routers", timeout=5)
            if r.status_code == 200:
                return gw
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as e:
            last_err = e
        if attempt < retries - 1:
            time.sleep(delay)
    raise GatewayError(
        f"LLM Gateway V3 not reachable at {gw.base_url}. "
        f"Start it in another terminal with `cd llm_gatewayV3 && ./run.sh` "
        f"and ensure the .env in the workspace root has at least "
        f"GEMINI_API_KEY (Perception is pinned to Gemini). Last error: {last_err}"
    )
