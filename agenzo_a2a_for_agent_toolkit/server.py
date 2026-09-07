"""FastMCP server: lets AI agents drive the Agenzo A2A orchestrator to book hotels/flights.

Design
------
The orchestrator speaks a *domain-agnostic card protocol*: every step returns one or more cards
(``{component, kind, data, actions}``) and you advance by sending a structured action referencing
one of ``actions[].id`` with a payload built from the previous card's ``data``. These tools are a
thin, **stateful** bridge over that protocol — the chat agent is the client that reads cards, asks
the user what it needs, and calls the next tool. New domains/schemas work with zero changes here.

Standalone payment & refund (merchant-independent)
--------------------------------------------------
Besides paying WITHIN an order (see ``start_payment``), the platform exposes two standalone,
merchant-independent flows — just ``book(...)`` them in natural language and drive with ``act(...)``
like any other scenario (no code changes here):
  - **make a payment** (``pay`` scenario): pick a bound card, then a **confirm card**
    (``payment.pay-confirm``) shows amount + currency — you MUST get the USER's explicit ``confirm``
    before any money moves. Routing follows the chosen card's brand, same generic loop: UnionPay →
    ``payment.action-required`` (``open_url`` passkey → ``paid`` → poll) then the charge is captured
    and ``payment.pay-result`` is returned; Mastercard/EVO → ``payment.card-action-required``
    (``open_url`` 3DS → ``paid`` → ``poll`` ``payment.card-await`` until terminal) then
    ``payment.card-result``. A successful charge returns a ``charge_no`` — keep it for refunds.
  - **refund** (``refund`` scenario): give the ``charge_no`` (or ``payment_token_id``) and an optional
    partial amount; a **confirm card** (``payment.refund-confirm``) requires the USER's explicit
    ``confirm`` before the refund is issued; ``payment.refund-result`` returns the outcome.
Never auto-confirm a funds action (pay/refund) — always surface the confirm card's amount to the
user and only send ``confirm`` after they approve. Drive the whole sub-flow with structured
``act(...)`` (no free-text ``send_message`` mid-payment).

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
The normal tools return a normalized ``{state, text, cards[]}`` view of each A2A Task — the exact
JSON-RPC traffic is NEVER inlined into tool results (that would bloat the chat context). To see the
raw A2A protocol:
  - Set ``AGENZO_A2A_DEBUG=1`` (or ``configure(debug="1")``) to LOG each exchange (request body +
    response) to stderr, and to ``AGENZO_A2A_LOG_FILE`` when that path is configured. This goes to
    the log only, not into the model context.
  - Call ``inspect()`` any time (independent of the flag) to pull the captured raw exchanges on
    demand: request body, response frames, ``url``/``status``, and a ``curl`` reproduction.
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


def _result(session_id: str, status: int, raw: str) -> dict[str, Any]:
    """Normalize an A2A response into a chat-friendly dict (or an error).

    The raw A2A request/response is deliberately NOT inlined here (it would bloat the chat context).
    When ``AGENZO_A2A_DEBUG`` is on, each exchange is written to the log (stderr + ``AGENZO_A2A_LOG_FILE``
    if set); use the ``inspect`` tool to pull the exact JSON-RPC traffic on demand."""
    if status != 200:
        return {
            "session_id": session_id,
            "error": f"orchestrator returned HTTP {status}",
            "detail": _short(raw),
            "cards": [],
            "text": "",
        }
    task = final_task(raw)
    if task is None:
        return {
            "session_id": session_id,
            "error": "no A2A Task in response",
            "detail": _short(raw),
            "cards": [],
            "text": "",
        }
    out = normalize_task(task)
    out["session_id"] = session_id
    out["member_id"] = _get_bridge().member_id
    return out


def _tool_result(status: int, raw: str) -> dict[str, Any]:
    """Parse a plain JSON response from an orchestrator utility endpoint (e.g. ``/orch-public/tools/*``).

    On non-200 or non-JSON, surface a compact error dict instead of raising — the chat agent then
    knows geocoding is unavailable and can ask the user for coordinates."""
    if status != 200:
        return {"error": f"orchestrator returned HTTP {status}", "detail": _short(raw)}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "invalid JSON from orchestrator", "detail": _short(raw)}
    return obj if isinstance(obj, dict) else {"error": "unexpected response", "detail": _short(raw)}


# Actions that SETTLE money (place/charge an order). Kept small on purpose to avoid false positives.
_SETTLE_ACTIONS = {"confirm", "book"}
# Fields that carry an explicit, user-chosen payment credential on a settle action.
_PAYMENT_FIELDS = ("payment_method_id", "payment_token_id")


def _settle_without_payment(component: str, action: str, payload: dict[str, Any]) -> bool:
    """Detect an order action that SETTLES money but carries no explicit, user-chosen payment
    credential. Used to attach a NON-BLOCKING advisory to the result — the action is still
    forwarded to the orchestrator (any HARD stop belongs in the orchestrator/merchant backend, not
    in this thin bridge). Deliberately narrow (settle actions on booking/payment cards only) so it
    does not fire on unrelated confirms such as cancellations."""
    if (action or "").strip().lower() not in _SETTLE_ACTIONS:
        return False
    comp = (component or "").lower()
    # Skip actions that do NOT charge the card (cancellations, refunds, void, check-out), and the
    # STANDALONE payment scenario's own confirm (``payment.pay-confirm``): there the card is chosen
    # in-flow via ``select-method`` and threaded through ``$collected`` — it is not a merchant order
    # confirm that must carry a payment credential in its payload, so the advisory would misfire.
    # ("pay-confirm" is deliberately NOT a substring of "ride.payment-confirm", so merchant confirms
    # stay flagged.)
    if any(k in comp for k in ("cancel", "refund", "void", "checkout", "check-out", "pay-confirm")):
        return False
    if not any(k in comp for k in ("confirm", "booking", "payment", "order")):
        return False
    return not any(str((payload or {}).get(f) or "").strip() for f in _PAYMENT_FIELDS)


_PAYMENT_WARNING = (
    "POLICY WARNING: this order confirmation carried NO user-chosen payment method "
    "(payload had neither payment_method_id nor payment_token_id). You must resolve payment via "
    "start_payment() and let the USER pick a card BEFORE confirming — omitting it can cause a "
    "silent charge to a platform/member default card. If this order settles money, verify or cancel "
    "it, then redo the confirm carrying the payment credential the user explicitly selected."
)


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
      - debug: "1" to LOG each A2A exchange (request + response) to stderr (and to ``log_file`` when
        set), "0" to turn it off. It does NOT inline the raw traffic into tool results — that would
        bloat the chat context. Use the ``inspect`` tool (works regardless of this flag) to view the
        raw JSON-RPC on demand.
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

    IMPORTANT — pass the user's request VERBATIM and COMPLETE. Do NOT summarize or trim it: keep
    every detail the user stated, especially the traveler/guest NAME, PHONE and EMAIL. The server
    prefills the entry form by extracting fields from exactly this text, so dropping the name/phone
    forces an avoidable extra round-trip asking for them again.

    Examples of ``request`` (note the contact details are kept in):
      - "Book a one-way economy flight from Shanghai to Beijing on August 24th for 1 adult.
        Passenger: Richard Chen, passport E1234567, phone +86 13275666789, richard@example.com."
      - "Book a hotel near the Bund in Shanghai, 1 adult, check-in Aug 18, check-out Aug 19.
        Guest Richard Chen, phone 13275666789."

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

    USER CHOOSES — never auto-select. When the current card offers a CHOICE (a list with multiple
    rows, or several options: hotels, room types, room rates, flight offers, vehicle classes, payment
    methods), present the options to the user and send the chosen action ONLY after the user picks.
    Do not pick by price/distance/rating/position, and do not auto-pick even a single option — confirm
    it with the user first.

    To attach a payment result to a booking confirm, include ``payment_token_id`` (UnionPay) or
    ``payment_method_id`` (EVO/Visa/Mastercard) in the confirm ``payload`` (see ``start_payment``).
    An order ``confirm``/``book`` that settles money MUST carry the payment method the USER explicitly
    chose — never omit it to fall back to a platform/member default charge."""
    bridge = _get_bridge()
    payload = payload or {}
    try:
        status, raw = await bridge.send_action(_ctx(session_id), component, action, payload)
    except AuthError as exc:
        return {"session_id": session_id, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    out = _result(session_id, status, raw)
    # Non-blocking soft guardrail: flag a money-settling confirm that omitted an explicit,
    # user-chosen payment method (see _settle_without_payment). The action is still forwarded.
    if "error" not in out and _settle_without_payment(component, action, payload):
        out["payment_warning"] = _PAYMENT_WARNING
    return out


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
    """MERCHANT-ORDER PRE-PAYMENT ONLY. Use this to pick/mint a payment credential to attach to a
    booking ``confirm`` (hotel/flight/ride): it drives the payment ``pay-setup`` scenario and by
    itself NEVER charges the card (no charge, no ``charge_no``). For a STANDALONE payment (charge a
    card directly and get a refundable ``charge_no``) or a REFUND, do NOT call this — start those
    from ``book(...)`` with a natural-language request (e.g. "make a payment of 44.33 USD, cardholder
    phone +1..." or "refund charge chg_..."), then drive the returned cards with ``act(...)``.

    Start a SEPARATE payment session (independent context) to pick/verify a payment method BEFORE
    confirming an order. This is BRAND-AGNOSTIC — it returns the method-picker card listing ALL the
    member's payment methods (UnionPay AND EVO Visa/Mastercard); each row carries an ``id`` and a
    ``payment_brand``. It does NOT force UnionPay.

    MANDATORY & USER-DRIVEN. Call this BEFORE every order ``confirm``/``book`` that settles money —
    never confirm without a user-chosen payment method, and never rely on a platform/member "default"
    card. Present the returned methods to the user (brand + last4) and let the USER choose which one
    to use; do NOT auto-select, not even when exactly one card is listed (confirm that one with the
    user first). If the picker is EMPTY, ask the user to add a card and ask which brand they want
    (UnionPay or Visa/Mastercard) — do not pick the brand for them.

    Then drive it card-first with ``act(payment_session_id, ...)`` — read each card's ``actions`` /
    ``item_actions`` and follow the generic loop (see ``guide()``); do not assume component names:
      - Pick a listed method with its row action (it ``carries`` the ``id`` + ``payment_brand``).
        The routing follows the CHOSEN card's brand: an EVO card (Visa/Mastercard) is returned
        directly as ``payment_method_id`` (no passkey); a UnionPay card mints a network-token via a
        passkey (open_url → poll to ACTIVE) yielding ``payment_token_id``. Attach whichever the
        confirm action ``carries`` names.
      - If selecting a method returns a card with an ``open_url`` action (passkey / enrollment /
        Drop-in), call ``open_url`` on the URL at that card's ``data`` field, have the user finish,
        send the action's ``callback`` id, then ``poll(session_id, component)`` until
        ``data.status == "ACTIVE"``. Use the resulting credential in the booking confirm.
      - NO method listed / want a NEW card: use the picker's scenario-jump action (``add-method``)
        to bind one, then submit the add-method form. That form REQUIRES ``user_email`` and takes an
        optional ``brand`` that CHOOSES the card type: ``brand:"evo"`` binds a Visa/Mastercard (EVO
        Drop-in), any other/absent value binds a UnionPay card. So to add a Mastercard, submit
        ``{"user_email": "...", "brand": "evo"}`` — omitting ``brand`` defaults to UnionPay.

    Drive this session with STRUCTURED ``act(...)`` calls ONLY — do not send natural-language
    ``send_message`` here (free text in a payment sub-flow gets misrouted). Reuse THIS session for
    the whole payment sub-flow (pick / add-method / mint), don't open a new one per step.

    After you BIND a UnionPay card, mint its token BY PICKING IT: call ``start_payment`` again (or
    reuse this session), read ``payment.method-list``, and ``select-method`` the bound card (its row
    ``carries`` ``payment_brand="unionpay"``, which drives the network-token mint). Do NOT pass
    ``payment_method_id`` into the ``payment.setup`` submit — that skips the picker, leaves
    ``payment_brand`` uncaptured, and the token is silently never minted.

    ``amount_cents`` = order total × 100. ``recipient_account`` (phone or email) is only needed for
    the UnionPay network-token branch; leave it empty for EVO. ``member_id`` (optional) must match
    the booking's member."""
    bridge = _get_bridge()
    if member_id:
        await bridge.set_member(member_id)
    sid = _new_session("payment")
    try:
        # Brand-neutral routing seed. The orchestrator classifies intent by *literal substring*
        # match against the pay-setup keywords ("prepare payment" / "select payment method"), and
        # normalize() only lowercases + folds whitespace (it does NOT drop stop-words). So the seed
        # MUST contain one of those keywords VERBATIM as a substring — e.g. "prepare a payment" does
        # NOT match ("a" splits the phrase), which silently drops the payment scenario and makes the
        # follow-up payment.setup#submit fall back to a context-less agent turn (guard refusal).
        # Keep an exact keyword substring here, and keep it brand-neutral (do NOT presuppose UnionPay;
        # the picker lists all brands and routes by the card the user actually picks).
        status, raw = await bridge.send_text(_ctx(sid), "Prepare payment. Select payment method.")
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
        status, raw = await bridge.call_tool("/orch-public/tools/resolve-location", {"address": address})
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
            "/orch-public/tools/resolve-pickup-time",
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

CORE PRINCIPLE — THE USER CHOOSES, NOT YOU (applies to EVERY domain & step)
  Whenever a card presents a CHOICE — a list with multiple rows, or several actions/options that
  represent alternatives (hotels, room types, room rates, flight offers, vehicle classes, AND
  payment methods) — you MUST surface those options to the user and advance ONLY on the user's
  explicit pick. NEVER auto-select on the user's behalf — not by price, distance, rating, "lowest",
  "closest", list position, or because there is only ONE option. When a single option exists, still
  confirm it with the user before acting. Any step that settles money (an order `confirm`/`book`)
  requires the user's explicit go-ahead on BOTH what is being bought AND which payment method pays
  for it. The only things you may fill without asking are values the user ALREADY stated (name,
  phone, email, dates, counts, etc.). If you are unsure whether a decision is yours to make, it is
  not — ask the user.

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
    `data` (already-prefilled values) PLUS every field the user has ALREADY stated anywhere in the
    conversation (e.g. traveler/guest name, phone, email — even if the card didn't prefill them).
    Send them all in the FIRST submit; only when a value is genuinely unknown do you ASK the user
    (or send_message(session_id, "...")). Never fabricate a value, and never drop one you already
    have — omitting known name/phone/email just triggers an extra "please provide …" round-trip.
  • An action with `scenario: "<name>"` is a SCENARIO JUMP (e.g. adding a payment method): sending it
    returns that scenario's entry card; then keep following the same loop.
  • An action with `dispatch:"client"` + `capability:"open_url"` needs an out-of-band browser page:
    open the URL at the card's `data[<data_ref>]` via open_url(...), have the user finish, then send
    the action's `callback` id (carrying whatever it lists). Poll any `*-await` card with
    poll(session_id, component) until `data.status == "ACTIVE"` (or another terminal state).

ANSWERING THE SERVER
  • A response with `text` and no cards means the server is asking/telling you something  ->  reply
    with send_message(session_id, text).

PAYMENT — runs in its OWN session (BRAND-AGNOSTIC: UnionPay OR EVO Visa/Mastercard)
  • Booking + payment MUST share the same member_id.
  • MANDATORY — RESOLVE PAYMENT BEFORE EVERY ORDER. Before you send ANY order `confirm`/`book` that
    settles money, you MUST first run start_payment(...) and let the USER choose how to pay. NEVER
    confirm an order without an explicit, user-chosen payment credential — do NOT rely on, or fall
    back to, any "platform / developer / member default" card. If you skip this, the backend may
    silently charge whatever default it finds, which is exactly what must NOT happen.
  • start_payment(amount_cents, recipient_name, recipient_account) opens a SEPARATE payment session
    and returns a picker listing ALL the member's cards (any brand). Drive it with
    act(payment_session_id, ...) like the loop above.
      – IF ONE OR MORE CARDS ARE LISTED: present them to the user (brand + last4) and let the USER
        pick which card to use. Do NOT auto-select — not even when there is exactly ONE card; confirm
        that one with the user first. Only after the user picks do you `select-method` that row.
      – IF THE PICKER IS EMPTY (no cards): you MUST ask the user to add one before going further —
        there is no valid "just charge the default" path. Ask whether they want a UnionPay card or a
        Visa/Mastercard, then send `add-method` and submit its form (REQUIRES `user_email`; `brand`
        CHOOSES the type: `brand:"evo"` → Visa/Mastercard EVO Drop-in; omitted/other → UnionPay).
        Never pick the brand for the user.
  • Routing follows the card the USER picked: an EVO card is returned directly as payment_method_id;
    a UnionPay card mints a token via a passkey (open_url → poll to ACTIVE) → payment_token_id.
  • AFTER binding a UnionPay card, MINT THE TOKEN BY PICKING THE CARD — do NOT shortcut. Reuse the
    SAME payment session: submit `payment.setup` with ONLY {amount_cents, recipient_name,
    recipient_account} (do NOT put payment_method_id in it), read the `payment.method-list`, then
    `select-method` the card the user chose. That row `carries` `payment_brand="unionpay"`, which is
    what drives the network-token mint (checkout_url → open_url passkey → poll to ACTIVE →
    payment_token_id). If you instead pass `payment_method_id` in the setup submit, the picker/select
    step is skipped, `payment_brand` is never captured, the token is silently NOT minted, and the
    turn dead-ends — so always go through `select-method`.
  • DRIVE PAYMENT WITH STRUCTURED ACTIONS ONLY. Inside a payment/add-method session use
    act(session_id, component, action, payload) — do NOT send natural-language send_message there
    (free text in a payment sub-flow gets misrouted to hotel-search / refused). Reuse ONE payment
    session_id for the whole sub-flow; do not open a new session per step.
  • When you have the user-chosen credential, attach it to the booking `confirm` payload. The confirm
    card's action `carries` names the field (e.g. payment_token_id or payment_method_id). Do NOT omit
    it to let the platform charge a default — a confirm that settles money must always carry the
    payment method the user explicitly selected.

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
