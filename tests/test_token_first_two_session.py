"""任务 7.4：MCP 桥「两会话 / 同 member / 先锁单后创建网络令牌」示例测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``book`` 与 ``start_token_creation`` 两个 MCP
工具，以及它们对 A2A bridge 的调用契约。

覆盖需求：
  - R5.1 创建网络令牌走独立于 Booking_Session 的 Payment_Session（``_new_session("payment")``，context 隔离）。
  - R5.2 Payment_Session 与其 Booking_Session 使用相同的 member_id（传入 → ``set_member`` 同值调用）。
  - R5.3 创建网络令牌子流程仅用结构化卡片动作推进（``send_action`` 提交 ``payment.token-setup#submit``），
         不发自由文本 ``send_message``（唯一固定分类种子 "Create a network token." 除外）。
  - R6.1/R6.2/R6.3 集成示例：先锁单取得 order_id 与权威金额，再以该金额 + order_id 创建网络令牌；
         提交 payload 的 ``amount_cents`` == 订单权威金额、``external_transaction_id`` == order_id。

设计参照：design.md 第 8 节（MCP_Bridge）与 requirements R5/R6。

说明：子仓当前未安装 pytest / pytest-asyncio，也无既有测试栈。为「实际可运行且通过」，本测试仅依赖
标准库 ``unittest.IsolatedAsyncioTestCase``（原生支持 async 测试方法）。同一文件也可被 pytest 直接收集。
未改动任何被测实现——仅以一个假的 A2A bridge 替换模块级 ``server._bridge`` 记录其被调用的契约。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from collections import deque
from typing import Any

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身：假 A2A bridge —— 记录 send_text / send_action / set_member 的调用契约。
# 只实现被测工具会用到的接口（member_id / set_member / send_text / send_action）。
# ─────────────────────────────────────────────────────────────────────────────
def _task_json(cards: list[dict[str, Any]] | None = None, state: str = "input-required") -> str:
    """构造一条最小可解析的 A2A 阻塞式 JSON-RPC 响应（final_task/normalize_task 可解析）。"""
    parts = [{"kind": "data", "data": c} for c in (cards or [])]
    history = [{"role": "agent", "parts": parts}] if parts else []
    task = {"kind": "task", "status": {"state": state}, "history": history}
    return json.dumps({"jsonrpc": "2.0", "id": "test", "result": task})


def _order_card(order_id: str, amount_minor: int, currency: str) -> dict[str, Any]:
    """模拟锁单后返回的订单详情卡（携带权威 order_id / 金额 / 币种）。"""
    return {
        "component": "hotel.order-detail",
        "kind": "detail",
        "data": {
            "order_id": order_id,
            "amount_minor": amount_minor,
            "price_currency": currency,
        },
        "actions": [{"id": "pay", "carries": ["payment_token_id", "order_id"]}],
    }


class FakeBridge:
    """记录被测工具对 A2A bridge 的调用；不做任何网络交互。"""

    def __init__(self, member_id: str = "m-config-default") -> None:
        self._member_id = member_id
        self.text_calls: list[tuple[str, str]] = []          # (context_id, text)
        self.action_calls: list[tuple[str, str, str, dict]] = []  # (context_id, component, action, payload)
        self.set_member_calls: list[str] = []
        self._text_responses: deque[tuple[int, str]] = deque()
        self._action_responses: deque[tuple[int, str]] = deque()

    # server._result() 读取 bridge.member_id 填充返回值
    @property
    def member_id(self) -> str:
        return self._member_id

    async def set_member(self, member_id: str) -> None:
        member_id = (member_id or "").strip()
        self.set_member_calls.append(member_id)
        if member_id:
            self._member_id = member_id

    def queue_text_response(self, status: int, raw: str) -> None:
        self._text_responses.append((status, raw))

    def queue_action_response(self, status: int, raw: str) -> None:
        self._action_responses.append((status, raw))

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        self.text_calls.append((context_id, text))
        if self._text_responses:
            return self._text_responses.popleft()
        return 200, _task_json()

    async def send_action(
        self, context_id: str, component: str, action: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        # 复制 payload，避免调用方后续复用/变更影响断言
        self.action_calls.append((context_id, component, action, dict(payload or {})))
        if self._action_responses:
            return self._action_responses.popleft()
        return 200, _task_json()


class TokenFirstTwoSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 隔离并替换模块级全局状态，测试后恢复。
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = FakeBridge(member_id="m-config-default")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    # ── R5.1：创建网络令牌开独立 payment 会话，与 booking 会话 context 隔离 ────────────────
    async def test_mint_opens_independent_payment_session(self) -> None:
        booking = await server.book("book a hotel near the Bund", member_id="m-shared")
        booking_sid = booking["session_id"]
        token = await server.start_token_creation(
            amount_cents=42800, order_id="hho_ISO1", member_id="m-shared"
        )
        token_sid = token["session_id"]

        # 两个会话是不同的、独立的 A2A context。
        self.assertNotEqual(booking_sid, token_sid)
        self.assertEqual(server._sessions[booking_sid]["kind"], "booking")
        self.assertEqual(server._sessions[token_sid]["kind"], "payment")
        # 一个 context 一份 session：context_id == session_id（相互隔离）。
        self.assertEqual(server._sessions[token_sid]["context_id"], token_sid)
        self.assertEqual(server._sessions[booking_sid]["context_id"], booking_sid)

        # booking 的交互只落在 booking context；payment 的交互只落在 payment context。
        booking_ctxs = {c[0] for c in self.fake.text_calls if c[1] != "Create a network token."}
        self.assertEqual(booking_ctxs, {booking_sid})
        payment_text_ctxs = {c[0] for c in self.fake.text_calls if c[1] == "Create a network token."}
        payment_action_ctxs = {c[0] for c in self.fake.action_calls}
        self.assertEqual(payment_text_ctxs, {token_sid})
        self.assertEqual(payment_action_ctxs, {token_sid})

    # ── R5.2：两会话使用同一 member_id ─────────────────────────────────────────────
    async def test_both_sessions_share_member_id(self) -> None:
        booking = await server.book("book a hotel", member_id="m-shared")
        token = await server.start_token_creation(
            amount_cents=42800, order_id="hho_MEM1", member_id="m-shared"
        )

        # 传入 member_id → set_member 被以同值调用（booking 一次、payment 一次）。
        self.assertEqual(self.fake.set_member_calls, ["m-shared", "m-shared"])
        # 两个会话返回的归属 member 一致。
        self.assertEqual(booking["member_id"], "m-shared")
        self.assertEqual(token["member_id"], "m-shared")

    # ── R5.3：创建网络令牌子流程仅用结构化动作，不发自由文本（固定分类种子除外）───────────────
    async def test_mint_uses_structured_actions_only(self) -> None:
        token = await server.start_token_creation(
            amount_cents=42800, order_id="hho_ACT1", member_id="m-shared"
        )
        token_sid = token["session_id"]

        text_calls = [t for t in self.fake.text_calls if t[0] == token_sid]
        action_calls = [a for a in self.fake.action_calls if a[0] == token_sid]

        # payment 会话内唯一的自由文本就是固定分类种子，别无其他 send_message。
        self.assertEqual(len(text_calls), 1)
        self.assertEqual(text_calls[0][1], "Create a network token.")
        # 推进创建网络令牌用的是结构化卡片动作 payment.token-setup#submit。
        submits = [a for a in action_calls if a[1] == "payment.token-setup" and a[2] == "submit"]
        self.assertEqual(len(submits), 1)

    # ── R6.1/R6.2/R6.3：集成示例——先锁单取得 order_id 与权威金额，再创建网络令牌并绑定 ─────────
    async def test_lock_first_then_mint_binds_amount_and_order(self) -> None:
        order_id = "hho_TESTORDER123"
        authoritative_minor = 42800  # 订单权威金额（最小币种单位）

        # ① 先锁单：booking 会话返回携带权威 order_id / 金额 / 币种的订单卡。
        self.fake.queue_text_response(
            200, _task_json(cards=[_order_card(order_id, authoritative_minor, "USD")])
        )
        booking = await server.book(
            "Lock a hotel order near the Bund, 1 adult, check-in Aug 18, check-out Aug 19. "
            "Guest Richard Chen, phone 13275666789.",
            member_id="m-shared",
        )
        booking_sid = booking["session_id"]

        # 从锁单返回的卡片读取权威 order_id 与权威金额（客户端据此驱动后续创建网络令牌）。
        order_card = booking["cards"][-1]
        got_order_id = order_card["data"]["order_id"]
        got_authoritative_minor = order_card["data"]["amount_minor"]
        self.assertEqual(got_order_id, order_id)
        self.assertEqual(got_authoritative_minor, authoritative_minor)

        # ② 再创建网络令牌：以订单权威金额 + order_id，在独立 payment 会话中创建。
        token = await server.start_token_creation(
            amount_cents=got_authoritative_minor,
            order_id=got_order_id,
            member_id="m-shared",
        )
        token_sid = token["session_id"]

        # 会话隔离 + 同 member。
        self.assertNotEqual(booking_sid, token_sid)
        self.assertEqual(booking["member_id"], token["member_id"])

        # 提交 create-token 入口卡的 payload：令牌权威金额 == 订单权威金额、
        # external_transaction_id == order_id。
        submits = [
            a for a in self.fake.action_calls
            if a[0] == token_sid and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        payload = submits[0][3]
        self.assertEqual(payload["amount_cents"], authoritative_minor)
        self.assertEqual(payload["external_transaction_id"], order_id)

    # ── 补充守卫：无 order_id 的独立创建网络令牌不带 external_transaction_id ─────────────────
    async def test_orderless_mint_omits_binding(self) -> None:
        token = await server.start_token_creation(amount_cents=1000, member_id="m-shared")
        token_sid = token["session_id"]
        submits = [
            a for a in self.fake.action_calls
            if a[0] == token_sid and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        payload = submits[0][3]
        self.assertNotIn("external_transaction_id", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
