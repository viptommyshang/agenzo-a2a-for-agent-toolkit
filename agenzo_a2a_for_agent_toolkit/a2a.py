"""Self-contained A2A transport for the Agenzo orchestrator.

Deliberately depends only on ``httpx`` (no ``a2a-sdk``): the JSON-RPC envelope and the card
extraction are simple and stable enough to build/parse directly. This mirrors the reference
implementation in ``agenzo-agent-orchestrator-base/scripts/prod/a2a_common.py`` while dropping
all interactive prompts (the chat agent drives instead).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

logger = logging.getLogger("agenzo_a2a")


class AuthError(RuntimeError):
    """Onboarding (register / token exchange) failed, with a human-readable reason."""


def _short(text: str | None, limit: int = 400) -> str:
    return (text or "").strip().replace("\n", " ")[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Request construction (JSON-RPC 2.0 over the A2A message endpoints)
# ─────────────────────────────────────────────────────────────────────────────
def _rpc(parts: list[dict[str, Any]], context_id: str | None, streaming: bool) -> dict[str, Any]:
    message: dict[str, Any] = {"messageId": uuid4().hex, "role": "user", "parts": parts}
    if context_id:
        message["contextId"] = context_id  # one context per conversation → server session_id
    return {
        "jsonrpc": "2.0",
        "id": uuid4().hex,
        "method": "message/stream" if streaming else "message/send",
        "params": {"message": message},
    }


def text_body(text: str, context_id: str | None, streaming: bool = False) -> dict[str, Any]:
    """Natural-language turn (a single TextPart)."""
    return _rpc([{"kind": "text", "text": text}], context_id, streaming)


def data_body(data: dict[str, Any], context_id: str | None, streaming: bool = False) -> dict[str, Any]:
    """Structured card action (a single DataPart), e.g. ``{component, action, payload}``."""
    return _rpc([{"kind": "data", "data": data}], context_id, streaming)


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing (blocking single-JSON or streamed SSE both supported)
# ─────────────────────────────────────────────────────────────────────────────
def _part_root(p: Any) -> dict[str, Any]:
    if not isinstance(p, dict):
        return {}
    return p.get("root") if isinstance(p.get("root"), dict) else p


def cards_and_texts(task: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect all DataPart envelopes + all TextPart strings from a Task (history + status.message)."""
    cards: list[dict[str, Any]] = []
    texts: list[str] = []
    msgs = list(task.get("history") or [])
    status = task.get("status")
    if isinstance(status, dict) and isinstance(status.get("message"), dict):
        msgs.append(status["message"])
    for m in msgs:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts") or []:
            root = _part_root(p)
            if root.get("kind") == "text" and isinstance(root.get("text"), str):
                texts.append(root["text"])
            elif root.get("kind") == "data" and isinstance(root.get("data"), dict):
                cards.append(root["data"])
    return cards, texts


def _iter_sse_data(raw: str) -> Iterator[str]:
    """Yield each SSE block's joined ``data:`` payload (a JSON string) from a raw event stream."""
    for block in raw.split("\n\n"):
        data_str = "".join(
            line[len("data:"):].strip() for line in block.splitlines() if line.startswith("data:")
        )
        if data_str:
            yield data_str


def sse_frames(raw: str) -> list[dict[str, Any]]:
    """Parse a raw SSE stream into the list of JSON-RPC frame objects it carried (best-effort).

    Each frame is one ``event:/data:`` block's parsed ``data`` object (e.g. a
    ``SendStreamingMessageSuccessResponse`` whose ``result`` is a ``TaskStatusUpdateEvent`` /
    ``Message`` / ``Task``). Non-JSON blocks are skipped. Used both to find the final Task and to
    surface the raw frames for protocol inspection."""
    frames: list[dict[str, Any]] = []
    for data_str in _iter_sse_data(raw):
        try:
            obj = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            frames.append(obj)
    return frames


def final_task(raw: str | None) -> dict[str, Any] | None:
    """Extract the final Task ``result`` from a response (single JSON, or SSE with many frames)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):  # blocking: whole body is one JSON-RPC response
        try:
            obj = json.loads(raw)
            res = obj.get("result") if isinstance(obj, dict) else None
            return res if isinstance(res, dict) else None
        except json.JSONDecodeError:
            return None
    # streaming: last `data:` frame whose result kind == "task" (else last result)
    last_task: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    for obj in sse_frames(raw):
        res = obj.get("result") if isinstance(obj, dict) else None
        if isinstance(res, dict):
            last_result = res
            if res.get("kind") == "task":
                last_task = res
    return last_task or last_result


def task_state(task: dict[str, Any]) -> str:
    st = task.get("status") if isinstance(task, dict) else None
    return str(st.get("state") or "") if isinstance(st, dict) else ""


def normalize_task(task: dict[str, Any] | None) -> dict[str, Any]:
    """Turn a Task into a compact, chat-friendly shape: ``{state, text, cards[], primary_component}``."""
    if not isinstance(task, dict):
        return {"state": "", "text": "", "cards": [], "primary_component": None}
    cards_raw, texts = cards_and_texts(task)
    cards: list[dict[str, Any]] = []
    for env in cards_raw:
        if not isinstance(env, dict):
            continue
        actions = [
            {"id": a.get("id"), "dispatch": a.get("dispatch")}
            for a in (env.get("actions") or [])
            if isinstance(a, dict)
        ]
        cards.append(
            {
                "component": env.get("component"),
                "kind": env.get("kind"),
                "data": env.get("data"),
                "actions": actions,
                "item_actions": env.get("item_actions"),
            }
        )
    return {
        "state": task_state(task),
        "text": "\n".join(t for t in texts if t),
        "cards": cards,
        "primary_component": cards[-1]["component"] if cards else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Raw protocol inspection: capture the exact A2A request/response for debugging
# ─────────────────────────────────────────────────────────────────────────────
def _context_of(body: dict[str, Any]) -> str | None:
    """Best-effort extract of ``params.message.contextId`` from a JSON-RPC A2A body."""
    try:
        return body["params"]["message"].get("contextId")
    except (KeyError, TypeError, AttributeError):
        return None


def _cap(text: str, maxlen: int) -> str:
    """Truncate ``text`` to ``maxlen`` chars (0 = no cap), appending a truncation note."""
    if maxlen and len(text) > maxlen:
        return text[:maxlen] + f"\n…[truncated {len(text) - maxlen} chars]"
    return text


def _curl_repro(record: dict[str, Any]) -> str:
    """Build a copy-pasteable ``curl`` that reproduces the captured request.

    The Bearer token is shown as ``$AGENZO_A2A_TOKEN`` (never the real value) so the line is safe
    to share; set that env var (or paste your own token) to replay it."""
    parts = [f"curl -X {record.get('method', 'POST')} '{record.get('url', '')}'"]
    parts.append("  -H 'Content-Type: application/json'")
    parts.append("  -H 'Authorization: Bearer '\"$AGENZO_A2A_TOKEN\"")
    if record.get("transport") == "stream":
        parts.append("  -H 'Accept: text/event-stream'")
    body = json.dumps(record.get("request") or {}, ensure_ascii=False)
    parts.append(f"  -d '{body}'")
    return " \\\n".join(parts)


def exchange_view(record: dict[str, Any], *, maxlen: int = 0) -> dict[str, Any]:
    """Render a captured exchange into a faithful, inspectable dict.

    Surfaces the exact request body plus the response as both raw text (``response_raw``) and,
    best-effort, structured form (``response_json`` for a blocking JSON-RPC reply, or
    ``response_frames`` for a streamed SSE reply). Includes a ``curl`` reproduction."""
    raw = record.get("response") or ""
    view: dict[str, Any] = {
        "seq": record.get("seq"),
        "ts": record.get("ts"),
        "kind": record.get("kind"),
        "transport": record.get("transport"),
        "method": record.get("method"),
        "url": record.get("url"),
        "context_id": record.get("context_id"),
        "status": record.get("status"),
        "request": record.get("request"),
        "response_raw": _cap(raw, maxlen),
    }
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            view["response_json"] = json.loads(stripped)
        except json.JSONDecodeError:
            view["response_json"] = None
    elif stripped:
        frames = sse_frames(raw)
        if frames:
            view["response_frames"] = frames
    view["curl"] = _curl_repro(record)
    return view


# ─────────────────────────────────────────────────────────────────────────────
# Bridge: holds the async client + a cached short-lived token, drives A2A turns
# ─────────────────────────────────────────────────────────────────────────────
class A2ABridge:
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(timeout=cfg.HTTP_TIMEOUT)
        self._token: str | None = None
        self._member_id: str = cfg.MEMBER_ID
        self._lock = asyncio.Lock()
        # Raw-exchange capture (for inspect() / debug output). Bounded ring buffer; each turn's
        # exact request body + response text is recorded here so integrators can see the real A2A
        # protocol that the normalized tool output hides.
        self.debug: bool = bool(getattr(cfg, "DEBUG", False))
        self._maxlen: int = int(getattr(cfg, "DEBUG_MAXLEN", 20000))
        buffer = max(1, int(getattr(cfg, "DEBUG_BUFFER", 50)))
        self._exchanges: deque[dict[str, Any]] = deque(maxlen=buffer)
        self._seq: int = 0

    # ── raw-exchange capture / retrieval ──────────────────────────────────────
    def _record(
        self,
        *,
        kind: str,
        transport: str,
        method: str,
        url: str,
        request_body: dict[str, Any],
        status: int,
        response_text: str,
    ) -> dict[str, Any]:
        """Append one exchange to the ring buffer and emit a DEBUG log line. Returns the record."""
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": kind,
            "transport": transport,
            "method": method,
            "url": url,
            "context_id": _context_of(request_body),
            "request": request_body,
            "status": status,
            # Cap stored response to bound memory; exposure re-caps as needed.
            "response": _cap(response_text, self._maxlen),
        }
        self._exchanges.append(record)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "A2A %s %s -> HTTP %s\n  request:  %s\n  response: %s",
                transport,
                url,
                status,
                json.dumps(request_body, ensure_ascii=False),
                record["response"],
            )
        return record

    def recent_exchanges(
        self, limit: int | None = None, context_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return captured exchanges (oldest→newest), optionally filtered by ``context_id`` and
        capped to the most recent ``limit`` entries."""
        items = list(self._exchanges)
        if context_id:
            items = [e for e in items if e.get("context_id") == context_id]
        if limit is not None and limit >= 0:
            items = items[-limit:]
        return items

    def last_exchange(self, context_id: str | None = None) -> dict[str, Any] | None:
        """Return the most recent captured exchange (optionally for a given ``context_id``)."""
        items = self.recent_exchanges(context_id=context_id)
        return items[-1] if items else None

    @property
    def buffer_capacity(self) -> int | None:
        """Max number of raw exchanges retained in the ring buffer."""
        return self._exchanges.maxlen

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def member_id(self) -> str:
        return self._member_id

    async def set_member(self, member_id: str) -> None:
        """Switch the acting member (JWT sub). Forces a token re-exchange on next call."""
        member_id = (member_id or "").strip()
        if member_id and member_id != self._member_id:
            self._member_id = member_id
            self._token = None

    # ── discovery ────────────────────────────────────────────────────────────
    async def agent_card(self) -> dict[str, Any]:
        url = f"{self.cfg.BASE_URL}/a2a/agents/{self.cfg.AGENT_ID}/.well-known/agent-card.json"
        resp = await self._client.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── auth: api_key (env / file / register) → short-lived token ─────────────
    def _read_key_file(self) -> str | None:
        try:
            p = Path(self.cfg.API_KEY_FILE)
            if not p.is_file():
                return None
            for line in p.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    return s
        except OSError:
            return None
        return None

    def _save_key_file(self, api_key: str) -> None:
        try:
            p = Path(self.cfg.API_KEY_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "# agenzo-a2a-for-agent-toolkit developer api_key (auto-saved on first register).\n"
                "# gitignored (*.local). Do not commit.\n" + api_key + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    async def _register(self) -> str:
        url = f"{self.cfg.BASE_URL}/api/v1/auth/register"
        payload: dict[str, str] = {"name": self.cfg.AGENT_NAME}
        if self.cfg.INVITATION_CODE:
            payload["invitation_code"] = self.cfg.INVITATION_CODE
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise AuthError(f"cannot reach orchestrator at {url}: {exc}") from exc
        if resp.status_code == 200:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            api_key = body.get("api_key") if isinstance(body, dict) else None
            if api_key:
                self._save_key_file(api_key)
                return api_key
            raise AuthError(f"register returned 200 but no api_key: {_short(resp.text)}")
        raise AuthError(
            f"register failed (HTTP {resp.status_code}): {_short(resp.text)}. "
            "Set AGENZO_A2A_API_KEY (or AGENZO_A2A_API_KEY_FILE) to reuse a key, "
            "or AGENZO_A2A_INVITATION_CODE to self-register."
        )

    async def _exchange_token(self, api_key: str) -> str:
        url = f"{self.cfg.BASE_URL}/api/v1/auth/token"
        try:
            resp = await self._client.post(url, json={"api_key": api_key, "member_id": self._member_id})
        except httpx.HTTPError as exc:
            raise AuthError(f"cannot reach orchestrator at {url}: {exc}") from exc
        if resp.status_code == 200:
            token = resp.json().get("access_token")
            if token:
                return token
            raise AuthError(f"token exchange returned 200 but no access_token: {_short(resp.text)}")
        raise AuthError(f"token exchange failed (HTTP {resp.status_code}): {_short(resp.text)}")

    async def ensure_token(self, *, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        async with self._lock:
            if self._token and not force:
                return self._token
            api_key = (self.cfg.API_KEY or "").strip() or self._read_key_file()
            if not api_key:
                api_key = await self._register()
            self._token = await self._exchange_token(api_key)
            return self._token

    # ── driving turns (with a single 401 re-auth retry) ───────────────────────
    async def _raw_post(self, body: dict[str, Any], token: str) -> tuple[int, str]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        base, agent = self.cfg.BASE_URL, self.cfg.AGENT_ID
        if self.cfg.STREAM:
            url = f"{base}/a2a/agents/{agent}/v1/message:stream"
            buf = ""
            async with self._client.stream(
                "POST", url, json=body, headers={**headers, "Accept": "text/event-stream"}
            ) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode(errors="replace")
                    self._record(
                        kind="a2a", transport="stream", method="POST", url=url,
                        request_body=body, status=resp.status_code, response_text=err,
                    )
                    return resp.status_code, err
                async for chunk in resp.aiter_text():
                    buf += chunk
            self._record(
                kind="a2a", transport="stream", method="POST", url=url,
                request_body=body, status=200, response_text=buf,
            )
            return 200, buf
        url = f"{base}/a2a/agents/{agent}/v1/message:send"
        resp = await self._client.post(url, json=body, headers=headers)
        self._record(
            kind="a2a", transport="send", method="POST", url=url,
            request_body=body, status=resp.status_code, response_text=resp.text,
        )
        return resp.status_code, resp.text

    async def _drive(self, body: dict[str, Any]) -> tuple[int, str]:
        token = await self.ensure_token()
        status, text = await self._raw_post(body, token)
        if status == 401:  # token likely expired → re-exchange once and retry
            token = await self.ensure_token(force=True)
            status, text = await self._raw_post(body, token)
        return status, text

    # ── calling the orchestrator's authed utility endpoints (non-A2A, e.g. /tools/*) ──
    async def _raw_tool_post(
        self, path: str, payload: dict[str, Any], token: str
    ) -> tuple[int, str]:
        url = f"{self.cfg.BASE_URL}{path}"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        resp = await self._client.post(url, json=payload, headers=headers)
        self._record(
            kind="tool", transport="http", method="POST", url=url,
            request_body=payload, status=resp.status_code, response_text=resp.text,
        )
        return resp.status_code, resp.text

    async def call_tool(self, path: str, payload: dict[str, Any]) -> tuple[int, str]:
        """POST a JSON payload to an authed orchestrator utility endpoint (e.g. ``/tools/resolve-location``).

        Mirrors :meth:`_drive`'s single 401 re-auth retry, but targets a plain HTTP endpoint (not the
        A2A message transport). Used by the geocoding / pickup-time tools so the chat agent can resolve
        place names + times to real coordinates/epoch before submitting a ride."""
        token = await self.ensure_token()
        status, text = await self._raw_tool_post(path, payload, token)
        if status == 401:  # token likely expired → re-exchange once and retry
            token = await self.ensure_token(force=True)
            status, text = await self._raw_tool_post(path, payload, token)
        return status, text

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        return await self._drive(text_body(text, context_id, self.cfg.STREAM))

    async def send_action(
        self, context_id: str, component: str, action: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        data = {"component": component, "action": action, "payload": payload or {}}
        return await self._drive(data_body(data, context_id, self.cfg.STREAM))
