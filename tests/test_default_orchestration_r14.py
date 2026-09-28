"""任务 11.6（bridge 层）：R14「MCP 桥默认编排令牌现结主路径（软引导）」示例/集成测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 MCP 桥软引导（方案 A）——即 ``act`` /
``start_token_creation`` / ``guide`` 在货币结算动作与创建网络令牌子流程上承接的默认编排三段式主路径。

本文件聚焦 **R14 的默认编排三段式主路径「顺序 + 不变量」** 以及 ``guide()`` 文案：
  - R14.1 未显式选 EVO 时，默认编排三段式主路径——**顺序**：锁单（create-order）→ 创建网络令牌
    （独立 Payment_Session）→ 以 ``payment_token_id`` 直扣；**不变量**：令牌 ``external_transaction_id``
    == order_id、令牌权威金额 == 订单权威金额（Order_Authoritative_Amount）、两会话使用同一 member_id。
  - R14.2 显式提供 ``payment_method_id`` → 走 EVO、**不创建网络令牌**（以「默认↔显式」决策边界的角度断言，
    并守卫 EVO 分流不叠加创建网络令牌软引导）。
  - R14.3 令牌主路径失败 → 返回可回退 EVO 引导，且**绝不静默改用 member/平台/开发者默认卡**（以
    「失败短路成功态软引导 + 禁默认卡」的角度断言）。
  - R14.4 / R12.3 ``guide()`` 把默认编排顺序表述为规范主路径，并含三条约束文案（先锁单后创建网络令牌、
    两会话同 member、strict 订单绑定）。

> 与任务 11.7（``tests/test_evo_narrowing_r9.py``）的**去重边界**：
>   - 11.7 聚焦 **R9「EVO 收窄」的分支语义**——逐条断言三个软引导键（``default_orchestration`` /
>     ``evo_orchestration`` / ``token_path_failure``）在各单点 ``act`` / ``start_token_creation``
>     调用下的**出现/缺席**与失败**环节标签**（LOCKING / MINTING / DIRECT CHARGE）及回退文案。
>   - 本文件（11.6）**不**逐条复述那些分支/环节标签断言，而是补齐 11.7 不覆盖的维度：
>       ① **端到端三段式集成**——用统一事件时间线断言「锁单 → 创建网络令牌 → 直扣」的**跨会话先后顺序**，
>          且三段式的四条不变量（顺序、``external_transaction_id`` == order_id、金额相等、同 member）；
>       ② **默认↔显式 EVO 的决策边界**（同一结算动作，payload 有/无 ``payment_method_id`` → 软引导键
>          互斥翻转，且仅默认分支叠加三段式创建网络令牌 nudge）；
>       ③ **失败短路**——失败态优先，短路掉成功态软引导（``payment_warning`` 等）且禁默认卡；
>       ④ **``guide()`` 文案**（11.7 完全未触及 ``guide()``）。

复用姊妹测试 ``test_token_first_two_session`` 的假 A2A bridge（``FakeBridge`` / ``_task_json`` /
``_order_card``）；仅依赖标准库 ``unittest.IsolatedAsyncioTestCase``，可被 ``unittest`` 或 ``pytest``
直接收集。未改动任何被测实现——仅以假 bridge 替换模块级 ``server._bridge``。
"""

from __future__ import annotations

import os
import sys
from typing import Any

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包
# 以及同目录的姊妹测试模块（复用其 FakeBridge / _task_json / _order_card 测试替身）。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import unittest  # noqa: E402

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402

# 复用姊妹测试文件里的假 A2A bridge、最小 A2A 响应构造器与 hotel 订单卡构造器（不重复造轮子）。
from test_token_first_two_session import FakeBridge, _order_card, _task_json  # noqa: E402


# 代表性的 hotel「锁后付」结算动作坐标（component, action）——满足 server._is_settle_action。
_HOTEL_PAY = ("hotel.order-detail", "pay")


class _RecordingBridge(FakeBridge):
    """在 FakeBridge 之上记录一条「统一事件时间线」，用于断言跨会话调用先后顺序。

    每个事件形如 ``(kind, context_id, label, payload)``：
      - text:   ``("text",   ctx, 原始文本, None)``
      - action: ``("action", ctx, "component#action", payload 副本)``
    仅追加事件，转发行为与父类 FakeBridge 完全一致。"""

    def __init__(self, member_id: str = "m-config-default") -> None:
        super().__init__(member_id)
        self.events: list[tuple[str, str, str, dict[str, Any] | None]] = []

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        self.events.append(("text", context_id, text, None))
        return await super().send_text(context_id, text)

    async def send_action(
        self, context_id: str, component: str, action: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        self.events.append(("action", context_id, f"{component}#{action}", dict(payload or {})))
        return await super().send_action(context_id, component, action, payload)


class DefaultOrchestrationR14Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 隔离并替换模块级全局状态，测试后恢复。
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = _RecordingBridge(member_id="m-config-default")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    # ── R14.1：默认编排三段式主路径的「顺序 + 四条不变量」端到端集成 ─────────────────────────
    async def test_three_stage_main_path_order_and_invariants(self) -> None:
        """完整走一遍默认主路径：锁单（book/create-order）→ 创建网络令牌（独立会话）→ 以 payment_token_id
        直扣，断言 R14.1 的四条不变量与三段式**先后顺序**。

        断言：
          - 顺序：锁单（Booking_Session 首个交互）< 创建网络令牌（payment.token-setup#submit）< 直扣
            （携 payment_token_id 的 pay 动作）；
          - 令牌 external_transaction_id == order_id；
          - 令牌权威金额（amount_cents，最小币种单位）== 订单权威金额（amount_minor）；
          - 两会话使用同一 member_id，且创建网络令牌在与 Booking_Session 隔离的独立 Payment_Session 内进行；
          - 最终直扣（stage 3，携 payment_token_id）成功时不叠加任何成功态软引导。

        Validates: Requirements 14.1, 6.2, 6.3
        """
        member = "m-shared"
        order_id = "hho_R14MAINPATH"
        authoritative_minor = 42800  # 订单权威金额（最小币种单位）

        # ① 锁单：Booking_Session 的 create-order 返回携带权威 order_id / 金额 / 币种的订单卡。
        self.fake.queue_text_response(
            200, _task_json(cards=[_order_card(order_id, authoritative_minor, "USD")])
        )
        booking = await server.book("Lock a hotel order near the Bund.", member_id=member)
        booking_sid = booking["session_id"]
        order_card = booking["cards"][-1]
        got_order_id = order_card["data"]["order_id"]
        got_authoritative_minor = order_card["data"]["amount_minor"]
        self.assertEqual(got_order_id, order_id)
        self.assertEqual(got_authoritative_minor, authoritative_minor)

        # ② 创建网络令牌：以订单权威金额 + order_id，在独立 payment 会话中创建。
        token = await server.start_token_creation(
            amount_cents=got_authoritative_minor, order_id=got_order_id, member_id=member
        )
        token_sid = token["session_id"]
        # 创建网络令牌成功（非失败态）→ 不挂令牌路径失败引导。
        self.assertNotIn("token_path_failure", token)

        # 会话隔离 + 同 member（两会话归属一致，且各自独立 A2A context）。
        self.assertNotEqual(booking_sid, token_sid)
        self.assertEqual(server._sessions[booking_sid]["kind"], "booking")
        self.assertEqual(server._sessions[token_sid]["kind"], "payment")
        self.assertEqual(booking["member_id"], member)
        self.assertEqual(token["member_id"], member)
        self.assertEqual(self.fake.set_member_calls, [member, member])

        # 创建网络令牌入口卡提交 payload 的两条不变量：金额相等 + external_transaction_id == order_id。
        submits = [
            a for a in self.fake.action_calls
            if a[0] == token_sid and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        mint_payload = submits[0][3]
        self.assertEqual(mint_payload["amount_cents"], authoritative_minor)
        self.assertEqual(mint_payload["external_transaction_id"], order_id)

        # ③ 直扣：回 Booking_Session，pay 动作携创建得到的 payment_token_id（stage 3）。
        self.fake.queue_action_response(200, _task_json(state="completed"))
        minted_token_id = "ptk_r14_main"
        charge = await server.act(
            booking_sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {"payment_token_id": minted_token_id}
        )
        # 直扣成功（stage 3 携令牌凭据）→ 三类软引导均不应出现。
        self.assertNotIn("token_path_failure", charge)
        self.assertNotIn("default_orchestration", charge)
        self.assertNotIn("evo_orchestration", charge)
        self.assertNotIn("payment_warning", charge)

        # ── 三段式顺序守卫（统一事件时间线）：锁单 < 创建网络令牌 < 直扣 ──────────────────────────
        lock_idx = next(
            i for i, e in enumerate(self.fake.events) if e[1] == booking_sid  # Booking 首个交互=锁单
        )
        mint_idx = next(
            i for i, e in enumerate(self.fake.events)
            if e[0] == "action" and e[1] == token_sid and e[2] == "payment.token-setup#submit"
        )
        charge_idx = next(
            i for i, e in enumerate(self.fake.events)
            if e[0] == "action" and e[1] == booking_sid and e[2] == f"{_HOTEL_PAY[0]}#{_HOTEL_PAY[1]}"
        )
        self.assertLess(lock_idx, mint_idx, "锁单必须发生在创建网络令牌之前")
        self.assertLess(mint_idx, charge_idx, "创建网络令牌必须发生在直扣之前")
        # 直扣事件确实携带 payment_token_id（走令牌直扣分支，而非 payment_method_id）。
        charge_event_payload = self.fake.events[charge_idx][3] or {}
        self.assertEqual(charge_event_payload.get("payment_token_id"), minted_token_id)
        self.assertNotIn("payment_method_id", charge_event_payload)

    # ── R14.1：默认选择——未显式选 EVO 的结算步默认被引导走三段式令牌主路径 ──────────────────
    async def test_default_orchestration_advice_describes_three_stage_sequence(self) -> None:
        """现结结算动作未携任何显式支付凭据时，桥层附上「默认走独立 Network_Token 三段式主路径」的
        编排建议——本用例聚焦断言该建议**描述了有序的三段式与其不变量**（11.7 只断言该键出现/EVO 缺席，
        不校验其三段式顺序文案，两者互补不重复）。

        Validates: Requirements 14.1
        """
        booking = await server.book("book a hotel near the Bund", member_id="m-r14")
        out = await server.act(booking["session_id"], _HOTEL_PAY[0], _HOTEL_PAY[1], {})

        self.assertIn("default_orchestration", out)
        advice = out["default_orchestration"]
        # 有序三段式：LOCK FIRST → MINT SECOND → CHARGE。
        self.assertIn("THREE stages", advice)
        self.assertIn("1) LOCK FIRST", advice)
        self.assertIn("2) MINT SECOND", advice)
        self.assertIn("3) CHARGE", advice)
        # 三段式落地手段与两条绑定不变量的文案。
        self.assertIn("start_token_creation", advice)
        self.assertIn("external_transaction_id", advice)
        self.assertIn("authoritative", advice)  # 令牌金额 == 订单权威金额
        self.assertIn("payment_token_id", advice)  # stage 3 携令牌直扣（而非 method）
        # 默认（未选卡）分支必带「未选卡」强约束警告，且不与显式 EVO 分流互相叠加。
        self.assertIn("payment_warning", out)
        self.assertNotIn("evo_orchestration", out)
        self.assertNotIn("token_path_failure", out)

    # ── R14.2：默认 ↔ 显式 EVO 的「决策边界」——同一结算动作按 evo_explicit 翻转分支 ───────
    async def test_default_vs_explicit_evo_decision_boundary(self) -> None:
        """同一 hotel 锁后付结算动作，边界由 **evo_explicit** 划定（而非仅凭 payment_method_id）：
        (a) payload 无凭据 → 默认三段式令牌主路径（default_orchestration + payment_warning）；
        (b) payload 带 payment_method_id 但**未** evo_explicit（选了已绑卡）→ 仍走默认令牌主路径
        （default_orchestration，用该卡创建网络令牌；已选卡故不挂 payment_warning）；
        (c) payload 显式 evo_explicit=true（用户明确选直刷 EVO）→ 翻转到显式 EVO 分流
        （evo_orchestration、不创建网络令牌）。三者软引导键互斥。

        Validates: Requirements 14.2, 14.1
        """
        sid = await self._booking_sid("m-boundary")

        # (a) 无任何凭据 → 默认三段式令牌主路径 + 未选卡警告。
        default_out = await server.act(sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {})
        self.assertIn("default_orchestration", default_out)
        self.assertIn("payment_warning", default_out)
        self.assertNotIn("evo_orchestration", default_out)

        # (b) 带已绑卡 payment_method_id 但未显式选 EVO → 仍默认令牌主路径（用该卡创建网络令牌），
        #     evo_orchestration 缺席；已选卡故不再挂未选卡警告。
        bound_out = await server.act(
            sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {"payment_method_id": "pm_evo_boundary"}
        )
        self.assertIn("default_orchestration", bound_out)
        self.assertNotIn("evo_orchestration", bound_out)
        self.assertNotIn("payment_warning", bound_out)

        # (c) 显式 evo_explicit=true → 翻转到 EVO 兜底轨、不创建网络令牌；不叠加令牌主路径 nudge / 未选卡警告。
        evo_out = await server.act(
            sid,
            _HOTEL_PAY[0],
            _HOTEL_PAY[1],
            {"payment_method_id": "pm_evo_boundary", "evo_explicit": True},
        )
        self.assertIn("evo_orchestration", evo_out)
        self.assertNotIn("default_orchestration", evo_out)
        self.assertNotIn("token_path_failure", evo_out)
        self.assertNotIn("payment_warning", evo_out)
        # 「不创建网络令牌」不变量：显式 EVO 结算动作全程未触发任何创建网络令牌子流程（无 payment.token-setup#submit）。
        mint_actions = [
            a for a in self.fake.action_calls
            if a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(mint_actions, [], "显式 EVO 分流不得创建 Network_Token")

    # ── R14.3：令牌主路径失败 → 失败短路成功态软引导，且绝不静默改用默认卡 ──────────────────────
    async def test_main_path_failure_short_circuits_success_advice_and_forbids_default_card(
        self,
    ) -> None:
        """令牌主路径直扣环节失败（携 payment_token_id 的结算动作返回 failed 终态）→ 结果附
        token_path_failure 引导，并且：
          - **短路**掉成功态软引导（不再出现 default_orchestration / evo_orchestration / payment_warning）；
          - 引导语义**禁止静默改用 platform / developer / member 默认卡**、且回退 EVO 属客户端显式决定。

        本用例从「失败优先短路 + 禁默认卡」角度断言（11.7 已逐环节断言 LOCKING/MINTING/DIRECT CHARGE
        标签，本处不重复环节标签，改为守卫失败态对成功态软引导的短路与 R14.3 的「不静默改卡」硬语义）。

        Validates: Requirements 14.3
        """
        sid = await self._booking_sid("m-fail")
        self.fake.queue_action_response(200, _task_json(state="failed"))
        out = await server.act(
            sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {"payment_token_id": "ptk_r14_fail"}
        )

        # 失败态优先：只挂 token_path_failure，短路掉所有成功态软引导。
        self.assertIn("token_path_failure", out)
        self.assertNotIn("default_orchestration", out)
        self.assertNotIn("evo_orchestration", out)
        self.assertNotIn("payment_warning", out)

        advice = out["token_path_failure"]
        # R14.3 硬语义：可回退 EVO 但须客户端显式决定；绝不静默改用平台/开发者/会员默认卡。
        self.assertIn("EVO", advice)
        self.assertIn("EXPLICIT decision", advice)
        self.assertIn("NEVER silently fall back", advice)
        # 实现文案用大写 DEFAULT，这里大小写无关地守卫「默认卡」措辞。
        self.assertIn("default card", advice.lower())

    # ── R14.4 / R12.3：guide() 把默认编排顺序表述为规范主路径，并含三条约束文案 ─────────────────
    async def test_guide_states_default_main_path_and_three_constraints(self) -> None:
        """``guide()`` 文案把「锁单 → 独立会话创建网络令牌 → 回会话直扣」表述为默认/规范主路径，并包含
        R12.3 三条约束（三条均须出现）：①先锁单后创建网络令牌；②两会话同 member；③strict 订单绑定；同时
        保留「不得静默回退平台/开发者/会员默认卡」的既有强约束。

        （11.7 完全不触及 ``guide()``，本用例为 11.6 独有维度。）

        Validates: Requirements 14.4, 12.3
        """
        text = await server.guide()

        # 默认/规范主路径的三段式表述与有序性。
        self.assertIn("DEFAULT / CANONICAL MAIN PATH", text)
        self.assertIn("INDEPENDENT NETWORK-TOKEN DIRECT CHARGE", text)
        self.assertIn("THREE-STAGE", text)
        self.assertIn("→ MINT SECOND", text)
        self.assertIn("→ CHARGE", text)
        # 明确 EVO 不是默认，仅显式/回退。
        self.assertIn("EVO preauth+capture is NOT the default", text)

        # 三条约束（R12.3 / R14.4）——三条均须出现。
        self.assertIn("LOCK FIRST, MINT SECOND", text)              # ① 先锁单后创建网络令牌
        self.assertIn("SAME member_id ACROSS BOTH SESSIONS", text)  # ② 两会话同 member
        self.assertIn("STRICT ORDER BINDING", text)                 # ③ strict 订单绑定
        self.assertIn("external_transaction_id", text)              # strict 绑定的落地字段

        # 既有强约束保留：绝不静默回退平台/开发者/会员默认卡（该句在实现里跨行，断言不跨行的连续片段）。
        self.assertIn(
            "silently falling back to any platform / developer / member default card",
            text,
        )

    # ── 辅助 ─────────────────────────────────────────────────────────────────────
    async def _booking_sid(self, member_id: str) -> str:
        booking = await server.book("book a hotel near the Bund", member_id=member_id)
        return booking["session_id"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
