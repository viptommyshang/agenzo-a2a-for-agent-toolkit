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
Besides paying WITHIN an order (see ``start_payment``), the platform exposes three standalone,
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
  - **create a network token** (``create-token`` scenario): mint a REUSABLE network token for a
    bound card WITHOUT charging — pick a card, then it is minted by the CARD BRAND on ALL THREE
    rails: UnionPay (``open_url`` checkout passkey → poll to ACTIVE), Visa (``open_url`` payment_url
    FIDO passkey → poll to ACTIVE), or EVO/Mastercard (SYNCHRONOUS — no browser step). No charge
    and no ``charge_no`` occur; ``payment.token-result`` returns the ``payment_token_id`` (+ status).
    NOTE: minting is NOT UnionPay-only here — EVERY listed brand mints. (This differs from
    ``start_payment`` / ``pay``, where only UnionPay mints and EVO/Visa are handed back as a
    ``payment_method_id``.)
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


# ── Per-session opaque-token vault (tool-output slimming) ─────────────────────
# a2a.normalize_task shows the model a short handle (e.g. "@tok1") instead of an ~800-char opaque
# product_token/pt_… blob. This vault remembers handle → real token per session so ``act`` can swap
# the handle back to the real value BEFORE it goes on the wire — the orchestrator always receives
# the real token, and the trace log records the real value, so nothing is lost for driving/auditing.
class _TokenVault:
    """Bidirectional short-handle codec for one session's opaque tokens (in-memory, per session)."""

    __slots__ = ("_h2r", "_r2h", "_n")

    def __init__(self) -> None:
        self._h2r: dict[str, str] = {}
        self._r2h: dict[str, str] = {}
        self._n = 0

    def mint(self, real: str) -> str:
        """Return a stable short handle for ``real`` (same real → same handle within the session)."""
        handle = self._r2h.get(real)
        if handle is None:
            self._n += 1
            handle = f"@tok{self._n}"
            self._h2r[handle] = real
            self._r2h[real] = handle
        return handle

    def resolve(self, value: str) -> str:
        """Swap a handle back to its real token; pass through anything that isn't a known handle."""
        return self._h2r.get(value, value)


_token_vaults: dict[str, _TokenVault] = {}


def _vault(session_id: str) -> _TokenVault:
    v = _token_vaults.get(session_id)
    if v is None:
        v = _TokenVault()
        _token_vaults[session_id] = v
    return v


def _walk_resolve(value: Any, vault: _TokenVault) -> Any:
    if isinstance(value, dict):
        return {k: _walk_resolve(x, vault) for k, x in value.items()}
    if isinstance(value, list):
        return [_walk_resolve(x, vault) for x in value]
    if isinstance(value, str):
        return vault.resolve(value)
    return value


def _resolve_handles(session_id: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Swap any minted handle in an outgoing ``act`` payload back to its real opaque token.

    No-op when the session minted no handles (vault absent/empty), so a payload that never touched a
    handle-ized card passes through unchanged."""
    v = _token_vaults.get(session_id)
    if not v or not payload:
        return payload
    return _walk_resolve(payload, v)


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
    if cfg.PROJECT_TOOL_OUTPUT:
        # Slim the model-facing view: drop heavy content keys + handle-ize long opaque tokens into
        # this session's vault (act resolves them back before sending). Raw payload is untouched on
        # the wire and in the trace log.
        out = normalize_task(
            task,
            mint_handle=_vault(session_id).mint,
            token_min_len=cfg.TOKEN_HANDLE_MIN_LEN,
            str_cap=cfg.TOOL_OUTPUT_STR_CAP,
        )
    else:
        # Slimming off: keep the full card data verbatim (original behaviour).
        out = normalize_task(task, heavy_keys=frozenset())
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


# Actions that SETTLE money (place/charge an order). WHICH action charges depends on the domain:
# most domains charge at the order confirm/book, but HOTEL is lock-then-pay — its booking-confirm
# only LOCKS inventory (no money), and the charge (with the user-chosen card) is on `pay`
# (hotel.order-detail). Kept small on purpose to avoid false positives.
_SETTLE_ACTIONS = {"confirm", "book"}
_HOTEL_SETTLE_ACTIONS = {"pay"}
# Fields that carry an explicit, user-chosen payment credential on a settle action.
_PAYMENT_FIELDS = ("payment_method_id", "payment_token_id")


def _is_settle_action(component: str, action: str) -> bool:
    """判断本次卡片动作是否为「会实际扣款/收钱」的货币结算动作（与支付凭据无关，只看组件+动作）。

    这是 ``_settle_without_payment`` 与显式 EVO 分流（``_explicit_evo_no_mint``）共用的结算动作判定，
    抽取出来保证两处口径完全一致、避免漂移。判定范围刻意收窄（仅下单/支付卡上的 settle 动作），
    不会误伤取消这类无关 confirm。"""
    act = (action or "").strip().lower()
    comp = (component or "").lower()
    # Skip actions that do NOT charge the card (cancellations, refunds, void, check-out), and the
    # STANDALONE payment scenario's own confirm (``payment.pay-confirm``): there the card is chosen
    # in-flow via ``select-method`` and threaded through ``$collected`` — it is not a merchant order
    # confirm that must carry a payment credential in its payload, so the advisory would misfire.
    # ("pay-confirm" is deliberately NOT a substring of "ride.payment-confirm", so merchant confirms
    # stay flagged.)
    if any(k in comp for k in ("cancel", "refund", "void", "checkout", "check-out", "pay-confirm")):
        return False
    # HOTEL is lock-then-pay: its booking-confirm only LOCKS inventory (no money) — the charge, with
    # the user-chosen card, is on `pay` (hotel.order-detail). Every other domain charges at the order
    # confirm/book. (Hotel's 3DS `resume` reuses a persisted preauth and is intentionally NOT here.)
    # Selecting the settle action per-domain avoids BOTH a false-positive on hotel's non-charging
    # booking-confirm AND a miss on hotel's charging `pay`.
    settle_actions = _HOTEL_SETTLE_ACTIONS if "hotel" in comp else _SETTLE_ACTIONS
    if act not in settle_actions:
        return False
    return any(k in comp for k in ("confirm", "booking", "payment", "order"))


def _settle_without_payment(component: str, action: str, payload: dict[str, Any]) -> bool:
    """Detect an order action that SETTLES money but carries no explicit, user-chosen payment
    credential. Used to attach a NON-BLOCKING advisory to the result — the action is still
    forwarded to the orchestrator (any HARD stop belongs in the orchestrator/merchant backend, not
    in this thin bridge). Deliberately narrow (settle actions on booking/payment cards only) so it
    does not fire on unrelated confirms such as cancellations."""
    if not _is_settle_action(component, action):
        return False
    return not any(str((payload or {}).get(f) or "").strip() for f in _PAYMENT_FIELDS)


def _evo_explicit(payload: dict[str, Any]) -> bool:
    """本次结算 payload 是否**显式声明**用户选择了 EVO 直扣兜底轨（``evo_explicit`` 为真）。

    这是「显式 EVO」的**唯一**判据（R16 EVO_Explicit_Opt_In）——单凭 payload 里有 ``payment_method_id``
    （即选了一张已绑卡）**不**构成显式 EVO：那种情况的默认应是「用这张卡create Network_Token 再以
    payment_token_id 直扣」，而非 EVO 预授权+捕获直扣。只有用户**明确**表示「直接刷这张 EVO 卡、
    不走网络令牌」时，agent 才应在 payload 置 ``evo_explicit=true``。接受布尔或其字符串形态。"""
    v = (payload or {}).get("evo_explicit")
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "1", "yes", "on")


def _needs_token_mint(component: str, action: str, payload: dict[str, Any]) -> bool:
    """默认令牌主路径触发判据：本次是货币结算动作、**未**携带 payment_token_id、且**未**显式选 EVO
    （``evo_explicit`` 非真）。

    覆盖两种情形，二者的默认都应是「先create Network_Token 再以 payment_token_id 直扣」：
      ① payload 完全无支付凭据（尚未选卡）；
      ② payload 带 ``payment_method_id``（已选/已有一张绑卡）但**未**显式选 EVO —— 已有卡**不**是跳过
         创建网络令牌的理由，应用**这张卡**创建网络令牌（在 create-token 流程里选它，无需重新绑卡）。
    仅当已携带 payment_token_id（正走令牌直扣）或显式选了 EVO（``evo_explicit=true``）时不触发。"""
    if not _is_settle_action(component, action):
        return False
    p = payload or {}
    has_token = bool(str(p.get("payment_token_id") or "").strip())
    return not has_token and not _evo_explicit(p)


_PAYMENT_WARNING = (
    "POLICY WARNING: this money-settling order action carried NO user-chosen payment method "
    "(payload had neither payment_method_id nor payment_token_id). You must resolve payment via "
    "start_payment() and let the USER pick a card BEFORE this step — omitting it can cause a "
    "silent charge to a platform/member default card. If this action settles money, verify or cancel "
    "the order, then redo it carrying the payment credential the user explicitly selected. "
    "(Hotel is lock-then-pay: it charges at `pay` on hotel.order-detail — attach the card THERE, not "
    "at booking-confirm, which only locks the room.)"
)


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A 软引导：默认编排令牌现结三段式主路径（R14.1、R6.1/R6.2/R6.3）
# ─────────────────────────────────────────────────────────────────────────────
# 现结订单在「未显式提供 payment_method_id（未选 EVO 卡）」时，MCP 桥的默认编排不再回退到
# EVO / 平台默认卡，而是走「独立 Network_Token 现结」这条规范主路径：先锁单 → 独立 Payment_Session
# 创建网络令牌 → 回 Booking_Session 以 payment_token_id 直扣。该默认属**软引导**——桥层不做硬拦截，仅在
# 货币结算动作上附一条非阻塞的编排建议，指引 agent 按三段式顺序推进；动作本身仍照常转发给编排器。
_DEFAULT_TOKEN_ORCHESTRATION = (
    "DEFAULT ORCHESTRATION (soft guidance): this pay-per-call settle step carries NO explicit, "
    "user-chosen payment_method_id (EVO card) and NO payment_token_id. The DEFAULT main path is an "
    "INDEPENDENT Network_Token direct charge — and it MUST be MINTED FIRST, BEFORE you settle.\n"
    "PRE-EMPT THE ROUND-TRIP: do NOT settle a pay-per-call order without a payment_token_id — the "
    "platform hard-gate REJECTS a token-less, non-explicit-EVO pay-per-call settle (HTTP 400 "
    "INVALID_PAYMENT_METHOD), so calling this bare settle now just wastes a round-trip. Mint the "
    "token first, then settle WITH it. Orchestrate in THREE stages sharing ONE member_id across "
    "both sessions:\n"
    "  1) LOCK FIRST — complete create-order in THIS Booking_Session to obtain the order_id "
    "(hotel hho_… / flight ffo_… / ride rio_…) plus its Order_Authoritative_Amount/Currency.\n"
    "  2) MINT SECOND — in a SEPARATE Payment_Session call start_token_creation("
    "amount_cents=<order authoritative amount>, order_id=<that order_id>, member_id=<same member>) "
    "to mint the Network_Token; the minted token's frozen amount equals the order's authoritative "
    "amount and its external_transaction_id equals the order_id (strict order↔token binding).\n"
    "  3) CHARGE — back in THIS Booking_Session, (re)send this settle action carrying the minted "
    "payment_token_id (NOT payment_method_id); choose the payment_token_id field per the action's "
    "`carries`.\n"
    "EXISTING CARD IS NO EXCEPTION: if you already have or just picked a bound card (this payload "
    "carries a payment_method_id), that is NOT a reason to charge it directly — the DEFAULT is STILL "
    "to MINT a Network_Token FROM that SAME card. In stage 2 call start_token_creation and PICK THIS "
    "SAME already-bound card in the create-token method list (no re-binding needed); then settle with "
    "the minted payment_token_id. Skipping the bind step (card already exists) does NOT mean skipping "
    "the mint — the payment stays identical to a fresh-card booking, only the bind step is omitted.\n"
    "Only skip the token main path if the USER EXPLICITLY chose to charge their EVO card DIRECTLY "
    "instead of via a network token — then settle on the EVO rail WITH evo_explicit=true (see the "
    "explicit-EVO branch). Never fall back to a platform/developer/member default card."
)


def _default_token_orchestration_advice(
    component: str, action: str, payload: dict[str, Any]
) -> str | None:
    """方案 A 软引导（R14.1）：判断是否应对本次货币结算动作附「默认走独立 Network_Token 现结
    三段式主路径」的非阻塞编排建议，返回建议文案或 ``None``。

    触发条件（见 ``_needs_token_mint``）：本次是货币结算动作、未携带 payment_token_id（还没创建网络令牌）、
    且未显式选 EVO（``evo_explicit`` 非真）。这**包含**「payload 带 payment_method_id（选了已绑卡）
    但未显式选 EVO」的情形——已有卡默认也应先用该卡创建网络令牌，故此时仍给三段式主路径建议。仅当已带
    payment_token_id（正走令牌直扣）或显式选了 EVO（``evo_explicit=true``）时返回 ``None``。"""
    if not _needs_token_mint(component, action, payload):
        return None
    return _DEFAULT_TOKEN_ORCHESTRATION


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A 软引导：显式 EVO 分流不创建网络令牌（R14.2、R9.1）
# ─────────────────────────────────────────────────────────────────────────────
# 与默认三段式主路径（未显式选卡 → 创建网络令牌直扣）相对：当客户端**显式提供** payment_method_id（EVO 卡）
# 时，这是客户端明确选择 EVO 兜底轨结算的信号——此时编排应**明确**走 EVO 预授权+捕获、**不创建**
# Network_Token。这条分支在 11.1 里仅表现为「不给令牌建议」的沉默（`_settle_without_payment` 见到凭据
# 即返回 False）；本处把它表述为一条清晰、可断言的显式引导，同时严格保持**软引导**语义：尊重客户端
# 的显式选择，桥层不覆盖、不改卡、不把它改走令牌路径，也不硬拦截。
_EXPLICIT_EVO_ORCHESTRATION = (
    "EXPLICIT EVO SETTLEMENT (soft guidance): this pay-per-call settle step is an EXPLICIT EVO "
    "direct charge — the payload carries evo_explicit=true, signalling the USER explicitly chose to "
    "charge their EVO card DIRECTLY instead of minting a Network_Token. Honor that explicit choice — "
    "settle via the EVO preauth+capture fallback rail and DO NOT mint a Network_Token: the default "
    "token main path (create-order → mint → payment_token_id direct charge) is intentionally SKIPPED "
    "here. This is SOFT guidance only: the bridge respects the explicit EVO selection as-is and does "
    "NOT override, block, reroute to the token path, or fall back to any platform/developer/member "
    "default card. NOTE — evo_explicit=true is required here so the platform hard-gate (Plan B) lets "
    "this token-less EVO settle through; set it ONLY on a genuine explicit user EVO choice, never to "
    "silence the gate on the default path (whose correct behavior is to mint and pay with "
    "payment_token_id — including when a bound card already exists)."
)


def _explicit_evo_no_mint(component: str, action: str, payload: dict[str, Any]) -> bool:
    """方案 A 软引导（R14.2）：判断本次货币结算动作是否为**显式 EVO 直扣**（因而应明确走 EVO 兜底轨、
    **不创建** Network_Token）。

    触发条件（两者同时满足）：①是货币结算动作（见 ``_is_settle_action``）；②payload 显式声明
    ``evo_explicit=true``（用户明确选择直刷 EVO 卡，而非走网络令牌——见 ``_evo_explicit``）；且未携带
    payment_token_id（后者是令牌直扣路径，不属于 EVO 分流）。**注意**：单有 payment_method_id（选了
    已绑卡）但无 ``evo_explicit`` **不**触发本分支——那种情况归默认令牌主路径（用该卡创建网络令牌），由
    ``_needs_token_mint`` 承接。据此把「显式 EVO → 走 EVO、不创建网络令牌」与默认令牌主路径以 ``evo_explicit``
    为界互斥切分。"""
    if not _is_settle_action(component, action):
        return False
    p = payload or {}
    has_token = bool(str(p.get("payment_token_id") or "").strip())
    return _evo_explicit(p) and not has_token


def _explicit_evo_orchestration_advice(
    component: str, action: str, payload: dict[str, Any]
) -> str | None:
    """方案 A 软引导（R14.2）：显式 EVO 分流时返回「走 EVO 兜底轨、不创建网络令牌」的非阻塞引导文案，
    否则返回 ``None``。"""
    if not _explicit_evo_no_mint(component, action, payload):
        return None
    return _EXPLICIT_EVO_ORCHESTRATION


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A 软引导：令牌主路径失败回退不静默改卡（R14.3、R9.2）
# ─────────────────────────────────────────────────────────────────────────────
# 与默认三段式主路径（11.1）/ 显式 EVO 分流（11.2）互补，本段处理**失败**场景：令牌现结主路径的
# 三个环节——① 锁单（create-order）、② 创建 Network_Token（start_token_creation 相关子流程）、
# ③ 以 payment_token_id 直扣——任一环节返回失败/错误态时，桥层附一条**非阻塞**引导，说明失败发生在
# 主路径的哪一环，并提示「可回退到 EVO 兜底轨，但须由客户端显式决定；绝不静默改用 platform/developer/
# member 默认卡结算」。仍是软引导：桥层不硬拦截、不自动改卡、不替客户端决定是否回退 EVO——是否回退
# EVO 交由客户端显式决定。

# A2A 任务的终态失败状态（task.status.state，大小写无关比较）。只有这两个视为「失败」，其余
# （completed / input-required / working / canceled 等）不触发失败回退引导。取值口径与领域无关，
# 不依赖任何具体卡片内部字段，与本文件既有归一化（``normalize_task`` 的 ``state``）保持一致。
_TOKEN_PATH_FAILURE_STATES = frozenset({"failed", "rejected"})


def _is_failure_result(out: dict[str, Any]) -> bool:
    """判断一次 A2A 交互的归一化结果是否为「失败态」。

    两类领域无关的稳健信号（不依赖任何具体卡片内部字段）：
      ① 传输/协议级失败：``_result`` 在 HTTP 非 200 或响应中无 A2A Task 时置的 ``error`` 键；
      ② 业务级失败终态：Task 的 ``state`` 为 ``failed`` / ``rejected``。"""
    if not isinstance(out, dict):
        return False
    if out.get("error"):
        return True
    return str(out.get("state") or "").strip().lower() in _TOKEN_PATH_FAILURE_STATES


# 令牌主路径三环节的人类可读标签（把失败定位到具体环节）。
_TOKEN_PATH_FAILURE_STAGES = {
    "lock": "LOCKING the order (create-order)",
    "mint": "MINTING the Network_Token (the start_token_creation / payment-credential sub-flow)",
    "charge": "the payment_token_id DIRECT CHARGE",
}

_TOKEN_PATH_FAILURE_ADVICE = (
    "TOKEN MAIN-PATH FAILURE (soft guidance): the default INDEPENDENT Network_Token settle path "
    "FAILED at {stage}. The main path is create-order (lock) → mint Network_Token → payment_token_id "
    "direct charge; this failure is in that path (NOT an explicit EVO settlement). You MAY fall back "
    "to the EVO preauth+capture fallback rail, but that fallback is the CLIENT's EXPLICIT decision: "
    "surface this failure to the USER and only settle via EVO if the user explicitly chooses it (then "
    "resend the settle action carrying the user-chosen EVO payment_method_id). Do NOT silently retry, "
    "and — above all — NEVER silently fall back to any platform / developer / member DEFAULT card to "
    "settle this order."
)


def _token_path_stage(
    session_id: str, component: str, action: str, payload: dict[str, Any]
) -> str | None:
    """把一次交互定位到令牌现结主路径的哪一环，返回 ``"lock"`` / ``"mint"`` / ``"charge"``；若本次
    交互不属于令牌主路径（例如显式 EVO 结算、或与资金无关的普通卡片动作）则返回 ``None``。

    判定顺序（互斥）：
      ① 独立 Payment_Session（``kind == "payment"``，由 start_token_creation / start_payment 开启）
         内的任何交互 → ``"mint"``（创建网络令牌 / 支付凭据准备子流程，含其 passkey/poll 环节）。
      ② Booking_Session 内 payload 显式携带 ``payment_token_id`` → ``"charge"``（令牌直扣）。
      ③ Booking_Session 内**显式声明 evo_explicit=true（显式 EVO 直扣）** → 非令牌主路径，返回
         ``None``（EVO 结算失败属 R9.5，不在本令牌回退引导范围）。注意：仅带 payment_method_id 但无
         evo_explicit（选了已绑卡、默认应先创建网络令牌）**不**属此列，仍按 ④ 归为 ``"lock"``。
      ④ Booking_Session 内的货币结算动作（见 ``_is_settle_action``）且未带任何显式凭据 → ``"lock"``
         （默认主路径的锁单 / 结算环节）。"""
    kind = (_sessions.get(session_id, {}) or {}).get("kind")
    if kind == "payment":
        return "mint"
    p = payload or {}
    if str(p.get("payment_token_id") or "").strip():
        return "charge"
    if _explicit_evo_no_mint(component, action, p):
        return None
    if _is_settle_action(component, action):
        return "lock"
    return None


def _token_path_failure_advice(
    session_id: str, component: str, action: str, payload: dict[str, Any], out: dict[str, Any]
) -> str | None:
    """方案 A 软引导（R14.3）：当本次交互属于令牌现结主路径且结果为失败态时，返回定位到具体环节的
    「失败 + 可显式回退 EVO、绝不静默改用默认卡」引导文案；否则返回 ``None``。"""
    if not _is_failure_result(out):
        return None
    stage = _token_path_stage(session_id, component, action, payload)
    if stage is None:
        return None
    return _TOKEN_PATH_FAILURE_ADVICE.format(stage=_TOKEN_PATH_FAILURE_STAGES[stage])


def _attach_token_path_failure(
    out: dict[str, Any], session_id: str, component: str, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """若本次交互命中令牌主路径失败回退场景（R14.3），把引导挂到 ``out["token_path_failure"]`` 后
    原样返回。非阻塞：不改动其余字段、不改写卡片、不替客户端决定是否回退 EVO。"""
    advice = _token_path_failure_advice(session_id, component, action, payload, out)
    if advice:
        out["token_path_failure"] = advice
    return out


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

    To attach a payment result, include ``payment_token_id`` (UnionPay) or ``payment_method_id``
    (EVO/Visa/Mastercard) in the payload of the action that SETTLES money (see ``start_payment``).
    That action varies by domain: most domains charge at the order ``confirm``/``book``, but HOTEL is
    lock-then-pay — its ``booking-confirm#confirm`` only LOCKS the room (no charge), and the payment
    method goes on ``hotel.order-detail#pay``. Whichever action settles money MUST carry the payment
    method the USER explicitly chose — never omit it to fall back to a platform/member default charge.
    Read the action's ``carries`` to know which field name to send."""
    bridge = _get_bridge()
    payload = payload or {}
    # Swap any short token handle the model copied from a slimmed card back to the real opaque token
    # before it goes on the wire (no-op if this session minted no handles). Done first so both the
    # A2A call and the soft-guidance below see the real payment_token_id / product_token.
    payload = _resolve_handles(session_id, payload) or {}
    try:
        status, raw = await bridge.send_action(_ctx(session_id), component, action, payload)
    except AuthError as exc:
        return {"session_id": session_id, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    out = _result(session_id, status, raw)
    # 非阻塞软引导（三分支互斥，失败态优先）：动作本身始终照常转发给编排器（硬拦截属于编排器/商户
    # 后端，不在这层薄桥里做）。
    #   ⓪ 令牌主路径失败回退（R14.3）：锁单 / 创建网络令牌 / payment_token_id 直扣任一环节失败（传输错误或
    #      failed/rejected 终态）→ 附 token_path_failure 引导（指示失败环节 + 可显式回退 EVO + 绝不
    #      静默改用默认卡）；失败优先，不再叠加下面的成功态软引导。
    #   ① 显式 EVO 分流（R14.2）：payload 显式声明 evo_explicit=true（用户明确选择直刷 EVO 卡而非
    #      网络令牌）→ 明确走 EVO 兜底轨、不创建网络令牌；软引导，尊重客户端显式选择，不覆盖、不改走令牌路径。
    #   ② 默认三段式主路径（R14.1）：现结且未携 payment_token_id、且未显式选 EVO（evo_explicit 非真）
    #      → 默认走独立 Network_Token 现结（锁单 → 独立会话创建网络令牌 → 回会话以 payment_token_id 直扣）。
    #      **含**「payload 带 payment_method_id（选了已绑卡）但未显式选 EVO」的情形——已有卡默认也应
    #      先用该卡创建网络令牌。仅当真的无任何凭据时才附「未选卡」默认卡警告（有 payment_method_id 时不附）。
    fail_advice = _token_path_failure_advice(session_id, component, action, payload, out)
    if fail_advice:
        out["token_path_failure"] = fail_advice
        return out
    if "error" not in out:
        evo_advice = _explicit_evo_orchestration_advice(component, action, payload)
        if evo_advice:
            out["evo_orchestration"] = evo_advice
        else:
            advice = _default_token_orchestration_advice(component, action, payload)
            if advice:
                out["default_orchestration"] = advice
                # 「未选任何卡、可能被静默扣默认卡」的警告仅在**真的无任何支付凭据**时附上；
                # 若已带 payment_method_id（选了已绑卡、只是默认应先用它创建网络令牌），该前提不成立，不附。
                if _settle_without_payment(component, action, payload):
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
    # 令牌主路径失败回退（R14.3）：创建网络令牌子流程的 passkey/poll 等环节若返回失败态，附回退引导（仅
    # Payment_Session 命中 mint 环节；Booking_Session 的普通 *-await 轮询不触发）。
    return _attach_token_path_failure(_result(session_id, status, raw), session_id, component, "poll", {})


# ─────────────────────────────────────────────────────────────────────────────
# 方案 A 软引导：start_payment 入口即提醒「令牌主路径优先」（R12.2/R14.1 的提前触发）
# ─────────────────────────────────────────────────────────────────────────────
# 之前 _default_token_orchestration_advice 只在「裸结算（未带任何凭据）」时才触发，而 agent 往往
# 一开始就按老指引走 start_payment 选卡、拿回 payment_method_id，令牌建议永远来不及出现。这里在
# start_payment 的返回上恒附一条非阻塞提醒：说明它是显式/回退 EVO 轨，pay-per-call 默认应走
# start_token_creation 创建网络令牌直扣——把令牌主路径的引导**提前**到 agent 刚进入选卡这一刻。
_START_PAYMENT_TOKEN_REMINDER = (
    "REMINDER (soft guidance): start_payment is the EXPLICIT / FALLBACK EVO rail, NOT the pay-per-call "
    "default. If the user has NOT explicitly chosen an EVO card for a pay-per-call order, the DEFAULT "
    "main path is an INDEPENDENT Network_Token direct charge via start_token_creation: create-order "
    "(lock the order_id + authoritative amount) → start_token_creation(amount_cents=<that amount>, "
    "order_id=<that order_id>, member_id=<same member>) → settle the money-action with the minted "
    "payment_token_id (NOT payment_method_id). Only continue on THIS rail if the user explicitly picked "
    "an EVO card, or you are falling back after a token-path failure with the user's consent — and "
    "NEVER fall back to any platform / developer / member default card. If you DO settle via the EVO "
    "rail on the user's explicit choice, the pay-per-call settle payload MUST carry evo_explicit=true "
    "(the server-side hard-gate rejects a token-less pay-per-call settle without it)."
)


@mcp.tool()
async def start_payment(
    amount_cents: int, recipient_name: str, recipient_account: str = "", member_id: str = ""
) -> dict[str, Any]:
    """MERCHANT-ORDER PRE-PAYMENT — EXPLICIT / FALLBACK EVO RAIL ONLY (not the pay-per-call default).
    Use this to pick a payment credential to attach to a booking ``confirm`` (hotel/flight/ride): it
    drives the payment ``pay-setup`` scenario and by itself NEVER charges the card (no charge, no
    ``charge_no``).

    NOT THE DEFAULT MAIN PATH. For a pay-per-call order where the user has NOT explicitly chosen an
    EVO card, the DEFAULT is an INDEPENDENT Network_Token direct charge via ``start_token_creation``
    (create-order to lock the order_id + authoritative amount → mint a Network_Token bound to that
    order_id → settle with ``payment_token_id``). Call ``start_payment`` ONLY when the user
    EXPLICITLY picks an EVO card, or as a FALLBACK after a token-path stage fails and the user opts
    to fall back. See ``guide()`` for the full precedence.

    For a STANDALONE payment (charge a
    card directly and get a refundable ``charge_no``) or a REFUND, do NOT call this — start those
    from ``book(...)`` with a natural-language request (e.g. "make a payment of 44.33 USD, cardholder
    phone +1..." or "refund charge chg_..."), then drive the returned cards with ``act(...)``.
    To CREATE A STANDALONE NETWORK TOKEN (mint-only, no charge) do NOT call this either — that is
    the ``create-token`` scenario via book("create a network token …"), and it mints on ALL THREE
    rails (UnionPay / Visa / EVO). The "only a UnionPay card mints a network-token; EVO/Visa handed
    back as ``payment_method_id``" behavior described below is specific to THIS merchant pre-payment
    path.

    Start a SEPARATE payment session (independent context) to pick/verify a payment method BEFORE
    confirming an order. This is BRAND-AGNOSTIC — it returns the method-picker card listing ALL the
    member's payment methods (UnionPay AND EVO Visa/Mastercard); each row carries an ``id`` and a
    ``payment_brand``. It does NOT force UnionPay.

    USER-DRIVEN (explicit/fallback only). Do NOT call this as the default pre-step for every settle —
    that is ``start_token_creation`` (see above). When this EVO rail IS used, never confirm without a
    user-chosen payment method, and never rely on a platform/member "default"
    card. Present the returned methods to the user (brand + last4) and let the USER choose which one
    to use; do NOT auto-select, not even when exactly one card is listed (confirm that one with the
    user first). If the picker is EMPTY, ask the user to add a card and ask which brand they want
    (UnionPay or Visa/Mastercard) — do not pick the brand for them.

    Then drive it card-first with ``act(payment_session_id, ...)`` — read each card's ``actions`` /
    ``item_actions`` and follow the generic loop (see ``guide()``); do not assume component names:
      - Pick a listed method with its row action (it ``carries`` the ``id`` + ``payment_brand``).
        The routing follows the CHOSEN card's ``payment_brand``: a UnionPay card mints a
        network-token via a passkey (open_url → poll to ACTIVE) yielding ``payment_token_id``;
        **any other brand** (``evo`` for Mastercard-and-friends, ``visa`` for a VTS-bound Visa) is
        returned directly as ``payment_method_id`` (no passkey) for the host order to settle
        server-side. Attach whichever the confirm action ``carries`` names.
      - If selecting a method returns a card with an ``open_url`` action (passkey / enrollment /
        card entry), call ``open_url`` on the URL at that card's ``data`` field, have the user finish,
        send the action's ``callback`` id, then ``poll(session_id, component)`` until
        ``data.status == "ACTIVE"``. Use the resulting credential in the booking confirm.
      - NO method listed / want a NEW card: use the picker's scenario-jump action (``add-method``)
        to bind one, then submit the add-method form. That form REQUIRES ``user_email``; the optional
        ``brand`` only chooses **which rail**, NOT the card brand:
          · ``brand:"unionpay"`` → UnionPay Agent Pay enrollment (its own ``enroll_url``).
          · **omitted** (or ``"card"`` / anything that is not ``unionpay``) → the ONE platform-hosted
            **unified card-entry page**. Submit ``{"user_email": "..."}`` and open the returned
            ``link_url``: the cardholder types the card there and **the page itself routes by the
            card's BIN** — Visa (4…) runs the native VTS + Payment Passkey rail, any other brand
            (Mastercard, …) runs EVO. So you do NOT need to know the brand up front, and there is
            nothing to pass for "Visa" vs "Mastercard".
        ⚠️ Omitting ``brand`` does **NOT** default to UnionPay — it goes to the unified card page.
        To bind a UnionPay card you MUST pass ``brand:"unionpay"`` explicitly. (Legacy note: the old
        ``brand:"evo"`` value still lands on the unified card page, since the gate is simply
        "is it unionpay or not", but it is obsolete — don't emit it.)

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
    out = _result(sid, status, raw)
    # 方案 A 软引导（提前触发）：进入 start_payment 选卡即提醒「令牌主路径优先、EVO 仅显式/回退」，
    # 不阻塞流程、不改卡。
    if "error" not in out:
        out["token_path_reminder"] = _START_PAYMENT_TOKEN_REMINDER
    return out


@mcp.tool()
async def start_token_creation(
    amount_cents: int,
    recipient_name: str = "",
    recipient_account: str = "",
    member_id: str = "",
    order_id: str = "",
) -> dict[str, Any]:
    """Mint a REUSABLE Network_Token in an INDEPENDENT payment session (create-token scenario).

    This is the token-minting entry point for the token-first flow: it opens a SEPARATE A2A context
    (a Payment_Session, isolated from any Booking_Session) and drives the ``create-token`` scenario,
    which mints a reusable network token for a bound card WITHOUT charging. No money moves and no
    ``charge_no`` is produced; ``payment.token-result`` returns the ``payment_token_id`` (+ status).

    USE THIS EVEN WHEN THE CARD ALREADY EXISTS. The ``create-token`` scenario LISTS the member's
    already-bound cards (``payment.method-list``) so you pick an EXISTING card to mint from — you do
    NOT need to re-bind. So for "pay with an existing card", the default is still: create-order →
    start_token_creation (pick that existing card, pass order_id) → pay with the minted
    payment_token_id. Having a bound card is NEVER a reason to charge it directly via the EVO rail;
    that EVO direct charge is only for an explicit user choice to skip the network token.
    Minting works on ALL THREE rails by the CARD's brand: UnionPay (``open_url`` checkout passkey →
    poll to ACTIVE), Visa (``open_url`` payment_url FIDO passkey → poll to ACTIVE), or EVO/Mastercard
    (SYNCHRONOUS — no browser step).

    WHY A SEPARATE SESSION: an A2A context owns a single ``flow_state`` (one active scenario, no
    scenario stack); a cross-domain strong intent RESETS that ``flow_state``. Minting inline in a
    Booking_Session would wipe the booking's flow_state — so minting always runs in its OWN
    Payment_Session here. The Payment_Session MUST share the same ``member_id`` as its Booking_Session
    so the token and the order are attributed to the same end user; pass ``member_id`` to keep them
    aligned (it defaults to the configured member otherwise).

    STRUCTURED ACTIONS ONLY: drive the returned cards with ``act(session_id, ...)`` — never
    ``send_message`` free text inside this payment sub-flow (free text gets misrouted). Reuse THIS
    session_id for the whole minting sub-flow (pick card → passkey/poll → token-result); do not open
    a new session per step. When a card lists multiple bound cards, present them to the USER (brand +
    last4) and ``select-method`` only the one the user picks — do NOT auto-select, not even a lone card.

    ``amount_cents`` = the token's authoritative amount × 100 (integer, smallest currency unit).
    ``recipient_name`` / ``recipient_account`` are the PAYER's own name + phone/email, needed ONLY for
    the UnionPay rail (leave empty for Visa/EVO; the flow re-collects them if a UnionPay card is
    picked). ``member_id`` (optional) MUST match the booking's member.

    ``order_id`` (optional) is the MERCHANT ORDER NUMBER (order_id) this token must be BOUND to —
    the ``order_id`` returned by the Booking_Session's create-order/lock step (e.g. hotel ``hho_…``,
    flight ``ffo_…``). When you mint a token FOR a specific order, pass it: the bridge forwards it as
    ``external_transaction_id`` in the create-token entry card's ``submit`` payload, so the platform
    fixes it onto the token document. Charge_Service then enforces strict order↔token binding —
    a token minted for one order cannot be used to charge another (same-amount cross-order misuse is
    rejected). Leave it empty for a standalone, order-less token.

    TRIP AGGREGATE — bind to the PAYMENT GROUP, not a single order. When paying a whole trip cart in
    ONE aggregate charge (the trip cart-summary checkout), pass ``order_id`` = the payment-group id
    (a ``pg_…`` value shown on that cart-summary card), and ``amount_cents`` = the AGGREGATE TOTAL ×
    100 (the cart's total, i.e. the sum of all the locked sub-orders — NOT any single sub-order's
    amount). The bridge forwards that ``pg_…`` as the token's ``external_transaction_id``; the
    platform's batch settlement then charges the ONE aggregate token against the payment group (its
    binding accepts the payment-group id), covering every sub-order at once. EVO/Mastercard mints this
    aggregate token SYNCHRONOUSLY (no passkey). See ``guide()``'s TRIP AGGREGATE PAYMENT section for
    the full mint→checkout→confirm→settle order."""
    bridge = _get_bridge()
    if member_id:
        await bridge.set_member(member_id)
    sid = _new_session("payment")
    try:
        # Brand-neutral routing seed for the create-token scenario. The orchestrator classifies
        # intent by *literal substring* match against each scenario's keywords, checked in
        # allowed_scenarios order (refund → pay → pay-setup → create-token → add-method), first hit
        # wins. This seed MUST contain a create-token keyword VERBATIM ("create a network token") and
        # MUST NOT contain any earlier scenario's keyword as a substring — notably NOT "select payment
        # method"/"prepare payment" (those belong to pay-setup and would hijack the classification).
        # It is the entry-point classification seed, not a mid-flow turn; everything after is driven
        # with structured actions.
        status, raw = await bridge.send_text(_ctx(sid), "Create a network token.")
        if status != 200:
            # 创建网络令牌分类种子即传输失败 → 附「创建网络令牌环节失败、可显式回退 EVO」引导（R14.3）；本会话
            # 为独立 Payment_Session（kind="payment"），命中 mint 环节。
            return _attach_token_path_failure(
                _result(sid, status, raw), sid, "payment.token-setup", "submit", {}
            )
        submit_payload: dict[str, Any] = {
            "amount_cents": int(amount_cents),
            "recipient_name": recipient_name,
            "recipient_account": recipient_account,
        }
        # R12.4 / R6.3: minting a token FOR a specific order — forward the merchant order_id as the
        # token's external_transaction_id so the platform fixes the order↔token binding at mint time.
        # Non-empty only (an order-less standalone token omits it and stays unbound).
        if order_id:
            submit_payload["external_transaction_id"] = order_id
        status, raw = await bridge.send_action(
            _ctx(sid),
            "payment.token-setup",
            "submit",
            submit_payload,
        )
    except AuthError as exc:
        return {"session_id": sid, "error": f"auth failed: {exc}", "cards": [], "text": ""}
    # 创建网络令牌提交若返回失败/错误态 → 附「创建网络令牌环节失败、可显式回退 EVO、绝不静默改用默认卡」引导
    # （R14.3）；成功则原样返回。
    return _attach_token_path_failure(
        _result(sid, status, raw), sid, "payment.token-setup", "submit", submit_payload
    )


@mcp.tool()
def open_url(url: str) -> dict[str, Any]:
    """Open a URL in the local default browser — used for the out-of-band pages of any card whose
    action has ``dispatch:"client"`` + ``capability:"open_url"``: UnionPay checkout (passkey),
    UnionPay card-enrollment (``enroll_url``), and the **unified card-entry page** (``link_url``,
    hosted by the platform) where the cardholder types any Visa/Mastercard and the page routes by
    the card's BIN (Visa → VTS + Payment Passkey; other brands → EVO). The MCP server runs on the
    user's machine, so this pops the page for the user to complete the passkey/enrollment/card
    entry. After they finish, drive the next card (e.g. "paid" then "poll", or "bound" then "poll").

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
  • EXCEPTION — payment / add-method sub-flows: NEVER send_message there, even on a text-with-no-cards
    reply (free text in a payment sub-flow gets misrouted to hotel-search / refused — this is the one
    place the rule above does NOT apply). Re-drive with a structured act(...) on the last card instead.
    If you reached a text-with-no-cards dead-end trying to ADD a card mid-checkout, that binding is not
    supported inside the booking/checkout session: bind the card in a SEPARATE standalone payment
    session (start_payment / book "add a payment method …"), then return to checkout and re-open the
    method picker — the newly bound card shows up there (booking + payment must share the same
    member_id). Checkout method pickers only offer `select-method`, not `add-method`, for this reason.

PAYMENT — INDEPENDENT NETWORK-TOKEN DIRECT CHARGE is the DEFAULT MAIN PATH (own session)
  • Booking + payment MUST share the same member_id.
  • DEFAULT / CANONICAL MAIN PATH — you MUST take it UNLESS the user EXPLICITLY chose an EVO card.
    For a pay-per-call order, settle via an INDEPENDENT NETWORK-TOKEN DIRECT CHARGE — a THREE-STAGE
    flow orchestrated through start_token_creation (NOT start_payment), in EXACTLY this order
    (LOCK FIRST → MINT SECOND → CHARGE):
      1) LOCK FIRST — complete create-order to obtain the order_id (hotel hho_… / flight ffo_… /
         ride rio_…) AND its authoritative amount/currency BEFORE minting.
      2) MINT SECOND — in a SEPARATE Payment_Session call start_token_creation(
         amount_cents=<that authoritative amount>, order_id=<that order_id>,
         member_id=<the SAME member_id as the booking>) to mint the Network_Token. The token's
         frozen amount equals the order's authoritative amount; passing order_id sets the token's
         external_transaction_id = order_id (STRICT order binding).
      3) CHARGE — back in the Booking session, settle the money-action carrying the minted
         payment_token_id (NOT payment_method_id); read the action's `carries` for the field name.
    Three hard rules bind this path: (1) LOCK FIRST, MINT SECOND — mint for the exact locked amount
    so the token's frozen amount equals the order's authoritative amount; (2) SAME member_id ACROSS BOTH SESSIONS
    — otherwise the token and the order belong to different users; (3) STRICT ORDER BINDING — at
    charge time the backend REFUSES any token whose external_transaction_id is missing or not equal
    to this order (no cross-order reuse, even at the same amount).
    ALL THREE DOMAINS follow create-order → mint → pay, INCLUDING RIDE: ride's create-order locks no
    upstream (eLife) inventory but STILL returns an authoritative rio_ order_ref + amount/currency,
    so a ride token charge is NOT a single-step book — create-order FIRST, then mint for that exact
    amount with external_transaction_id = order_ref, then pay. Drive the whole mint sub-flow with
    structured actions only, reusing ONE Payment_Session.
  • EXISTING (ALREADY-BOUND) CARD IS NO EXCEPTION — SAME PATH, MINUS THE BIND STEP. If the member
    already has a bound card, you STILL mint a Network_Token FROM that card and pay with
    payment_token_id — do NOT charge the bound card directly. start_token_creation drives the
    create-token scenario, which LISTS the member's existing bound cards; pick the SAME card there to
    mint from it (no re-binding). So "pay with an existing card" = create-order → start_token_creation
    (pick that card, bound to order_id) → pay with payment_token_id — identical to a fresh-card
    booking except you skip the bind step. Skipping the bind does NOT mean skipping the mint.
  • EVO preauth+capture is NOT the default — it is the EXPLICIT / FALLBACK rail ONLY. Take it ONLY
    when (a) the user EXPLICITLY chooses to charge their EVO card DIRECTLY instead of via a network
    token (merely HAVING or picking a bound EVO card is NOT this — that still mints a token from the
    card, see above), or (b) a stage of the token path above fails and the user explicitly opts to
    fall back. To use it, run start_payment(...) (the method picker). NEVER settle by
    silently falling back to any platform / developer / member default card, and NEVER auto-pick
    EVO just because it is the only bound card — confirm with the user first.
    HARD GATE (server-side): a pay-per-call settle that has NO payment_token_id is REJECTED with a
    retriable=false guidance error UNLESS the pay payload carries evo_explicit=true. So when — and
    ONLY when — the user explicitly chose the EVO rail, include evo_explicit=true in the settle
    payload (alongside any payment_method_id). Never set evo_explicit=true to silence the gate on the
    default path; the correct default is to mint a Network_Token and pay with payment_token_id.
  • start_payment(amount_cents, recipient_name, recipient_account) — EXPLICIT / FALLBACK ONLY —
    opens a SEPARATE payment session and returns a picker listing ALL the member's cards (any
    brand). Drive it with act(payment_session_id, ...) like the loop above.
      – IF ONE OR MORE CARDS ARE LISTED: present them to the user (brand + last4) and let the USER
        pick which card to use. Do NOT auto-select — not even when there is exactly ONE card; confirm
        that one with the user first. Only after the user picks do you `select-method` that row.
      – IF THE PICKER IS EMPTY (no cards): you MUST ask the user to add one before going further —
        there is no valid "just charge the default" path. Ask whether they want a UnionPay card or a
        Visa/Mastercard, then send `add-method` and submit its form (REQUIRES `user_email`; `brand`
        picks the RAIL, not the card brand: `unionpay` → UPI enrollment; omitted → the unified
        card-entry page, which detects Visa vs Mastercard from the BIN itself). Omitting `brand`
        does NOT mean UnionPay — pass `unionpay` explicitly for that. Never pick the rail for the user.
  • Routing follows the card the USER picked in the start_payment picker, by its payment_brand:
    a UnionPay card mints a token via a passkey (open_url → poll to ACTIVE) → payment_token_id;
    any other brand (evo / visa) is returned directly as payment_method_id.
  • STANDALONE TOKEN CREATION shares the same minting: the ``create-token`` scenario (start it with
    book("create a network token …")) mints a reusable network token on ALL THREE rails — UnionPay
    (passkey), Visa (FIDO passkey), and EVO/Mastercard (synchronous, no browser); start_token_creation
    drives this same mint for the main path with an order_id bound. So do NOT tell the user network
    tokens are "UnionPay-only", and do NOT steer them to add a UnionPay card just to mint one — any
    listed card (including an EVO Mastercard) can be minted. The "only UnionPay mints, EVO/Visa
    handed back" rule applies ONLY to paying an order via the start_payment picker.
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
  • When you have the resolved credential, attach it to the payload of the money-settling action
    (most domains: the booking `confirm`; HOTEL: `pay` on hotel.order-detail — its booking-confirm
    only locks). That action's `carries` names the field (payment_token_id for the token main path;
    payment_method_id ONLY for the explicit/fallback EVO rail). Do NOT omit it to let the platform
    charge a default — the settling action must always carry the credential resolved above.

TRIP AGGREGATE PAYMENT — settle a whole cart of locked orders in ONE charge (trip checkout)
  When a session locked SEVERAL orders (e.g. a multi-room hotel trip, or a flight+hotel trip), the
  flow ends at a CART SUMMARY card listing every locked order plus a total, a currency, and a
  PAYMENT-GROUP ID (a `pg_…` value — read it from the card's data). You settle the ENTIRE cart with
  ONE payment against that payment group — NOT one payment per order. Two ways to pay; (as always)
  the USER chooses, and the money-confirm card ALWAYS requires the user's explicit confirm — never
  auto-confirm.
  • NETWORK-TOKEN (the DEFAULT — same precedence as a single order: mint unless the user explicitly
    chose to charge an EVO card directly). Bind ONE aggregate token to the whole GROUP:
      1) From the cart-summary card read the payment-group id (pg_…) and the cart total + currency.
      2) In a SEPARATE payment session mint the aggregate token bound to the GROUP:
         start_token_creation(amount_cents=<cart TOTAL × 100 — the whole cart, not a single order>,
         order_id=<the pg_… payment-group id>, member_id=<the SAME member_id as the trip>). Pick a
         bound card when the create-token picker lists them (Mastercard/EVO mints SYNCHRONOUSLY, no
         passkey; UnionPay/Visa use a passkey → poll to ACTIVE). This yields ONE payment_token_id
         whose frozen amount = the cart total and whose external_transaction_id = the payment-group id.
      3) Back in the TRIP session, send the cart-summary's checkout action CARRYING that
         payment_token_id (the action's `carries` names the field). Supplying the token here goes
         straight to the money-confirm card (skipping card selection).
      4) Surface the confirm card's total to the USER; only after they approve, send its confirm
         action carrying the same payment_token_id. That settles the whole cart in one charge against
         the group and polls to a terminal result card.
  • EVO CARD (only when the user explicitly chose to charge a bound EVO card directly instead of a
    network token). Send the cart-summary checkout action with NO token → a method picker lists the
    member's cards → the USER picks one → the confirm card's confirm carries payment_method_id +
    evo_explicit=true → settles via one batch-level 3DS.
  • The trip session and the token-mint payment session MUST share the same member_id, else the token
    and the group belong to different users and the charge is refused. If a settle is refused for a
    token/order mismatch, the token wasn't bound to THIS pg_… — re-mint with order_id=<pg_…> and retry.

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
