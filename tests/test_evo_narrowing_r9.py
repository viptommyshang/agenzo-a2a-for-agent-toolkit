"""任务 11.7（bridge 层）：R9「EVO 预授权兜底轨收窄为显式/回退」回归测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 MCP 桥软引导（方案 A）——即 ``act`` /
``start_token_creation`` 在货币结算动作 / 铸令牌子流程上附挂的三分支非阻塞引导：
  - ``default_orchestration``：未显式选 EVO 卡时，默认编排「独立 Network_Token 现结」三段式主路径。
  - ``evo_orchestration``：显式携带 ``payment_method_id``（EVO 卡）时，走 EVO 兜底轨、不铸令牌。
  - ``token_path_failure``：令牌主路径（锁单/铸令牌/直扣）任一环节失败时，返回「可显式回退 EVO、
    绝不静默改用默认卡」的引导。

本文件聚焦 R9 的**「收窄」语义**（与任务 3.5 的「月结/EVO 兜底存在性」回归去重）：
  - R9.4 未显式指定支付方式 → 默认**不走 EVO**（走令牌现结主路径）；
  - R9.1 显式提供有效 ``payment_method_id`` → 明确走 EVO 兜底轨（Payment_Gate 预授权+捕获），不铸令牌；
  - R9.2 令牌路径失败 → 返回**可回退 EVO** 的引导（回退是客户端显式决定，桥层不静默改卡）。

> 层次边界（与设计 Testing Strategy「EVO 收窄（R9）」一致）：R9.5/R9.6（EVO 预授权/捕获失败、
> ``payment_method_id`` 无效 → 中止不扣款 + 可区分错误）属**平台结算层**行为，由 agenzo-platform 的
> ``test_evo_narrowing_r9_settlement.py`` 覆盖；本 bridge 文件只断言编排/引导层的收窄语义，并在
> 「显式 EVO 结算失败」用例里确认它**不**被误判为令牌主路径回退（划清两层职责）。

覆盖需求：R9.1、R9.2、R9.4（编排/引导层）。

说明：复用姊妹测试 ``test_token_first_two_session`` 的假 A2A bridge（``FakeBridge`` / ``_task_json``），
仅依赖标准库 ``unittest.IsolatedAsyncioTestCase``，可被 ``unittest`` 或 ``pytest`` 直接收集；
未改动任何被测实现——仅以假 bridge 替换模块级 ``server._bridge`` 记录/构造其响应。
"""

from __future__ import annotations

import os
import sys

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


# 代表性的「货币结算动作」卡片坐标（component, action）——满足 server._is_settle_action。
_HOTEL_PAY = ("hotel.order-detail", "pay")          # hotel 锁后付：pay 才扣款
_FLIGHT_CONFIRM = ("flight.booking-confirm", "confirm")  # 其它域在下单 confirm/book 扣款


class R9NarrowingBridgeTest(unittest.IsolatedAsyncioTestCase):
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

    async def _booking(self) -> str:
        booking = await server.book("book a hotel near the Bund", member_id="m-r9")
        return booking["session_id"]

    # ── R9.4：未显式指定支付方式 → 默认不走 EVO（走令牌现结主路径）────────────────────────
    async def test_unspecified_payment_defaults_to_token_main_path_not_evo(self) -> None:
        """现结结算动作未携带 payment_method_id（未选 EVO 卡）→ 默认编排令牌三段式主路径，
        **不**默认走 EVO；并提醒不得静默改用平台/会员默认卡。

        Validates: Requirements 9.4
        """
        sid = await self._booking()
        out = await server.act(sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {})

        # 默认走令牌主路径（default_orchestration），而不是 EVO（evo_orchestration 缺席）。
        self.assertIn("default_orchestration", out)
        self.assertNotIn("evo_orchestration", out)
        self.assertNotIn("token_path_failure", out)
        advice = out["default_orchestration"]
        self.assertIn("Network_Token", advice)
        # 明确表述：EVO 不是默认，仅显式/回退才用。
        self.assertIn("EVO", advice)
        # 软引导仍带「绝不静默改用默认卡」的强约束警告。
        self.assertIn("payment_warning", out)
        self.assertIn("default", out["payment_warning"].lower())

    async def test_unspecified_payment_default_is_domain_agnostic(self) -> None:
        """跨域一致：flight 下单 confirm 未选 EVO 卡时，同样默认走令牌主路径、不默认 EVO。

        Validates: Requirements 9.4
        """
        sid = await self._booking()
        out = await server.act(sid, _FLIGHT_CONFIRM[0], _FLIGHT_CONFIRM[1], {})

        self.assertIn("default_orchestration", out)
        self.assertNotIn("evo_orchestration", out)
        self.assertNotIn("token_path_failure", out)

    # ── R9.1：显式提供有效 payment_method_id → 走 EVO 兜底轨、不铸令牌 ────────────────────
    async def test_explicit_evo_optin_routes_to_evo_without_mint(self) -> None:
        """结算动作**显式声明 evo_explicit=true**（用户明确选择直刷 EVO 卡而非网络令牌）→ 明确走 EVO
        预授权+捕获兜底轨、**不铸造** Network_Token；不再叠加默认令牌主路径引导。

        Validates: Requirements 9.1
        """
        sid = await self._booking()
        out = await server.act(
            sid,
            _HOTEL_PAY[0],
            _HOTEL_PAY[1],
            {"payment_method_id": "pm_evo_valid", "evo_explicit": True},
        )

        # 显式 EVO 分流：evo_orchestration 出现，default_orchestration 缺席（互斥）。
        self.assertIn("evo_orchestration", out)
        self.assertNotIn("default_orchestration", out)
        self.assertNotIn("token_path_failure", out)
        # 显式选 EVO 时不再挂「未选卡」的默认卡警告。
        self.assertNotIn("payment_warning", out)
        evo = out["evo_orchestration"]
        self.assertIn("EVO preauth+capture", evo)
        self.assertIn("DO NOT mint", evo)

    async def test_bound_card_without_optin_defaults_to_token_mint(self) -> None:
        """结算动作带 payment_method_id（选了已绑卡）但**未**声明 evo_explicit → 默认令牌主路径
        （用该卡铸令牌再直扣），而非 EVO 直扣。已有卡不是跳过铸令牌的理由。

        Validates: Requirements 9.1, 14.1
        """
        sid = await self._booking()
        out = await server.act(
            sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {"payment_method_id": "pm_evo_valid"}
        )

        # 默认令牌主路径：default_orchestration 出现，evo_orchestration 缺席（互斥）。
        self.assertIn("default_orchestration", out)
        self.assertNotIn("evo_orchestration", out)
        self.assertNotIn("token_path_failure", out)
        advice = out["default_orchestration"]
        # 建议里明确「已有卡也先用该卡铸令牌」。
        self.assertIn("start_token_creation", advice)
        self.assertIn("EXISTING CARD", advice)
        # 已带 payment_method_id（选了卡）→ 不再挂「未选卡」默认卡警告。
        self.assertNotIn("payment_warning", out)

    async def test_explicit_evo_optin_is_not_rerouted_to_token(self) -> None:
        """软引导守卫：桥层尊重客户端**显式 evo_explicit=true** 的 EVO 选择，不覆盖/不改走令牌路径
        （仍是软引导，不硬拦截）。

        Validates: Requirements 9.1
        """
        sid = await self._booking()
        out = await server.act(
            sid,
            _FLIGHT_CONFIRM[0],
            _FLIGHT_CONFIRM[1],
            {"payment_method_id": "pm_evo_1", "evo_explicit": True},
        )
        self.assertIn("evo_orchestration", out)
        self.assertNotIn("default_orchestration", out)

    # ── R9.2：令牌路径失败 → 返回可回退 EVO 引导（锁单 / 铸令牌 / 直扣三环节）──────────────
    async def test_lock_failure_yields_fallback_evo_guidance(self) -> None:
        """令牌主路径**锁单**环节失败（结算动作返回 failed 终态、未携任何显式凭据）→ 返回定位到
        LOCKING 环节的回退引导：可显式回退 EVO、绝不静默改用默认卡。

        Validates: Requirements 9.2
        """
        sid = await self._booking()
        # 结算动作返回失败终态。
        self.fake.queue_action_response(200, _task_json(state="failed"))
        out = await server.act(sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {})

        self.assertIn("token_path_failure", out)
        advice = out["token_path_failure"]
        self.assertIn("LOCKING", advice)
        self._assert_fallback_semantics(advice)
        # 失败优先：不再叠加成功态的默认/显式软引导。
        self.assertNotIn("default_orchestration", out)
        self.assertNotIn("evo_orchestration", out)

    async def test_mint_failure_yields_fallback_evo_guidance(self) -> None:
        """令牌主路径**铸令牌**环节失败（独立 Payment_Session 的 token-setup#submit 返回 failed）→
        返回定位到 MINTING 环节的回退引导。

        Validates: Requirements 9.2
        """
        # 铸令牌提交返回失败终态（分类种子 send_text 用默认成功响应）。
        self.fake.queue_action_response(200, _task_json(state="failed"))
        out = await server.start_token_creation(
            amount_cents=42800, order_id="hho_R9FAIL", member_id="m-r9"
        )

        self.assertIn("token_path_failure", out)
        advice = out["token_path_failure"]
        self.assertIn("MINTING", advice)
        self._assert_fallback_semantics(advice)

    async def test_charge_failure_yields_fallback_evo_guidance(self) -> None:
        """令牌主路径**直扣**环节失败（结算动作携 payment_token_id 却返回 failed）→ 返回定位到
        DIRECT CHARGE 环节的回退引导。

        Validates: Requirements 9.2
        """
        sid = await self._booking()
        self.fake.queue_action_response(200, _task_json(state="failed"))
        out = await server.act(
            sid, _HOTEL_PAY[0], _HOTEL_PAY[1], {"payment_token_id": "ptk_r9"}
        )

        self.assertIn("token_path_failure", out)
        advice = out["token_path_failure"]
        self.assertIn("DIRECT CHARGE", advice)
        self._assert_fallback_semantics(advice)

    async def test_transport_failure_on_mint_yields_fallback_guidance(self) -> None:
        """铸令牌分类种子即传输失败（HTTP 非 200）→ 同样返回可回退 EVO 的引导（覆盖 error 键分支）。

        Validates: Requirements 9.2
        """
        # send_text（分类种子）直接返回 HTTP 502。
        self.fake.queue_text_response(502, "upstream boom")
        out = await server.start_token_creation(
            amount_cents=1000, order_id="hho_R9TX", member_id="m-r9"
        )
        self.assertIn("token_path_failure", out)
        self.assertIn("MINTING", out["token_path_failure"])

    # ── 层次边界：显式 EVO 结算失败 ≠ 令牌主路径回退（R9.5 归平台层）────────────────────────
    async def test_explicit_evo_failure_is_not_token_path_fallback(self) -> None:
        """**显式 EVO**（evo_explicit=true）结算动作即便返回 failed，也**不**被判为「令牌主路径失败
        回退」——EVO 预授权/捕获失败（R9.5）属平台结算层职责，不在桥层令牌回退引导范围。仅带
        payment_method_id 而无 evo_explicit 属默认令牌主路径，其失败会判为 lock 回退（见
        test_lock_failure_yields_fallback_evo_guidance）。

        Validates: Requirements 9.2
        """
        sid = await self._booking()
        self.fake.queue_action_response(200, _task_json(state="failed"))
        out = await server.act(
            sid,
            _HOTEL_PAY[0],
            _HOTEL_PAY[1],
            {"payment_method_id": "pm_evo_valid", "evo_explicit": True},
        )
        # 关键：显式 EVO 的失败不会挂上令牌回退引导（那是令牌主路径三环节专属）。
        self.assertNotIn("token_path_failure", out)

    # ── 辅助断言 ─────────────────────────────────────────────────────────────────
    def _assert_fallback_semantics(self, advice: str) -> None:
        """回退引导的语义守卫：可显式回退 EVO、且绝不静默改用平台/会员默认卡。"""
        self.assertIn("EVO", advice)
        self.assertIn("EXPLICIT decision", advice)
        self.assertIn("NEVER silently fall back", advice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
