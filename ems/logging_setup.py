"""Structured JSON logging with structlog. Module + trade_id context."""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

# Env vars whose VALUES are secrets and must never appear in any output sink
# (logs, the SQLite notification feed / order journal, or the dashboard detail).
_SECRET_ENV_VARS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_SECRET",
    "POLYMARKET_PASSPHRASE",
)
_REDACTION = "<redacted:secret>"


def _secret_values() -> set[str]:
    """Current secret values to scrub, in every form they might be rendered.

    We redact the EXACT known values (not a 0x+64hex pattern) on purpose: order
    ids, condition ids, token ids and tx hashes are also 0x+64hex, and masking
    by pattern would destroy legitimate, non-secret log/journal fields.
    """
    out: set[str] = set()
    for name in _SECRET_ENV_VARS:
        v = (os.environ.get(name) or "").strip()
        if len(v) < 16:  # ignore empty / trivially short (paper mode)
            continue
        out.add(v)
        out.add(v[2:] if v.startswith("0x") else "0x" + v)  # 0x-stripped / -added
    return out


def redact_secrets(value: Any) -> Any:
    """Replace any known secret value found inside a string with a marker.

    Precise (exact-value match) so it never touches legitimate hashes/ids.
    A no-op when no secret is configured (e.g. paper mode).
    """
    if not isinstance(value, str):
        return value
    for secret in _secret_values():
        if secret in value:
            value = value.replace(secret, _REDACTION)
    return value


def _redact_processor(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: scrub secrets from every rendered string field.

    Runs AFTER exception/stack rendering so a secret embedded in a traceback
    or exception message is scrubbed before it reaches stdout.
    """
    for key, val in event_dict.items():
        event_dict[key] = redact_secrets(val)
    return event_dict


def setup_logging(level: str = "INFO") -> None:
    """Configure structlog to emit JSON to stdout. Call once at startup."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,  # scrub secrets AFTER exc/stack are rendered
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str) -> structlog.BoundLogger:
    """Get a bound logger tagged with module name."""
    return structlog.get_logger().bind(module=module)
