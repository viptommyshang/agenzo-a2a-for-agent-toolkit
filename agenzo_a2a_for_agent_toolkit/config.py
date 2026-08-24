"""Runtime configuration, read from environment variables (with a local ``.env`` fallback).

All settings can be provided either via a project-local ``.env`` file or via the ``env`` block
of the MCP server entry in ``.kiro/settings/mcp.json`` (the latter takes precedence, since Kiro
injects them into the process environment before the server starts).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Load ``.env`` from the project root if present. Real process env always wins (override=False).
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


BASE_URL = _env("AGENZO_A2A_BASE_URL", "http://localhost:8000").rstrip("/")
AGENT_ID = _env("AGENZO_A2A_AGENT_ID", "base-orchestrator")
MEMBER_ID = _env("AGENZO_A2A_MEMBER_ID", "prod-user-001")

API_KEY = _env("AGENZO_A2A_API_KEY", "")
API_KEY_FILE = _env("AGENZO_A2A_API_KEY_FILE", str(PROJECT_ROOT / "api_key.local"))
INVITATION_CODE = _env("AGENZO_A2A_INVITATION_CODE", "")
AGENT_NAME = _env("AGENZO_A2A_AGENT_NAME", "kiro-agent")

STREAM = _env("AGENZO_A2A_STREAM", "1").strip().lower() not in ("0", "false", "no")
HTTP_TIMEOUT = float(_env("AGENZO_A2A_HTTP_TIMEOUT", "180") or "180")
