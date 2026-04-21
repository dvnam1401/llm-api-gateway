from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from gateway.config import GATEWAY_SECRET, HEALTH_PROBE_INTERVAL, REDIS_URL
from gateway.key_manager import KeyManager, NoHealthyKeyError
from gateway.proxy import UpstreamError, forward_request

# ── Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("gateway")


# ── App state holders ──────────────────────────────────────────────

class State:
    redis_client: aioredis.Redis
    key_manager: KeyManager
    http_client: httpx.AsyncClient


state = State()


# ── Health probe background task ───────────────────────────────────

async def _health_probe_loop() -> None:
    """Periodically probe circuit-open keys and recover them if Groq responds OK."""
    await asyncio.sleep(5)

    # Startup recovery: reset stale circuit states, then probe immediately
    try:
        reset_count = await state.key_manager.reset_all_circuits()
        logger.info("STARTUP_RECOVERY  reset %d circuit keys", reset_count)

        result = await state.key_manager.probe_and_recover(state.http_client)
        logger.info(
            "STARTUP_PROBE  recovered=%s still_open=%s",
            result["recovered"],
            result["still_open"],
        )
    except Exception:
        logger.exception("Startup recovery failed")

    while True:
        await asyncio.sleep(HEALTH_PROBE_INTERVAL)
        try:
            result = await state.key_manager.probe_and_recover(
                state.http_client
            )
            if result["recovered"]:
                logger.info("PROBE_RECOVERED  keys=%s", result["recovered"])
            if result["still_open"]:
                logger.warning(
                    "PROBE_STILL_OPEN  keys=%s", result["still_open"]
                )
        except Exception:
            logger.exception("Health probe error")


# ── Lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    state.redis_client = aioredis.from_url(REDIS_URL, decode_responses=False)
    state.key_manager = KeyManager(state.redis_client)
    state.http_client = httpx.AsyncClient(http2=False)

    key_count = len(state.key_manager.keys)
    logger.info("Gateway started with %d API key(s)", key_count)
    if key_count == 0:
        logger.warning("No GROQ_API_KEY_* env vars found!")

    probe_task = asyncio.create_task(_health_probe_loop())

    yield

    probe_task.cancel()
    try:
        await probe_task
    except asyncio.CancelledError:
        pass
    await state.http_client.aclose()
    await state.redis_client.aclose()
    logger.info("Gateway shut down")


app = FastAPI(title="LLM API Gateway", lifespan=lifespan)


# ── Auth helper ────────────────────────────────────────────────────

class AuthError(Exception):
    pass


def _verify_auth(authorization: str | None) -> None:
    if not GATEWAY_SECRET:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError
    token = authorization[7:]
    if token != GATEWAY_SECRET:
        raise AuthError


# ── Endpoints ──────────────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    try:
        _verify_auth(authorization)
    except AuthError:
        return JSONResponse(
            status_code=401, content={"error": "Unauthorized"}
        )

    body: dict[str, Any] = await request.json()

    try:
        result = await forward_request(
            body, state.key_manager, state.http_client
        )
    except NoHealthyKeyError:
        return JSONResponse(
            status_code=503,
            content={"error": "No healthy API keys available"},
        )
    except UpstreamError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream error", "detail": exc.detail},
        )

    return result


@app.get("/health")
async def health():
    statuses = await state.key_manager.get_all_key_statuses()
    healthy_count = sum(1 for s in statuses if s["healthy"])
    overall = "ok" if healthy_count > 0 else "degraded"

    return {
        "status": overall,
        "keys": statuses,
        "healthy_key_count": healthy_count,
    }
