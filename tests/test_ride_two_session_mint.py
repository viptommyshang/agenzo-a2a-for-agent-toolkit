"""任务 9.3：MCP 桥「ride 两会话 / 同 member / 先 create-order 后创建网络令牌 / order_ref 逐字符透传」示例测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``book`` 与 ``start_token_creation`` 两个 MCP
工具，验证在 **ride（打车）** 域下 Network_Token 直扣主路径的桥层契约（create-order → mint → pay）。

覆盖需求（ride-network-token-direct-charge）：
  - R3.2 客户端为某 ride 订单create Network_Token 时，the created network token的 ``external_transaction_id`` 读回值 ==
         订单 ``order_ref``。桥层体现为 ``start_token_creation(order_id=<order_ref>)`` 把
         ``external_transaction_id`` 放入 ``payment.token-setup#submit`` 的 payload。
  - R3.3 创建网络令牌的权威金额（以最小币种单位比对）== 订单 ``Order_Authoritative_Amount``（``amount_minor``）。
  - R8.4 MCP_Bridge 为 ride 订单创建网络令牌时，把等于 ``order_ref`` 的值 **逐字符** 透传为
         ``external_transaction_id``，不作任何改写或截断。
  - 会话模型（R8.5 主路径）：ride 创建网络令牌开 **独立** Payment_Session（与 Booking_Session context 隔离）、
         两会话使用 **相同** ``member_id``、创建网络令牌子流程 **仅用结构化卡片动作** 推进；且顺序为
         **先 create-order 取得 ``order_ref`` 与权威金额，之后再创建网络令牌**。

设计参照：design.md 方案 A（两步锁单）、``a2a-cards-ride.md`` §3/§3b（``ride.order-awaiting`` 待支付
锁单订单卡 + ``pay`` 动作）、requirements R3 / R8。

说明：复用 ``test_token_first_two_session`` 中已被验证过的 ``FakeBridge`` / ``_task_json`` 测试替身
（假 A2A bridge，仅记录调用契约、不做任何网络交互），仅新增 ride 专属的「待支付锁单订单卡」
（``ride.order-awaiting``，携带 ``rio_…`` 的 ``order_ref`` 与权威金额/币种）。为「实际可运行且通过」，
本测试只依赖标准库 ``unittest.IsolatedAsyncioTestCase``，可被 ``unittest`` 或 ``pytest`` 直接收集；
未改动任何被测实现——仅以假 bridge 替换模块级 ``server._bridge`` 记录其被调用的契约。
"""

from __future__ import annotations

import os
import sys
from typing import Any

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包
# 以及同目录的姊妹测试模块（复用其 FakeBridge / _task_json 测试替身）。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import unittest  # noqa: E402

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402

# 复用姊妹测试文件里的假 A2A bridge 与最小 A2A 响应构造器（不重复造轮子）。
from test_token_first_two_session import FakeBridge, _task_json  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身扩展：在 FakeBridge 之上记录一条「统一事件时间线」，用于断言跨会话调用顺序
# （先 create-order 取 order_ref，之后再创建网络令牌）。仅追加事件、行为与 FakeBridge 一致。
# ─────────────────────────────────────────────────────────────────────────────
class RideRecordingBridge(FakeBridge):
    """在 FakeBridge 基础上记录 text/action 的统一先后顺序（events）。"""

    def __init__(self, member_id: str = "m-config-default") -> None:
        super().__init__(member_id)
        # 每个元素形如 ("text", context_id, text) 或 ("action", context_id, "component#action")
        self.events: list[tuple[str, str, str]] = []

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        self.events.append(("text", context_id, text))
        return await super().send_text(context_id, text)

    async def send_action(
        self, context_id: str, component: str, action: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        self.events.append(("action", context_id, f"{component}#{action}"))
        return await super().send_action(context_id, component, action, payload)


# ride 的「create-order 锁单请求」自然语言（book 用的分类种子；内容不影响桩，只需可辨识）。
RIDE_BOOK_REQUEST = (
    "Lock a ride from The Bund to Shanghai Pudong Airport now, 1 passenger. "
    "Passenger Richard Chen, phone 13275666789."
)


def _ride_order_awaiting_card(
    order_ref: str, price_amount: float, amount_minor: int, currency: str
) -> dict[str, Any]:
    """模拟 ``ride.payment-confirm#confirm``（create-order 锁单）成功后返回的 ``ride.order-awaiting``
    待支付订单卡：携带权威 ``order_ref``（``rio_…``）、权威金额（十进制展示价 + 最小币种单位）与币种，
    并挂 ``pay`` 动作（carries: order_ref + payment_token_id | payment_method_id）。字段对齐
    ``a2a-cards-ride.md`` §3b。"""
    return {
        "component": "ride.order-awaiting",
        "kind": "detail",
        "data": {
            "orderRef": order_ref,
            "order_ref": order_ref,
            "status": "AWAITING_PAYMENT",
            "paymentStatus": "PENDING",
            "priceAmount": price_amount,          # 十进制货币单位（42.80 = $42.80）
            "amount_minor": amount_minor,         # 权威 Order_Authoritative_Amount（最小币种单位）
            "currency": currency,
            "quoteId": "qt_ride_demo",
            "vehicleClass": "comfort",
        },
        "actions": [
            {
                "id": "pay",
                "carries": ["order_ref", "payment_token_id", "payment_method_id"],
            }
        ],
    }


class RideTwoSessionMintTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 隔离并替换模块级全局状态，测试后恢复。
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = RideRecordingBridge(member_id="m-config-default")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    # ── 会话模型：ride 创建网络令牌开独立 payment 会话，与 booking 会话 context 隔离 ─────────────
    async def test_ride_mint_opens_independent_payment_session(self) -> None:
        booking = await server.book(RIDE_BOOK_REQUEST, member_id="m-ride")
        booking_sid = booking["session_id"]
        token = await server.start_token_creation(
            amount_cents=4280, order_id="rio_ISO1", member_id="m-ride"
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

    # ── 会话模型：两会话使用同一 member_id ──────────────────────────────────────────
    async def test_ride_both_sessions_share_member_id(self) -> None:
        booking = await server.book(RIDE_BOOK_REQUEST, member_id="m-ride")
        token = await server.start_token_creation(
            amount_cents=4280, order_id="rio_MEM1", member_id="m-ride"
        )

        # 传入 member_id → set_member 被以同值调用（booking 一次、payment 一次）。
        self.assertEqual(self.fake.set_member_calls, ["m-ride", "m-ride"])
        # 两个会话返回的归属 member 一致。
        self.assertEqual(booking["member_id"], "m-ride")
        self.assertEqual(token["member_id"], "m-ride")

    # ── 会话模型：创建网络令牌子流程仅用结构化动作，不发自由文本（固定分类种子除外）─────────────────
    async def test_ride_mint_uses_structured_actions_only(self) -> None:
        token = await server.start_token_creation(
            amount_cents=4280, order_id="rio_ACT1", member_id="m-ride"
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

    # ── 核心：先 create-order 取 order_ref 与权威金额，再创建网络令牌并逐字符绑定 ─────────────────
    #    覆盖 R3.2（external_transaction_id == order_ref）、R3.3（权威金额一致）、R8.4（逐字符透传）。
    async def test_ride_create_order_first_then_mint_binds_orderref_and_amount(self) -> None:
        order_ref = "rio_TESTORDER123"           # 平台权威订单号（rio_…）
        price_amount = 42.80                     # 展示价（十进制货币单位）
        authoritative_minor = 4280               # Order_Authoritative_Amount（最小币种单位）

        # 展示价与权威最小币种单位金额自洽（十进制 × 100 == minor）。
        self.assertEqual(round(price_amount * 100), authoritative_minor)

        # ① 先 create-order 锁单：booking 会话返回携带权威 order_ref / 金额 / 币种的待支付订单卡。
        self.fake.queue_text_response(
            200,
            _task_json(
                cards=[_ride_order_awaiting_card(order_ref, price_amount, authoritative_minor, "USD")]
            ),
        )
        booking = await server.book(RIDE_BOOK_REQUEST, member_id="m-ride")
        booking_sid = booking["session_id"]

        # 从锁单返回的订单卡读取权威 order_ref（rio_…）与权威金额（客户端据此驱动后续创建网络令牌）。
        order_card = booking["cards"][-1]
        self.assertEqual(order_card["component"], "ride.order-awaiting")
        self.assertEqual(order_card["data"]["status"], "AWAITING_PAYMENT")
        got_order_ref = order_card["data"]["orderRef"]
        got_authoritative_minor = order_card["data"]["amount_minor"]
        self.assertTrue(got_order_ref.startswith("rio_"))
        self.assertEqual(got_order_ref, order_ref)
        self.assertEqual(got_authoritative_minor, authoritative_minor)
        # pay 动作以 order_ref 为目标订单号（创建网络令牌绑定的目标值）。
        pay_action = next(a for a in order_card["actions"] if a["id"] == "pay")
        self.assertIn("order_ref", pay_action["carries"])

        # ② 再创建网络令牌：以订单权威金额 + order_ref，在独立 payment 会话中创建。
        token = await server.start_token_creation(
            amount_cents=got_authoritative_minor,
            order_id=got_order_ref,
            member_id="m-ride",
        )
        token_sid = token["session_id"]

        # 会话隔离 + 同 member。
        self.assertNotEqual(booking_sid, token_sid)
        self.assertEqual(booking["member_id"], token["member_id"])

        # 提交 create-token 入口卡的 payload：
        #   - 令牌权威金额 == 订单权威金额（R3.3，最小币种单位精确相等）；
        #   - external_transaction_id == order_ref（R3.2 / R8.4，逐字符透传、未改写/截断）。
        submits = [
            a for a in self.fake.action_calls
            if a[0] == token_sid and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        payload = submits[0][3]
        self.assertEqual(payload["amount_cents"], authoritative_minor)
        self.assertEqual(payload["external_transaction_id"], order_ref)
        # 逐字符透传守卫：既不截断也不改写（长度与首尾都保持一致）。
        self.assertEqual(len(payload["external_transaction_id"]), len(order_ref))
        self.assertTrue(payload["external_transaction_id"].startswith("rio_"))

        # ③ 顺序守卫：create-order（booking 会话取 order_ref）发生在创建网络令牌（payment submit）之前。
        booking_text_idx = next(
            i for i, e in enumerate(self.fake.events)
            if e[0] == "text" and e[1] == booking_sid
        )
        submit_idx = next(
            i for i, e in enumerate(self.fake.events)
            if e[0] == "action" and e[1] == token_sid and e[2] == "payment.token-setup#submit"
        )
        self.assertLess(booking_text_idx, submit_idx)

    # ── 补充守卫：无 order_id 的独立创建网络令牌不带 external_transaction_id（不绑定任何订单）───────
    async def test_ride_orderless_mint_omits_binding(self) -> None:
        token = await server.start_token_creation(amount_cents=4280, member_id="m-ride")
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
