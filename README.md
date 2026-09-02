# agenzo-a2a-for-agent-toolkit

An **MCP server** that lets AI agents book hotels and flights by driving the Agenzo
orchestrator's standard **A2A card protocol** — the same protocol the reference scripts
(`agenzo-agent-orchestrator-base/scripts/prod/prod_hotel_flow.py` / `prod_flight_flow.py`) use,
minus the interactive CLI. Any AI agent (Kiro, Claude Desktop, etc.) becomes the A2A client:
it reads each card, asks the user for anything it needs, and calls the next tool until the
order is placed.

## Why MCP

AI agents don't book travel natively, but they support MCP servers. This server exposes a small set of
**stateful tools** over the orchestrator's domain-agnostic card flow. Because the protocol is
schema-driven, new domains (added as orchestrator schemas) work here with **no code changes**.

## Tools

| Tool | Purpose |
| --- | --- |
| `configure(...)` | Configure the MCP server at runtime (override connection settings). |
| `discover()` | Agent card: which domains/skills are bookable + required client capabilities. |
| `guide()` | Domain-agnostic driving law + a LIVE catalog of bookable domains/scenarios (from the agent card). |
| `book(request)` | Start a booking conversation from natural language; returns a `session_id` + cards. |
| `send_message(session_id, text)` | Natural-language turn (answer a server follow-up). |
| `act(session_id, component, action, payload)` | Structured card action (the main driver). |
| `poll(session_id, component)` | Re-check an `*-await` card (`action:"poll"`). |
| `start_payment(amount_cents, recipient_name, recipient_account)` | Separate payment session: pick/verify a card before confirming. |
| `open_url(url)` | Open a checkout / card-enrollment page in the local browser. |
| `resolve_location(address)` | Geocode a place name → `{lat, lng, timezone}` (ride: call before `ride.search`). |
| `resolve_pickup_time(local_datetime, timezone)` | Local datetime → UTC epoch for a scheduled ride `pickupTime`. |
| `inspect(session_id, limit)` | Dump the exact A2A JSON-RPC request/response exchanges (debugging / integration). |

## Prerequisites

- The orchestrator running (default `http://localhost:8000`) and its platform (`:8001`).
- A developer `api_key` **or** an invitation code. See auth below.
- [`uv`](https://docs.astral.sh/uv/) installed (the server is launched with `uv run`).

## Configuration

Set these via the `env` block in `.kiro/settings/mcp.json` (recommended) or a local `.env`
(copy from `.env.example`):

- `AGENZO_A2A_BASE_URL` — orchestrator address (default `http://localhost:8000`).
- `AGENZO_A2A_AGENT_ID` — agent id (default `base-orchestrator`).
- `AGENZO_A2A_MEMBER_ID` — the end user placing the order (= JWT `sub`). Booking + payment share it.
- Auth (pick one):
  - `AGENZO_A2A_API_KEY` — reuse a key directly, or
  - `AGENZO_A2A_API_KEY_FILE` — path to a cached key file (defaults to `./api_key.local`; you can
    point it at `…/agenzo-agent-orchestrator-base/scripts/prod/api_key.local` to reuse the key the
    reference scripts already created), or
  - `AGENZO_A2A_INVITATION_CODE` — self-register when no key is found (key is cached for reuse).
- Debugging / raw protocol visibility (all optional):
  - `AGENZO_A2A_DEBUG` — `1` to attach the exact `raw_request` / `raw_response` to every tool result
    and log each exchange; `0` (default) keeps output normalized.
  - `AGENZO_A2A_LOG_FILE` — path to also write request/response traces to (in addition to stderr).
  - `AGENZO_A2A_DEBUG_BUFFER` — how many recent exchanges `inspect()` keeps in memory (default `50`).
  - `AGENZO_A2A_DEBUG_MAXLEN` — char cap applied to surfaced request/response bodies (default
    `20000`; `0` disables the cap).

## Register with your AI agent

Add the following to your MCP config — workspace `.kiro/settings/mcp.json` (or user-level
`~/.kiro/settings/mcp.json`). Merge into an existing `mcpServers` object if you already have one:

```json
{
  "mcpServers": {
    "agenzo-travel": {
      "command": "uvx",
      "args": ["agenzo-a2a-for-agent-toolkit"],
      "env": {
        "AGENZO_A2A_BASE_URL": "https://agent-dev.agenzo.com",
        "AGENZO_A2A_AGENT_ID": "your_agent_id",
        "AGENZO_A2A_MEMBER_ID": "your_member_id",
        "AGENZO_A2A_API_KEY": "your_api_key",
        "AGENZO_A2A_STREAM": "1",
        "AGENZO_A2A_HTTP_TIMEOUT": "180"
      },
      "disabled": false,
      "autoApprove": ["discover", "guide", "configure", "book", "send_message", "act", "poll", "start_payment", "open_url", "resolve_location", "resolve_pickup_time", "inspect"]
    }
  }
}
```

> If the package is not published to PyPI, use `--from git+https://...` to pull from your repo:
> ```json
> "args": ["--from", "git+https://your-gitlab.com/group/agenzo-a2a-for-agent-toolkit.git", "agenzo-a2a-for-agent-toolkit"]
> ```

After the orchestrator is up, reconnect the server from Kiro's **MCP Server** view (or restart
your agent). Then just ask in chat, e.g. *"Book a one-way flight from Shanghai to Beijing on Aug 24 for
1 adult"*, and the agent will drive the flow, asking you for passengers, card selection, etc.

## Run standalone (debug)

```bash
uv run python -m agenzo_a2a_for_agent_toolkit.server   # serves MCP over stdio
```

## Seeing the raw A2A protocol

The tools return a **normalized** view (`{state, text, cards[], primary_component}`) that is easy
for the chat agent to drive — it is *not* the raw A2A message. Likewise, what your MCP client (e.g.
Kiro) shows as the tool input (`{request, _meta: …}`) is the **MCP call**, not the A2A request: the
`request` string becomes a single `TextPart`, and the real JSON-RPC envelope (`contextId`,
`messageId`, `parts`, …) is built inside the bridge. Integrators who talk to A2A directly need the
real request/response — expose it two ways:

1. **`inspect(session_id?, limit?)`** — dumps the captured exchanges regardless of any flag. Each
   entry has the exact `request` body, the `response_raw` text plus structured `response_json`
   (blocking) or `response_frames` (streamed SSE), the `url` / `status` / `transport` / `context_id`,
   and a `curl` line that reproduces the call (token shown as `$AGENZO_A2A_TOKEN`).
2. **Debug mode** — set `AGENZO_A2A_DEBUG=1` (env) or call `configure(debug="1")`; every tool result
   then also carries `raw_request` / `raw_response` / `raw_exchange`, and each exchange is logged
   (stderr, plus `AGENZO_A2A_LOG_FILE` if set). In stdio transport, logs go to **stderr only** —
   stdout is the MCP protocol channel.

A captured `book(...)` turn looks like this on the wire:

```json
{
  "jsonrpc": "2.0",
  "id": "…",
  "method": "message/stream",
  "params": {
    "message": {
      "messageId": "…",
      "role": "user",
      "parts": [{ "kind": "text", "text": "Book a ride from …" }],
      "contextId": "…"
    }
  }
}
```

POSTed to `/a2a/agents/<agent_id>/v1/message:stream` (or `:send` when `AGENZO_A2A_STREAM=0`). One
A2A `contextId` is reused for the whole session (`start_payment` opens a separate one).

## Payment notes

Payment runs in its own session. `start_payment` returns your ACTIVE cards; **EVO** cards
(Visa/Mastercard) are used directly by `payment_method_id`, while **UnionPay** cards go through a
`checkout_url` passkey (Kiro opens it via `open_url`, you complete it, then it polls to `ACTIVE`
and gets a `payment_token_id`). The resulting id is attached to the booking `confirm`.

Security: this is a client only — it adds no network-exposed endpoints. It talks to the local
orchestrator with a short-lived Bearer token and can open your browser for checkout/enrollment.
