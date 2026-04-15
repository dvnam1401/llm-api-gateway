from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class ApiKey:
    suffix: str
    key: str


def load_api_keys() -> list[ApiKey]:
    """Scan env vars with prefix GROQ_API_KEY_, return sorted by suffix."""
    prefix = "GROQ_API_KEY_"
    keys: list[ApiKey] = []
    for name, value in os.environ.items():
        if name.startswith(prefix) and value:
            suffix = name[len(prefix):]
            keys.append(ApiKey(suffix=suffix, key=value))
    keys.sort(key=lambda k: k.suffix)
    return keys


GATEWAY_SECRET: str = os.getenv("GATEWAY_SECRET", "")
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "60"))
CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_COOLDOWN: int = int(os.getenv("CIRCUIT_BREAKER_COOLDOWN", "300"))

MODEL: str = "qwen/qwen3-32b"
GROQ_API_BASE: str = "https://api.groq.com/openai/v1"

DAILY_REQUEST_LIMIT: int = 1000
DAILY_TOKEN_LIMIT: int = 500_000
