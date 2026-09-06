"""Structured JSON event logging with redaction.

We log observable decisions and tool activity, never model reasoning and never anything from
the redaction list. Every event carries a ``run_id`` when one exists.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

REDACT_KEYS = {
    "cookie", "cookies", "authorization", "set-cookie", "session", "session_id", "password",
    "api_key", "openai_api_key", "address", "account_name", "email", "phone", "token",
}

_LOGGER_NAME = "shopping_hunter"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(payload, default=str, ensure_ascii=False)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if str(k).lower() in REDACT_KEYS else redact(v)) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 500:
        return obj[:500] + "…"
    return obj


def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(_JsonFormatter())
        logger.addHandler(h)
        logger.propagate = False
    logger.setLevel(level.upper())
    return logger


def log_event(event: str, run_id: str | None = None, level: int = logging.INFO, **fields: Any) -> None:
    logger = logging.getLogger(_LOGGER_NAME)
    if run_id:
        fields = {"run_id": run_id, **fields}
    logger.log(level, event, extra={"event": event, "fields": fields})
