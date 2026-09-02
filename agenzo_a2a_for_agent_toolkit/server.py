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
- ``guide()``              – concise cheat-sheet of the card sequences.
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

    - ``component``: the current card's ``component`` (e.g. "flight.offer-list", "hotel.search").
    - ``action``: one of that card's ``actions[].id`` (e.g. "submit", "select", "select-hotel",
      "select-rate", "confirm", "select-method", "paid", "poll").
    - ``payload``: the fields that action needs, taken from the previous card's ``data``
      (e.g. ``{"product_token": "..."}``, ``{"hotel_id": 606361}``).

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
    """Start a SEPARATE payment session (independent context) to pick/verify a card BEFORE
    confirming an order. Submits pay-setup and returns the ``payment.method-list`` card (the
    member's ACTIVE cards, each with a ``payment_brand``).

    Next steps (drive with ``act(payment_session_id, ...)``):
      - EVO card (Visa/Mastercard): use its ``id`` as ``payment_method_id`` directly in the booking
        confirm — no further payment steps needed.
      - UnionPay card: ``act(sid, "payment.method-list", "select-method", {"id": <id>,
        "payment_brand": "unionpay"})`` → returns ``payment.action-required`` with ``checkout_url``
        + ``payment_token_id``; call ``open_url(checkout_url)`` and have the user finish the passkey,
        then ``act(sid, "payment.action-required", "paid", {"payment_token_id": <id>})`` →
        ``poll(sid, "payment.await")`` until ``data.status == "ACTIVE"``. Use that ``payment_token_id``
        in the booking confirm.
      - No card listed: guide the user to bind one first (``act(sid, "payment.bind", "submit",
        {"user_email": "...", "brand": "unionpay"|"evo"})`` and follow the bind-required/await cards).

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


_GUIDE = """\
Agenzo A2A card driver — cheat sheet
====================================
General loop: call a tool -> read the LAST card's {component, data, actions} -> pick an action id
-> call `act(session_id, component, action, payload)` with fields copied from the card's `data`.
Server "text" (no cards) means the server is asking/telling you something -> reply with
`send_message(session_id, text)`. Booking + payment MUST use the same member_id.

FLIGHT (book):
  1) book("Book a one-way economy flight from Shanghai to Beijing on Aug 24 for 1 adult")
       -> card flight.search (prefilled). If fields missing, send_message to add them.
  2) act(sid,"flight.search","submit",{origin,destination,date,tripType:1,cabinClass,
       adultNum:1,childNum:0,infantNum:0})  -> flight.offer-list
  3) show offers (data.offers[]); act(sid,"flight.offer-list","select",{product_token})
       -> flight.booking-confirm (data.totalAmount/currency)
  4) [optional pay] start_payment(totalAmount*100, passengerName, contactPhone) -> see PAYMENT
  5) act(sid,"flight.booking-confirm","confirm",{passengers:[{surname,name,type:"adult",
       gender:"1",id_type:"2",id_number,birthday,expiration,nationality}],contact_name,
       contact_region:"86",contact_phone,contact_email, <payment_token_id | payment_method_id>?})
       -> flight.order-detail
  6) [optional] act(sid,"flight.order-detail","pay",{order_no}) to ticket.

HOTEL (book):
  1) book("Book a hotel near the Bund in Shanghai, 1 adult, check-in Aug 18, check-out Aug 19")
       -> hotel.search
  2) act(sid,"hotel.search","submit",{location,checkIn,checkOut,adults,children:0,roomNum:1,
       guestName,contactName,contactPhone}) -> hotel.location-select (destination candidates)
  3) act(sid,"hotel.location-select","select-destination",{destination_id,lat,lng})
       -> hotel.search-list
  4) act(sid,"hotel.search-list","select-hotel",{hotel_id}) -> hotel.detail (rooms[])
  5) act(sid,"hotel.detail","select-rate",{product_token,room_name,total_price:{amount,currency},
       price_items}) -> hotel.booking-confirm
  6) [optional pay] start_payment(...) -> see PAYMENT
  7) act(sid,"hotel.booking-confirm","confirm",{product_token,total_price:{amount,currency},
       price_items,guests:[{guest_name}],contact_name,contact_phone,
       <payment_token_id | payment_method_id>?}) -> hotel.order-detail

RIDE (book):
  0) RESOLVE FIRST (client-side — the ride backend does NOT geocode; NO hotel dependency):
       - resolve_location("<pickup place>") AND resolve_location("<dropoff place>") -> each returns
         {lat,lng,formatted_address,timezone}. Use those lat/lng at submit; NEVER guess coordinates.
       - scheduled trip: resolve_pickup_time("<local ISO datetime>","<IANA tz from resolve_location>")
         -> {epoch}; use epoch as pickupTime. Immediate trip: use the literal "now" (skip this call).
         NEVER compute epoch yourself. If geocoding is unconfigured the helper returns {error} -> ask
         the user for exact coordinates.
  1) book("Book a ride from PVG airport to The Bund, Shanghai, tomorrow 9pm, 1 passenger. Name
       Richard Chen, phone +8613275666789") -> ride.search (prefilled). Fill submit from step 0;
       pickupTime is the resolve_pickup_time epoch or the literal "now".
  2) act(sid,"ride.search","submit",{pickupName,pickupLat,pickupLng,dropoffName,dropoffLat,
       dropoffLng,pickupTime,passengerName,passengerPhone,passengerEmail,passengerCount:1})
       -> ride.vehicle-select (data.vehicleClasses[], each price{amount,currency,quote_id})
       NOTE: "No vehicle available" for an immediate ("now") trip is upstream stock, NOT an error
       -> retry submit with a scheduled pickupTime (a future epoch).
  3) act(sid,"ride.vehicle-select","select",{vehicle_class,price:{amount,currency,quote_id},
       passenger_capacity,luggage_capacity}) -> ride.payment-confirm (data.priceAmount/currency)
  4) PICK PAYMENT (agent-driven — there is NO in-flow card picker; you choose):
       - UnionPay/Visa: start_payment(priceAmount*100 incl. any surcharges you'll add, passengerName,
         recipient_account) -> see PAYMENT; finish passkey to ACTIVE -> use that payment_token_id.
       - A specific bound EVO card (Visa/Mastercard): start_payment(...) just to list cards, take a
         card id -> use it as payment_method_id (no passkey).
       - Omit both -> the platform auto-charges the developer's DEFAULT bound card (pay_per_call) or
         debits the balance (monthly_settlement).
  5) act(sid,"ride.payment-confirm","confirm",{quote_id,vehicle_class,
       price:{amount,currency,quote_id},passenger_name,passenger_phone,passenger_email,
       <payment_token_id | payment_method_id>?}) -> ride.order-tracking (data.status + payment_status)
       ORDER NUMBERS (show BOTH to the user on ride.order-tracking, clearly labelled):
         - data.rideOrderId  -> the PLATFORM order number (rio_…) — the user-facing booking id.
         - data.orderId (== data.rideId, e.g. 4149211) -> the elife UPSTREAM ride id; it is the
           handle passed as --order-id for status/cancel/update. Present it as the upstream/eLife id,
           NOT as "the order number". (rideOrderId may be absent on older backends -> then show only orderId.)
       NOTE: passenger_email is REQUIRED at book; vehicle_class is case-sensitive (pass verbatim).
       UnionPay MUST go via payment_token_id (an EVO preauth on a UnionPay card is declined). Mint the
       token for the EXACT total you'll book (base price + seat/meet-and-greet surcharges) so it matches.

PAYMENT (separate session):
  start_payment(amount_cents, recipient_name, recipient_account) -> payment.method-list
    - EVO card (payment_brand=evo): use card id as payment_method_id in the booking confirm.
    - UnionPay card: act(pay,"payment.method-list","select-method",{id,payment_brand:"unionpay"})
        -> payment.action-required{checkout_url, payment_token_id}; open_url(checkout_url);
           after user finishes passkey: act(pay,"payment.action-required","paid",{payment_token_id})
           -> poll(pay,"payment.await") until data.status=="ACTIVE" -> use payment_token_id.
    - No cards: bind one first: act(pay,"payment.bind","submit",{user_email,brand:"unionpay"|"evo"}).
        Both brands then use open_url (unified): payment.bind-required(-evo) carries a URL in its data
        (UnionPay: data.enrollUrl/enroll_url; EVO/Visa/Mastercard: data.dropinUrl/dropin_url — a hosted
        Drop-in card page); call open_url(<that URL>), have the user finish, then
        act(pay,"payment.bind-required(-evo)","bound",{payment_method_id?}) ->
        poll(pay,"payment.bind-await(-evo)") until data.status=="ACTIVE".
"""


@mcp.tool()
def guide() -> str:
    """Return a concise cheat-sheet of the hotel/flight/payment card sequences. Read this once when
    you start driving a booking to know which action ids and payload fields each card expects."""
    return _GUIDE


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
