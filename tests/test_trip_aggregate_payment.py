"""行程聚合支付（TRIP AGGREGATE）：一车多单一次扣款的桥层契约与指引文案测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``start_token_creation`` 与 ``guide``。
一次会话锁下多张订单（多房型行程、机票+酒店行程）时，流程收口在一张**购物车汇总卡**上，
卡里带一个**支付组 id**（``pg_…``）。整车只发生**一次**扣款，且这次扣款绑定的是支付组、
不是其中任何一张子订单。

本文件锁三类不变量：
  - **绑定行为**：``start_token_creation(order_id=<pg_…>)`` 把支付组 id **逐字符**透传为
    ``external_transaction_id``，与单订单绑定走同一条码路；``amount_cents`` 原样透传为
    整车总额（不是任何单张子订单的金额）。
  - **会话模型**：聚合令牌在**独立** Payment_Session 内创建（与行程会话 context 隔离），
    两会话使用**同一** ``member_id``——否则令牌与支付组归属不同用户，扣款会被拒。
  - **文案契约**：``start_token_creation`` 的 docstring 与 ``guide()`` 都把「绑支付组而非
    单订单」「金额取整车总额」「一次扣款而非逐单扣款」「money-confirm 必须用户显式确认」
    写明；``guide()`` 另含 payment / add-method 子流程**禁发自由文本**的例外条款。

与姊妹文件的去重边界：``test_token_first_two_session.py`` / ``test_ride_two_session_mint.py``
锁的是**单订单**绑定（``hho_…`` / ``rio_…``）与两会话模型的通用形状；本文件只补聚合支付
特有的维度（支付组 id 作绑定值、整车总额、汇总卡→一次扣款的指引文案），不复述那些断言。

未改动任何被测实现——仅以假 bridge 替换模块级 ``server._bridge``。
"""

from __future__ import annotations

import os
import sys

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包
# 以及同目录的姊妹测试模块（复用其 FakeBridge 测试替身）。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import unittest  # noqa: E402

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402

# 复用姊妹测试文件里的假 A2A bridge（不重复造轮子）。
from test_token_first_two_session import FakeBridge  # noqa: E402

# 代表性的支付组 id（购物车汇总卡的 data 里给出的 ``pg_…`` 值）。
_PAYMENT_GROUP_ID = "pg_01M3TRIPCART7QK2"
# 整车总额（最小币种单位）：两张子订单 29600 + 18400 之和 —— 刻意与任一子订单金额不等，
# 这样「传的是整车总额、不是单张子订单金额」这件事在断言里是可判别的。
_SUB_ORDER_MINORS = (29600, 18400)
_CART_TOTAL_MINOR = sum(_SUB_ORDER_MINORS)


class TripAggregatePaymentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 隔离并替换模块级全局状态，测试后恢复。
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = FakeBridge(member_id="m-trip")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    # ── 绑定行为：支付组 id 逐字符作绑定值，金额取整车总额 ────────────────────────────────
    async def test_aggregate_token_binds_payment_group_id_verbatim(self) -> None:
        """``order_id=<pg_…>`` → 入口卡 submit 的 ``external_transaction_id`` 逐字符等于该支付组 id。

        桥层对绑定值不做任何改写或截断：支付组 id 与单订单号走同一条码路，区别只在调用方
        传的是 ``pg_…`` 还是 ``hho_…``。金额同样原样透传为整车总额。
        """
        token = await server.start_token_creation(
            amount_cents=_CART_TOTAL_MINOR,
            order_id=_PAYMENT_GROUP_ID,
            member_id="m-trip",
        )
        token_sid = token["session_id"]

        submits = [
            a for a in self.fake.action_calls
            if a[0] == token_sid and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        payload = submits[0][3]

        # 逐字符透传，不改写、不截断。
        self.assertEqual(payload["external_transaction_id"], _PAYMENT_GROUP_ID)
        # 金额是整车总额，且确实不等于任何单张子订单的金额。
        self.assertEqual(payload["amount_cents"], _CART_TOTAL_MINOR)
        for sub_minor in _SUB_ORDER_MINORS:
            self.assertNotEqual(payload["amount_cents"], sub_minor)

    # ── 会话模型：独立 Payment_Session + 与行程会话同 member ──────────────────────────
    async def test_aggregate_mint_is_isolated_and_shares_trip_member(self) -> None:
        """聚合令牌在独立 Payment_Session 内创建，且与行程会话使用同一 member_id。

        两者 member 不一致时，令牌与支付组会归属不同用户、扣款必被拒（``guide()`` 里也写明了
        这条），所以这里同时守卫 context 隔离与 member 一致。
        """
        trip = await server.book(
            "book a flight and a hotel for my Shanghai trip", member_id="m-trip"
        )
        trip_sid = trip["session_id"]
        token = await server.start_token_creation(
            amount_cents=_CART_TOTAL_MINOR,
            order_id=_PAYMENT_GROUP_ID,
            member_id="m-trip",
        )
        token_sid = token["session_id"]

        # 两个独立 A2A context：行程会话是 booking，铸令牌会话是 payment。
        self.assertNotEqual(trip_sid, token_sid)
        self.assertEqual(server._sessions[trip_sid]["kind"], "booking")
        self.assertEqual(server._sessions[token_sid]["kind"], "payment")
        self.assertEqual(server._sessions[token_sid]["context_id"], token_sid)

        # 同一 member：两次 set_member 都是同值，两会话返回的归属也一致。
        self.assertEqual(self.fake.set_member_calls, ["m-trip", "m-trip"])
        self.assertEqual(trip["member_id"], "m-trip")
        self.assertEqual(token["member_id"], "m-trip")

    # ── 文案契约：start_token_creation 的 docstring 写明支付组绑定 ──────────────────────
    def test_start_token_creation_docstring_documents_group_binding(self) -> None:
        """``start_token_creation`` 的 docstring 必须写明聚合场景下 ``order_id`` 传支付组 id、
        ``amount_cents`` 传整车总额（而非任一子订单金额），以及 EVO/Mastercard 同步创建。

        这份 docstring 就是 MCP 工具描述，调用方（agent）只能看到它 —— 说明缺失等于让调用方
        按单订单的直觉去传参，把聚合令牌绑到其中一张子订单上。
        """
        doc = server.start_token_creation.__doc__ or ""

        self.assertIn("TRIP AGGREGATE", doc)
        # 绑的是支付组，不是单张订单。
        self.assertIn("bind to the PAYMENT GROUP, not a single order", doc)
        self.assertIn("pg_", doc)
        # 金额是整车总额，明确排除单张子订单金额。
        self.assertIn("AGGREGATE TOTAL", doc)
        self.assertIn("NOT any single sub-order", doc)
        # 绑定值经 external_transaction_id 落到令牌上。
        self.assertIn("external_transaction_id", doc)
        # Mastercard/EVO 这条轨同步创建、无 passkey 环节。
        self.assertIn("SYNCHRONOUSLY", doc)

    # ── 文案契约：guide() 的 TRIP AGGREGATE PAYMENT 段 ───────────────────────────────
    async def test_guide_documents_trip_aggregate_payment(self) -> None:
        """``guide()`` 必须把聚合支付写成「一次扣款打在支付组上」，并含四条要点：
        汇总卡给出支付组 id、整车一次扣款而非逐单扣款、两会话同 member、金额确认须用户显式同意。
        """
        text = await server.guide()

        self.assertIn("TRIP AGGREGATE PAYMENT", text)
        # 汇总卡 + 支付组 id 是这条路径的入口信息。
        self.assertIn("CART SUMMARY", text)
        self.assertIn("PAYMENT-GROUP ID", text)
        # 一次扣款打在支付组上，而不是每张订单各付一次。
        self.assertIn("ONE payment against that payment group", text)
        self.assertIn("NOT one payment per order", text)
        # 默认仍是网络令牌轨（与单订单同一优先级）。
        self.assertIn("NETWORK-TOKEN (the DEFAULT", text)
        # 金额确认卡永远要用户显式同意，不得自动确认。
        self.assertIn("never", text.lower())
        self.assertIn("auto-confirm", text)
        # 两会话同 member，否则扣款被拒；绑错组时重铸重试。
        self.assertIn("MUST share the same member_id", text)
        self.assertIn("re-mint with order_id=", text)

    # ── 文案契约：支付 / 加卡子流程禁发自由文本 ────────────────────────────────────────
    async def test_guide_forbids_free_text_in_payment_subflows(self) -> None:
        """``guide()`` 必须把「payment / add-method 子流程内绝不发自由文本」写成
        ANSWERING THE SERVER 的例外条款，并给出 checkout 中途加卡的正确做法。

        这条是实测踩出来的：支付子流程里发自由文本会被误路由到酒店搜索，流程原地断掉。
        """
        text = await server.guide()

        # 例外条款本身：明确它是上一条「有 text 无卡就 send_message」规则的唯一例外。
        self.assertIn("EXCEPTION", text)
        self.assertIn("NEVER send_message", text)
        self.assertIn("misrouted", text)
        self.assertIn("the rule above does NOT apply", text)
        # 正确做法：用结构化动作重新驱动最后一张卡。
        self.assertIn("Re-drive with a structured act(...)", text)
        # checkout 中途加卡不被支持 → 另开独立支付会话绑卡，再回来重开选卡器。
        self.assertIn("SEPARATE standalone payment", text)
        self.assertIn("re-open the", text)
        self.assertIn("only offer `select-method`", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
