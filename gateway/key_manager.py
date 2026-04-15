from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from gateway.config import (
    ApiKey,
    CIRCUIT_BREAKER_COOLDOWN,
    CIRCUIT_BREAKER_THRESHOLD,
    DAILY_REQUEST_LIMIT,
    DAILY_TOKEN_LIMIT,
    load_api_keys,
)


class NoHealthyKeyError(Exception):
    pass


def _seconds_until_end_of_day_utc() -> int:
    now = datetime.now(timezone.utc)
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
    remaining = int((end_of_day - now).total_seconds()) + 1
    return max(remaining, 1)


class KeyManager:
    def __init__(self, redis_client: aioredis.Redis) -> None:
        self.redis = redis_client
        self.keys: list[ApiKey] = load_api_keys()

    # ── Redis key helpers ──────────────────────────────────────────

    @staticmethod
    def _rk(suffix: str, field: str) -> str:
        return f"key:{suffix}:{field}"

    # ── Public API ─────────────────────────────────────────────────

    async def get_healthy_key(
        self, exclude: Optional[list[str]] = None
    ) -> Optional[ApiKey]:
        exclude = exclude or []

        for api_key in self.keys:
            if api_key.suffix in exclude:
                continue

            pipe = self.redis.pipeline(transaction=False)
            pipe.get(self._rk(api_key.suffix, "circuit_state"))
            pipe.get(self._rk(api_key.suffix, "daily_requests"))
            pipe.get(self._rk(api_key.suffix, "daily_tokens"))
            circuit_state, daily_req, daily_tok = await pipe.execute()

            if circuit_state == b"open":
                continue

            daily_req_int = int(daily_req) if daily_req else 0
            daily_tok_int = int(daily_tok) if daily_tok else 0

            if daily_req_int >= DAILY_REQUEST_LIMIT:
                continue
            if daily_tok_int >= DAILY_TOKEN_LIMIT:
                continue

            return api_key

        return None

    async def record_success(self, suffix: str, tokens_used: int) -> None:
        ttl = _seconds_until_end_of_day_utc()

        pipe = self.redis.pipeline(transaction=False)
        pipe.incrby(self._rk(suffix, "daily_requests"), 1)
        pipe.incrby(self._rk(suffix, "daily_tokens"), tokens_used)
        pipe.set(self._rk(suffix, "consecutive_errors"), 0)
        pipe.expire(self._rk(suffix, "daily_requests"), ttl)
        pipe.expire(self._rk(suffix, "daily_tokens"), ttl)
        await pipe.execute()

    async def record_failure(self, suffix: str, error_type: str) -> None:
        if error_type == "rate_limit_daily":
            ttl = _seconds_until_end_of_day_utc()
            await self.redis.set(
                self._rk(suffix, "daily_requests"),
                DAILY_REQUEST_LIMIT,
                ex=ttl,
            )
            return

        if error_type == "auth_error":
            await self.redis.set(self._rk(suffix, "circuit_state"), "open")
            return

        # rate_limit_rpm, server_error, timeout
        new_count = await self.redis.incr(
            self._rk(suffix, "consecutive_errors")
        )
        if new_count >= CIRCUIT_BREAKER_THRESHOLD:
            await self.redis.set(
                self._rk(suffix, "circuit_state"),
                "open",
                ex=CIRCUIT_BREAKER_COOLDOWN,
            )
            await self.redis.set(self._rk(suffix, "consecutive_errors"), 0)

    async def get_all_key_statuses(self) -> list[dict]:
        statuses: list[dict] = []

        for api_key in self.keys:
            pipe = self.redis.pipeline(transaction=False)
            pipe.get(self._rk(api_key.suffix, "circuit_state"))
            pipe.get(self._rk(api_key.suffix, "daily_requests"))
            pipe.get(self._rk(api_key.suffix, "daily_tokens"))
            circuit_state, daily_req, daily_tok = await pipe.execute()

            circuit = (circuit_state or b"closed").decode()
            req = int(daily_req) if daily_req else 0
            tok = int(daily_tok) if daily_tok else 0

            healthy = (
                circuit != "open"
                and req < DAILY_REQUEST_LIMIT
                and tok < DAILY_TOKEN_LIMIT
            )

            statuses.append(
                {
                    "suffix": api_key.suffix,
                    "circuit_state": circuit,
                    "daily_requests": req,
                    "daily_tokens": tok,
                    "healthy": healthy,
                }
            )

        return statuses
