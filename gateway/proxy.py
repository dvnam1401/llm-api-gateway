from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

import httpx
from fastapi.responses import StreamingResponse

from gateway.config import GROQ_API_BASE, MODEL, REQUEST_TIMEOUT
from gateway.key_manager import KeyManager, NoHealthyKeyError

logger = logging.getLogger("gateway")

GROQ_CHAT_URL = f"{GROQ_API_BASE}/chat/completions"


class UpstreamError(Exception):
    """Non-recoverable error from Groq that should be surfaced as 502."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


# ── Error classification ───────────────────────────────────────────


def _classify_error(response: httpx.Response) -> str:
    status = response.status_code

    if status == 401 or status == 403:
        return "auth_error"

    if status == 429:
        body = response.text.lower()
        if "daily" in body or "per day" in body or "tokens per day" in body:
            return "rate_limit_daily"
        return "rate_limit_rpm"

    if status >= 500:
        return "server_error"

    return "server_error"


# ── Streaming helpers ──────────────────────────────────────────────


async def _stream_response(
    resp: httpx.Response,
    key_manager: KeyManager,
    suffix: str,
    start_time: float,
) -> AsyncIterator[bytes]:
    """Yield SSE chunks and record usage from the final chunk."""
    total_tokens = 0

    try:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    yield f"data: [DONE]\n\n".encode()
                    break

                try:
                    chunk = json.loads(data_str)
                    usage = chunk.get("usage") or (
                        chunk.get("x_groq", {}).get("usage")
                    )
                    if usage:
                        total_tokens = usage.get("total_tokens", 0)
                except json.JSONDecodeError:
                    pass

                yield f"{line}\n\n".encode()
            elif line.strip():
                yield f"{line}\n\n".encode()
    except Exception:
        logger.exception("Stream interrupted for key=%s", suffix)
    finally:
        latency = time.monotonic() - start_time
        if total_tokens > 0:
            await key_manager.record_success(suffix, total_tokens)
            logger.info(
                "SUCCESS  key=%s tokens=%d latency=%.1fs (stream)",
                suffix,
                total_tokens,
                latency,
            )
        else:
            logger.warning(
                "STREAM_END  key=%s tokens=unknown latency=%.1fs",
                suffix,
                latency,
            )


# ── Main proxy logic ──────────────────────────────────────────────


async def forward_request(
    request_body: dict[str, Any],
    key_manager: KeyManager,
    http_client: httpx.AsyncClient,
) -> dict | StreamingResponse:
    request_body["model"] = MODEL
    is_stream = request_body.get("stream", False)

    logger.info(
        "REQUEST  model=%s stream=%s", MODEL, str(is_stream).lower()
    )

    tried_suffixes: list[str] = []

    while True:
        api_key = await key_manager.get_healthy_key(exclude=tried_suffixes)
        if api_key is None:
            raise NoHealthyKeyError("No healthy API keys available")

        tried_suffixes.append(api_key.suffix)
        logger.info("ATTEMPT  key=%s", api_key.suffix)
        start_time = time.monotonic()

        headers = {
            "Authorization": f"Bearer {api_key.key}",
            "Content-Type": "application/json",
        }

        try:
            if is_stream:
                # Streaming: send request with stream context
                req = http_client.build_request(
                    "POST",
                    GROQ_CHAT_URL,
                    headers=headers,
                    json=request_body,
                    timeout=REQUEST_TIMEOUT,
                )
                resp = await http_client.send(req, stream=True)

                if resp.status_code != 200:
                    body = await resp.aread()
                    error_type = _classify_error(resp)
                    logger.info(
                        "FAILURE  key=%s reason=%s", api_key.suffix, error_type
                    )
                    await key_manager.record_failure(api_key.suffix, error_type)
                    await resp.aclose()
                    continue

                return StreamingResponse(
                    _stream_response(resp, key_manager, api_key.suffix, start_time),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                    },
                )
            else:
                # Non-streaming
                resp = await http_client.post(
                    GROQ_CHAT_URL,
                    headers=headers,
                    json=request_body,
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code != 200:
                    error_type = _classify_error(resp)
                    logger.info(
                        "FAILURE  key=%s reason=%s", api_key.suffix, error_type
                    )
                    await key_manager.record_failure(api_key.suffix, error_type)
                    continue

                data = resp.json()
                total_tokens = (
                    data.get("usage", {}).get("total_tokens", 0)
                )
                latency = time.monotonic() - start_time

                await key_manager.record_success(api_key.suffix, total_tokens)
                logger.info(
                    "SUCCESS  key=%s tokens=%d latency=%.1fs",
                    api_key.suffix,
                    total_tokens,
                    latency,
                )
                return data

        except httpx.TimeoutException:
            logger.info("FAILURE  key=%s reason=timeout", api_key.suffix)
            await key_manager.record_failure(api_key.suffix, "timeout")
            continue

        except httpx.HTTPError:
            logger.info(
                "FAILURE  key=%s reason=server_error", api_key.suffix
            )
            await key_manager.record_failure(api_key.suffix, "server_error")
            continue
