"""Runtime configuration, read from environment variables (with a local ``.env`` fallback).

All settings can be provided either via a project-local ``.env`` file or via the ``env`` block
of the MCP server entry in ``.kiro/settings/mcp.json`` (the latter takes precedence, since Kiro
injects them into the process environment before the server starts).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Load ``.env`` from the project root if present. Real process env always wins (override=False).
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


BASE_URL = _env("AGENZO_A2A_BASE_URL", "http://localhost:8000").rstrip("/")
AGENT_ID = _env("AGENZO_A2A_AGENT_ID", "base-orchestrator")
MEMBER_ID = _env("AGENZO_A2A_MEMBER_ID", "prod-user-001")

API_KEY = _env("AGENZO_A2A_API_KEY", "")
API_KEY_FILE = _env("AGENZO_A2A_API_KEY_FILE", str(PROJECT_ROOT / "api_key.local"))
INVITATION_CODE = _env("AGENZO_A2A_INVITATION_CODE", "")
AGENT_NAME = _env("AGENZO_A2A_AGENT_NAME", "kiro-agent")

STREAM = _env("AGENZO_A2A_STREAM", "1").strip().lower() not in ("0", "false", "no")
HTTP_TIMEOUT = float(_env("AGENZO_A2A_HTTP_TIMEOUT", "180") or "180")

# ── Debug / raw A2A protocol visibility ───────────────────────────────────────
# This MCP server is primarily a *debugging* bridge. Integrators who talk to the A2A
# orchestrator directly need to see the exact JSON-RPC request/response — which the normal
# tool output hides (it returns a normalized {state, text, cards[]} view). These settings turn
# that raw visibility on.
#
# DEBUG: when true, each A2A exchange (request body + response) is LOGGED at DEBUG level (see
#   setup_logging → stderr, plus LOG_FILE when set). It is deliberately NOT inlined into tool
#   results — that would bloat the chat context. Use the ``inspect`` tool to view raw traffic on
#   demand (works regardless of this flag).
DEBUG = _bool("AGENZO_A2A_DEBUG", False)
# LOG_FILE: optional path; when set, logs are also written there (in addition to stderr). Never
#   write logs to stdout — in stdio transport stdout is the MCP protocol channel.
LOG_FILE = _env("AGENZO_A2A_LOG_FILE", "")
# DEBUG_BUFFER: how many recent raw exchanges the bridge keeps in memory for ``inspect()``.
DEBUG_BUFFER = _int("AGENZO_A2A_DEBUG_BUFFER", 50)
# DEBUG_MAXLEN: cap (chars) applied to raw request/response bodies when they are surfaced, so a
#   large SSE stream can't blow up the MCP payload. 0 disables the cap.
DEBUG_MAXLEN = _int("AGENZO_A2A_DEBUG_MAXLEN", 20000)

# Logger shared by the toolkit (the bridge emits request/response traces on it).
LOGGER_NAME = "agenzo_a2a"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging() -> logging.Logger:
    """Configure (idempotently) the toolkit logger and return it.

    - Level is DEBUG when :data:`DEBUG` is on, else WARNING (quiet by default).
    - Always attaches a ``stderr`` handler (safe for stdio transport, where stdout carries the
      MCP protocol). Adds a file handler when :data:`LOG_FILE` is set.
    - Re-callable: existing handlers are cleared first so ``configure(debug=…)`` can re-apply the
      level/handlers at runtime. Propagation is disabled to avoid the root logger duplicating
      output (and possibly writing to stdout).
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 — closing a handler must never break startup
            pass

    logger.setLevel(logging.DEBUG if DEBUG else logging.WARNING)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    if LOG_FILE:
        try:
            file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            # A bad log path must not crash the server — stderr logging still works.
            logger.warning("could not open AGENZO_A2A_LOG_FILE=%s; logging to stderr only", LOG_FILE)

    return logger
