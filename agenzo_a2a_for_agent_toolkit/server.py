"""FastMCP server: lets AI agents drive the Agenzo A2A orchestrator to book hotels/flights.

Design
------
The orchestrator speaks a *domain-agnostic card protocol*: every step returns one or more cards
(``{component, kind, data, actions}``) and you advance by sending a structured action referencing
one of ``actions[].id`` with a payload built from the previous card's ``data``. These tools are a
thin, **stateful** bridge over that protocol — the chat agent is the client that reads cards, asks
the user what it needs, and calls the next tool. New domains/schemas work with zero changes here.

Tools
-----
- ``discover()``            – agent card (which domains/skills are bookable).
- ``configure(...)``        – override orchestrator connection settings at runtime.
- ``guide()``              – domain-agnostic driving law + LIVE domains/scenarios (from the card).
- ``book(request)``        – start a booking conversation from natural language; returns a session.
- ``send_message(sid,txt)``– natural-language turn (answer a server follow-up).
- ``act(sid,comp,action,payload)`` – structured card action (the main driver).
- ``poll(sid,comp)``       – re-check an ``*-await`` card (``action:"poll"``).
- ``start_payment(...)``   – separate payment session (pick/verify a card before confirming).
- ``open_url(url)``        – open a checkout/enrollment page in the local browser.
- ``resolve_location(addr)``            – geocode a place name → {lat,lng,timezone} (ride: before submit).
- ``resolve_pickup_time(dt,tz)``        – local datetime → UTC epoch (ride: scheduled pickupTime).
- ``inspect(sid,limit)``   – dump the exact A2A JSON-RPC request/response exchanges (debugging).

Protocol visibility
-------------------
The normal tools return a normalized ``{state, text, cards[]}`` view of each A2A Task. Because this
bridge is primarily a *debugging* tool, the exact A2A protocol is also exposed: set
``AGENZO_A2A_DEBUG=1`` (or ``configure(debug="1")``) to attach ``raw_request`` / ``raw_response`` to
every result and log each exchange, and/or call ``inspect()`` any time to dump the captured raw
JSON-RPC traffic (request body, response frames, and a ``curl`` reproduction).
"""

from __future__ import annotations

import json
import webbrowser
from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from . import config as cfg
from .a2a import A2ABridge, AuthError, exchange_view, final_task, normalize_task, _short

mcp = FastMCP("agenzo-travel")

_bridge: A2ABridge | None = None
# session_id → {"context_id", "kind"}. context_id == session_id (one A2A context per session).
_sessions: dict[str, dict[str, str]] = {}

# Runtime config overrides (set by configure() tool or env vars)
_runtime_cfg: dict[str, Any] = {}


def _get_bridge() -> A2ABridge:
    global _bridge
    if _bridge is None:
        # Apply runtime overrides to config module
        if _runtime_cfg:
            for key, val in _runtime_cfg.items():
                if hasattr(cfg, key) and val not in (None, ""):
                    setattr(cfg, key, val)
        # (Re)configure logging with the effective settings before the bridge starts tracing.
        cfg.setup_logging()
        _bridge = A2ABridge(cfg)
    return _bridge


def _reset_bridge() -> None:
    """Force re-creation of bridge with updated config."""
    global _bridge
    if _bridge is not None:
        import asyncio
        try:
            asyncio.get_event_loop().create_task(_bridge.aclose())
        except Exception:
            pass
    _bridge = None


def _new_session(kind: str) -> str:
    sid = uuid4().hex
    _sessions[sid] = {"context_id": sid, "kind": kind}
    return sid


def _ctx(session_id: str) -> str:
    s = _sessions.get(session_id)
    return s["context_id"] if s else session_id


def _attach_debug(out: dict[str, Any], context_id: str | None = None) -> dict[str, Any]:
    """When debug is on, attach the exact A2A request/response for the last exchange.

    Adds ``raw_request`` (the JSON-RPC body sent), ``raw_response`` (structured when possible, else
    the raw text) and ``raw_exchange`` (full detail: url, status, curl repro). This is what a direct
    A2A integrator needs to see, which the normalized output otherwise hides. No-op when debug is
    off — use the ``inspect`` tool for on-demand access regardless of the flag."""
    bridge = _get_bridge()
    if not bridge.debug:
        return out
    record = bridge.last_exchange(context_id)
    if record is None:
        return out
    view = exchange_view(record, maxlen=cfg.DEBUG_MAXLEN)
    out["raw_request"] = view.get("request")
    out["raw_response"] = view.get("response_json") or view.get("response_frames") or view.get("response_raw")
    out["raw_exchange"] = view
    return out


def _result(session_id: str, status: int, raw: str) -> dict[str, Any]:
    """Normalize an A2A response into a chat-friendly dict (or an error)."""
    ctx = _ctx(session_id)
    if status != 200:
        return _attach_debug(
            {
                "session_id": session_id,
                "error": f"orchestrator returned HTTP {status}",
                "detail": _short(raw),
                "cards": [],
                "text": "",
            },
            ctx,
        )
    task = final_task(raw)
    if task is None:
        return _attach_debug(
            {
                "session_id": session_id,
                "error": "no A2A Task in response",
                "detail": _short(raw),
                "cards": [],
                "text": "",
            },
            ctx,
        )
    out = normalize_task(task)
    out["session_id"] = session_id
    out["member_id"] = _get_bridge().member_id
    return _attach_debug(out, ctx)


def _tool_result(status: int, raw: str) -> dict[str, Any]:
    """Parse a plain JSON response from an orchestrator utility endpoint (e.g. ``/tools/*``).

    On non-200 or non-JSON, surface a compact error dict instead of raising — the chat agent then
    knows geocoding is unavailable and can ask the user for coordinates."""
    if status != 200:
        return _attach_debug({"error": f"orchestrator returned HTTP {status}", "detail": _short(raw)})
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return _attach_debug({"error": "invalid JSON from orchestrator", "detail": _short(raw)})
    out = obj if isinstance(obj, dict) else {"error": "unexpected response", "detail": _short(raw)}
    return _attach_debug(out)


# ─────────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool()
async def discover() -> dict[str, Any]:
    """Return the orchestrator agent card: name, description (bookable domains + required client
    capabilities), input/output modes, and the discovery skill catalogue. Call this first to learn
    what can be booked (e.g. hotel, flight)."""
    bridge = _get_bridge()
    try:
        card = await bridge.agent_card()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "hint": f"Is the orchestrator running at {cfg.BASE_URL}?"}
    return {
        "name": card.get("name"),
        "description": card.get("description"),
        "defaultInputModes": card.get("defaultInputModes"),
        "defaultOutputModes": card.get("defaultOutputModes"),
        "skills": [
            {"id": s.get("id"), "name": s.get("name"), "tags": s.get("tags"), "examples": s.get("examples")}
            for s in (card.get("skills") or [])
            if isinstance(s, dict)
        ],
    }


@mcp.tool()
async def configure(
    base_url: str = "",
    agent_id: str = "",
    member_id: str = "",
    api_key: str = "",
    invitation_code: str = "",
    agent_name: str = "",
    stream: str = "",
    http_timeout: str = "",
    debug: str = "",
    log_file: str = "",
) -> dict[str, Any]:
    """Configure the MCP server at runtime. Call this BEFORE booking if you need to override
    the default orchestrator connection settings. Only non-empty values are applied.

    Parameters:
      - base_url: orchestrator address (e.g. "https://agent-dev.agenzo.com")
      - agent_id: agent id (default "base-orchestrator")
      - member_id: end user placing the order (isolated per developer+member)
      - api_key: developer API key for authentication
      - invitation_code: self-register invitation code (used if no api_key)
      - agent_name: display name for registration (default "kiro-agent")
      - stream: "1" for streaming, "0" for blocking
      - http_timeout: timeout in seconds (default "180")
      - debug: "1" to attach the exact A2A ``raw_request``/``raw_response`` to every tool result
        (and log each exchange), "0" to turn it off. The ``inspect`` tool works regardless.
      - log_file: optional path to also write request/response traces to (in addition to stderr).

    Returns the active configuration (secrets masked)."""
    mapping = {
        "BASE_URL": base_url.rstrip("/") if base_url else "",
        "AGENT_ID": agent_id,
        "MEMBER_ID": member_id,
        "API_KEY": api_key,
        "INVITATION_CODE": invitation_code,
        "AGENT_NAME": agent_name,
    }

    changed = False
    for key, val in mapping.items():
        if val:
            _runtime_cfg[key] = val
            changed = True

    if stream:
        _runtime_cfg["STREAM"] = stream.strip().lower() not in ("0", "false", "no")
        changed = True
    if http_timeout:
        _runtime_cfg["HTTP_TIMEOUT"] = float(http_timeout)
        changed = True
    if debug:
        _runtime_cfg["DEBUG"] = debug.strip().lower() not in ("0", "false", "no", "off")
        changed = True
    if log_file:
        _runtime_cfg["LOG_FILE"] = log_file
        changed = True

    if changed:
        _reset_bridge()
        # Recreate the bridge now so the new settings take effect (and logging is re-applied).
        _get_bridge()

    # Return active config (mask secrets)
    bridge_cfg = {
        "BASE_URL": getattr(cfg, "BASE_URL", ""),
        "AGENT_ID": getattr(cfg, "AGENT_ID", ""),
        "MEMBER_ID": _runtime_cfg.get("MEMBER_ID", getattr(cfg, "MEMBER_ID", "")),
        "AGENT_NAME": _runtime_cfg.get("AGENT_NAME", getattr(cfg, "AGENT_NAME", "")),
        "STREAM": _runtime_cfg.get("STREAM", getattr(cfg, "STREAM", True)),
        "HTTP_TIMEOUT": _runtime_cfg.get("HTTP_TIMEOUT", getattr(cfg, "HTTP_TIMEOUT", 180)),
        "DEBUG": _runtime_cfg.get("DEBUG", getattr(cfg, "DEBUG", False)),
        "LOG_FILE": _runtime_cfg.get("LOG_FILE", getattr(cfg, "LOG_FILE", "")) or "(stderr only)",
        "API_KEY": "***" if (_runtime_cfg.get("API_KEY") or getattr(cfg, "API_KEY", "")) else "(not set)",
        "INVITATION_CODE": "***" if (_runtime_cfg.get("INVITATION_CODE") or getattr(cfg, "INVITATION_CODE", "")) else "(not set)",
    }
    # Apply overrides to show actual values
    for key in ("BASE_URL", "AGENT_ID", "MEMBER_ID", "AGENT_NAME"):
        if key in _runtime_cfg:
            bridge_cfg[key] = _runtime_cfg[key]

    return {"status": "configured" if changed else "unchanged", "config": bridge_cfg}


@mcp.tool()
async def book(request: str, member_id: str = "") -> dict[str, Any]:
    """Start a NEW booking conversation from a natural-language request and return the first
    card(s).

    Examples of ``request``:
      - "Book a one-way economy flight from Shanghai to Beijing on August 24th for 1 adult."
      - "Book a hotel near the Bund in Shanghai, 1 adult, check-in Aug 18, check-out Aug 19."

    Returns ``{session_id, cards, text, state, primary_component, member_id}``. Read the last card's
    ``component``/``data``/``actions`` and advance with ``act(session_id, ...)``; use
    ``send_message(session_id, ...)`` to answer a natural-language follow-up. ``member_id`` (optional)
    overrides the acting end user for this and subsequent calls (booking + payment must share it)."""
    bridge = _get_bridge()
    if member_id:
        await bridge.set_member(member_id)
    sid = _new_session("booking")
    try:
        status, raw = await bridge.send_text(_ctx(sid), request)
    except AuthError as exc:
        return {"session_id": sid, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    return _result(sid, status, raw)


@mcp.tool()
async def send_message(session_id: str, text: str) -> dict[str, Any]:
    """Continue an existing session with a natural-language message — use this to answer the
    server's follow-up questions (e.g. missing dates/passengers) or to change search criteria."""
    bridge = _get_bridge()
    try:
        status, raw = await bridge.send_text(_ctx(session_id), text)
    except AuthError as exc:
        return {"session_id": session_id, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    return _result(session_id, status, raw)


@mcp.tool()
async def act(session_id: str, component: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a STRUCTURED card action to advance the flow (the main driver).

    - ``component``: the current card's ``component`` (copy it verbatim from the card; e.g.
      "flight.offer-list", "hotel.search" — names come from the card, not from this doc).
    - ``action``: one of that card's ``actions[].id`` / ``item_actions[].id`` (e.g. "submit",
      "select", "confirm", "poll").
    - ``payload``: read it from the chosen action, don't guess. If the action declares
      ``carries: [f1, f2, …]``, send exactly those fields copied from the card's ``data`` (or the
      chosen list row's data). For a form ``submit``/``confirm``, send the fields present in the
      card's ``data`` plus anything the user supplied. An action carrying a ``scenario`` navigates
      to that scenario; an action with ``capability:"open_url"`` needs ``open_url`` on the URL at
      its ``data_ref`` then its ``callback`` id.

    To attach a payment result to a booking confirm, include ``payment_token_id`` (UnionPay) or
    ``payment_method_id`` (EVO/Visa/Mastercard) in the confirm ``payload`` (see ``start_payment``)."""
    bridge = _get_bridge()
    try:
        status, raw = await bridge.send_action(_ctx(session_id), component, action, payload or {})
    except AuthError as exc:
        return {"session_id": session_id, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    return _result(session_id, status, raw)


@mcp.tool()
async def poll(session_id: str, component: str) -> dict[str, Any]:
    """Convenience for ``*-await`` cards: send ``{component, action:"poll"}`` to re-check status
    (e.g. component="payment.await" while waiting for a UnionPay passkey to reach ACTIVE)."""
    bridge = _get_bridge()
    try:
        status, raw = await bridge.send_action(_ctx(session_id), component, "poll", {})
    except AuthError as exc:
        return {"session_id": session_id, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    return _result(session_id, status, raw)


@mcp.tool()
async def start_payment(
    amount_cents: int, recipient_name: str, recipient_account: str = "", member_id: str = ""
) -> dict[str, Any]:
    """Start a SEPARATE payment session (independent context) to pick/verify a payment method BEFORE
    confirming an order. Returns the method-picker card listing the member's payment methods (each
    row carries an ``id`` and a ``payment_brand``).

    Then drive it card-first with ``act(payment_session_id, ...)`` — read each card's ``actions`` /
    ``item_actions`` and follow the generic loop (see ``guide()``); do not assume component names:
      - Pick a listed method with its row action (it ``carries`` the ``id`` + ``payment_brand``).
        A method usable without extra steps yields an id you attach to the booking confirm (the
        confirm action's ``carries`` names the field, e.g. ``payment_method_id``).
      - If selecting a method returns a card with an ``open_url`` action (passkey / enrollment /
        Drop-in), call ``open_url`` on the URL at that card's ``data`` field, have the user finish,
        send the action's ``callback`` id, then ``poll(session_id, component)`` until
        ``data.status == "ACTIVE"``. Use the resulting credential in the booking confirm.
      - No method listed / want a new one: use the picker's scenario-jump action (an action carrying
        a ``scenario``, e.g. "add-method") to enter the add-a-method flow, then follow its cards.

    ``amount_cents`` = order total × 100. ``recipient_account`` (phone or email) is REQUIRED for
    UnionPay, may be empty for EVO. ``member_id`` (optional) must match the booking's member."""
    bridge = _get_bridge()
    if member_id:
        await bridge.set_member(member_id)
    sid = _new_session("payment")
    try:
        status, raw = await bridge.send_text(_ctx(sid), "Prepare a UnionPay payment.")
        if status != 200:
            return _result(sid, status, raw)
        status, raw = await bridge.send_action(
            _ctx(sid),
            "payment.setup",
            "submit",
            {
                "amount_cents": int(amount_cents),
                "recipient_name": recipient_name,
                "recipient_account": recipient_account,
            },
        )
    except AuthError as exc:
        return {"session_id": sid, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    return _result(sid, status, raw)


@mcp.tool()
def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the local default browser — used for the out-of-band pages of any card whose
    action has ``dispatch:"client"`` + ``capability:"open_url"``: UnionPay checkout (passkey),
    UnionPay card-enrollment (enroll_url), and the EVO (Visa/Mastercard) Drop-in card-binding page
    (dropin_url, hosted by the orchestrator). The MCP server runs on the user's machine, so this
    pops the page for the user to complete the passkey/enrollment/card entry. After they finish,
    drive the next card (e.g. "paid" then "poll", or "bound" then "poll").

    When deployed remotely (SSE transport), the browser can't be opened on the server, so the URL is
    returned for the client/user to open instead."""
    import os

    if os.environ.get("AGENZO_MCP_TRANSPORT", "stdio").strip().lower() == "sse":
        # Remote mode: cannot open a browser on the server — return the URL for the client to handle.
        return {"ok": True, "opened": url, "note": "Remote mode: please open this URL in your browser."}
    try:
        opened = webbrowser.open(url)
        return {"ok": bool(opened), "opened": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "url": url}


@mcp.tool()
async def resolve_location(address: str) -> dict[str, Any]:
    """Resolve a place name or address to geographic coordinates + timezone (via the orchestrator's
    geocoding helper). Call this BEFORE submitting ride.search and resolve BOTH the pickup AND the
    dropoff (one call each) — the ride backend does NOT geocode, so you must supply real {lat, lng}.
    NEVER guess coordinates.

    ``address`` — a place name or address, e.g. "Shanghai Pudong Airport" or "The Bund, Shanghai".

    Returns ``{lat, lng, formatted_address, timezone}`` on success (use lat/lng at ride.search submit,
    and pass the returned ``timezone`` to ``resolve_pickup_time``); or ``{error: ...}`` when geocoding
    is unconfigured/fails — then ask the user for exact coordinates."""
    bridge = _get_bridge()
    try:
        status, raw = await bridge.call_tool("/tools/resolve-location", {"address": address})
    except AuthError as exc:
        return {"error": f"auth failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "hint": f"Is the orchestrator running at {cfg.BASE_URL}?"}
    return _tool_result(status, raw)


@mcp.tool()
async def resolve_pickup_time(local_datetime: str, timezone: str) -> dict[str, Any]:
    """Convert a local civil date-time to UTC epoch seconds (via the orchestrator's helper). Call this
    to compute a scheduled ride's ``pickupTime`` — NEVER compute epoch yourself. For an immediate ride
    use the literal string "now" instead of calling this.

    ``local_datetime`` — an ABSOLUTE local time in ISO 8601 WITHOUT offset, e.g. "2026-09-20T21:00:00";
    first resolve any relative phrase ("tomorrow 9pm") against today's date. ``timezone`` — the pickup
    location's IANA zone, e.g. "Asia/Shanghai"; take it from ``resolve_location``'s ``timezone`` field.

    Returns ``{epoch, iso_utc, local_datetime, timezone}`` on success (use ``epoch`` as ride.search
    ``pickupTime``); or ``{error: ...}`` for an invalid or past time."""
    bridge = _get_bridge()
    try:
        status, raw = await bridge.call_tool(
            "/tools/resolve-pickup-time",
            {"local_datetime": local_datetime, "timezone": timezone},
        )
    except AuthError as exc:
        return {"error": f"auth failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "hint": f"Is the orchestrator running at {cfg.BASE_URL}?"}
    return _tool_result(status, raw)


# Domain-AGNOSTIC driving law. It never names a specific domain, component, action or field, so it
# does NOT drift when the orchestrator's schemas change (renamed scenarios/components, new domains).
# The concrete "what to send next" always comes from each card the orchestrator returns (component +
# actions[].id + actions[].carries) and from the live scenario catalog appended by guide().
_GUIDE_GENERIC = """\
Agenzo A2A card driver — how to drive ANY booking (domain-agnostic)
===================================================================
This orchestrator speaks a schema-driven CARD protocol. You do NOT need hardcoded per-domain steps:
every response tells you what to do next. New domains/scenarios work with no changes to these tools.

THE LOOP
  1) Start: book("<natural-language request>")  -> {session_id, cards[], text, primary_component}.
     Use discover() (or the LIVE catalog at the bottom of this guide) to see which domains &
     scenarios exist and example phrasings.
  2) Read the LAST card: {component, kind, data, actions[], item_actions[]}.
  3) Pick one of actions[].id and advance:  act(session_id, component, action_id, payload)
  4) Repeat until an order / tracking / detail card appears. Surface to the user the ids and fields
     the card actually exposes (don't invent field names).

BUILDING THE PAYLOAD — read it from the card, do not guess
  • An action may declare `carries: [f1, f2, …]`  ->  payload = EXACTLY those fields, copied from the
    card's `data`. For a list-row action (in `item_actions`), copy them from the chosen row's data.
  • A form card (kind="form") `submit`/`confirm`  ->  payload = the fields present in the card's
    `data` (already-prefilled values) plus anything the user supplied. If a required value is
    missing, ASK the user (or send_message(session_id, "...")) — never fabricate it.
  • An action with `scenario: "<name>"` is a SCENARIO JUMP (e.g. adding a payment method): sending it
    returns that scenario's entry card; then keep following the same loop.
  • An action with `dispatch:"client"` + `capability:"open_url"` needs an out-of-band browser page:
    open the URL at the card's `data[<data_ref>]` via open_url(...), have the user finish, then send
    the action's `callback` id (carrying whatever it lists). Poll any `*-await` card with
    poll(session_id, component) until `data.status == "ACTIVE"` (or another terminal state).

ANSWERING THE SERVER
  • A response with `text` and no cards means the server is asking/telling you something  ->  reply
    with send_message(session_id, text).

PAYMENT — runs in its OWN session
  • Booking + payment MUST share the same member_id.
  • start_payment(amount_cents, recipient_name, recipient_account) opens a SEPARATE payment session
    and returns a card to pick/verify a payment method. Drive it with act(payment_session_id, ...)
    exactly like the loop above — including any `scenario`-jump action to add a new method, and
    open_url for a passkey / enrollment / Drop-in page, then poll the await card to "ACTIVE".
  • When you have a usable credential, attach it to the booking `confirm` payload. The confirm card's
    action `carries` names the field (e.g. payment_token_id or payment_method_id). Omit payment to
    let the platform charge the developer's default.

RIDES — resolve on the client BEFORE submitting a ride search
  • The ride backend does NOT geocode. Resolve BOTH pickup and dropoff with resolve_location(addr)
    -> {lat,lng,timezone}; for a scheduled trip compute pickupTime with
    resolve_pickup_time(local_iso, timezone) -> {epoch} (use the literal "now" for immediate).
    Never guess coordinates or epochs. Put the returned values into the ride search submit payload
    (the exact field names come from that card's `data` / action `carries`).

DEBUGGING
  • inspect(session_id?, limit?) dumps the exact A2A JSON-RPC request/response (the raw protocol).
"""


def _render_scenario_catalog(card: dict[str, Any]) -> str:
    """Render the agent card's skills into a live 'domains -> scenarios (+ example phrasings)' list.

    Fully data-driven: each in-scope scenario the orchestrator advertises is one skill (tags =
    [category, scenario, noun], examples = intent keywords). So new/renamed scenarios show up here
    automatically — nothing to maintain in this file."""
    skills = card.get("skills") or []
    if not isinstance(skills, list) or not skills:
        return "(The orchestrator advertised no skills.)"
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for s in skills:
        if not isinstance(s, dict):
            continue
        tags = s.get("tags") or []
        domain = (tags[0] if tags else None) or "other"
        by_domain.setdefault(str(domain), []).append(s)
    lines: list[str] = []
    for domain, items in by_domain.items():
        lines.append(f"- {domain}:")
        for s in items:
            tags = s.get("tags") or []
            scenario = tags[1] if len(tags) > 1 else (s.get("name") or s.get("id") or "?")
            examples = [str(e) for e in (s.get("examples") or []) if str(e).strip()][:4]
            line = f"    • {scenario} (skill: {s.get('id')})"
            if examples:
                line += " — e.g. " + "; ".join(f'"{e}"' for e in examples)
            lines.append(line)
    return "\n".join(lines)


@mcp.tool()
async def guide() -> str:
    """Return the domain-agnostic driving law PLUS a LIVE catalog of bookable domains/scenarios.

    The step-by-step logic no longer hardcodes per-domain component/action/field names (those come
    from each card the orchestrator returns — read `actions[].id` and `actions[].carries`). The
    catalog is generated from the orchestrator's schema-driven agent card, so it tracks new/renamed
    domains and scenarios automatically. Read this once when you start driving a booking."""
    parts = [_GUIDE_GENERIC]
    bridge = _get_bridge()
    try:
        card = await bridge.agent_card()
    except Exception as exc:  # noqa: BLE001 — guide must work even if the orchestrator is down
        parts.append(
            "Bookable domains & scenarios: could not reach the orchestrator's agent card "
            f"({exc}). Call discover() once it is up to list them."
        )
        return "\n".join(parts)

    parts.append("Bookable domains & scenarios (LIVE from the orchestrator's agent card)")
    parts.append("=" * 70)
    desc = card.get("description")
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip())
        parts.append("")
    parts.append(_render_scenario_catalog(card))
    return "\n".join(parts)


@mcp.tool()
async def inspect(session_id: str = "", limit: int = 10) -> dict[str, Any]:
    """Return the exact A2A protocol exchanges this bridge has made (for debugging / integration).

    The normal tools return a normalized ``{state, text, cards[]}`` view; this MCP server is a
    debugging bridge, so ``inspect`` exposes the REAL JSON-RPC traffic underneath: for each recent
    exchange it returns the exact ``request`` body sent, the response as raw text
    (``response_raw``) plus structured form (``response_json`` for blocking, ``response_frames``
    for a streamed SSE reply), the ``url``/``status``/``transport``/``context_id``, and a ``curl``
    line that reproduces the call (Bearer token shown as ``$AGENZO_A2A_TOKEN``).

    Works regardless of the ``debug`` flag (the buffer is always captured). Parameters:
      - session_id: if given, only exchanges for that session's A2A ``contextId`` are returned.
      - limit: max number of most-recent exchanges to return (default 10)."""
    bridge = _get_bridge()
    ctx = _ctx(session_id) if session_id else None
    records = bridge.recent_exchanges(limit=max(0, int(limit)), context_id=ctx)
    return {
        "count": len(records),
        "debug": bridge.debug,
        "buffer_capacity": bridge.buffer_capacity,
        "session_id": session_id or None,
        "exchanges": [exchange_view(r, maxlen=cfg.DEBUG_MAXLEN) for r in records],
    }


def main() -> None:
    """Entry point: run the MCP server.

    Transport is selected by the ``AGENZO_MCP_TRANSPORT`` env var:
      - ``stdio`` (default): the AI agent launches it locally, communicates via stdin/stdout.
      - ``sse``: remote deployment, listens on HTTP for SSE connections.

    For SSE mode, configure host/port via:
      - ``AGENZO_MCP_HOST`` (default ``0.0.0.0``)
      - ``AGENZO_MCP_PORT`` (default ``8080``)
    """
    import os

    # Configure logging up front so AGENZO_A2A_DEBUG traces appear from the first call. stderr-only
    # in stdio mode (stdout is the MCP protocol channel); a file is added when AGENZO_A2A_LOG_FILE set.
    cfg.setup_logging()

    transport = os.environ.get("AGENZO_MCP_TRANSPORT", "stdio").strip().lower()

    if transport == "sse":
        # FastMCP (mcp 1.x) reads host/port from its settings, not from run() kwargs.
        mcp.settings.host = os.environ.get("AGENZO_MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("AGENZO_MCP_PORT", "8080"))
        mcp.run(transport="sse")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
