"""收单门店按供应商路由（`provider_id`）：桥层透传契约、空值语义与模型可见文案测试。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``start_token_creation``（声明点）、
``start_payment``（**刻意不是**声明点）与 ``guide`` / ``_DEFAULT_TOKEN_ORCHESTRATION`` 两处
模型可见的编排文案。

一个 API Key 名下的付款可以按**承接供应商**落到不同的收单门店（契约
``docs/a2a-docs/a2a-cards-payment.md`` §9.5「收单门店按供应商路由」）。对接方只声明供应商标识，
门店号是平台侧配置、不出现在任何请求 / 卡片 / 响应里。走令牌的付款**只在创建令牌时声明一次**，
之后业务域 ``pay`` / 聚合结算 / 按令牌扣款都从令牌读。

本文件锁四类不变量：
  - **透传（AC-1.8 前半句）**：``start_token_creation(provider_id=…)`` 把声明值原样放进
    ``payment.token-setup#submit`` 的 payload 键 ``provider_id``，与 ``external_transaction_id``
    共存、互不干扰；聚合形态（``pg_…`` + ``platform``）同样成立。
  - **不传则不出现（AC-1.8 后半句）**：省略入参 → payload 与基线**逐字一致**（没有该键 →
    落平台默认门店，不触发任何新错误码）。「省略」与「声明了无效取值」是两件事（契约 §9.5）：
    空串 / 纯空白属**已声明但无效**，原样上送、由平台 fail-closed 以 409
    ``PROVIDER_NOT_CONFIGURED`` 拒付，**不**在本层折叠成省略——折叠会让调用方的拼装 bug 静默
    落到默认门店。入参默认值因此是 ``None``（唯一能区分「没传」与「传了空串」的办法）。
  - **声明点只有一个（否定护栏）**：``start_payment`` 驱动的是 ``payment.setup#submit``
    ——「宿主交回 ``payment_method_id`` 的多品牌路由入口」，自己不发起扣款，按 design §2.4
    是**刻意不加**的声明点；真正发起独立支付的 ``payment.pay-setup`` 在本仓经 ``act(...)``
    驱动（payload 原样透传，无字段白名单）。在 ``start_payment`` 上再加一个声明点会让同一笔
    付款有两个真相，违反契约 §9.5「只声明一次」。
  - **文案契约（AC-11.1 的落地前提）**：docstring 就是 MCP 工具说明、
    ``_DEFAULT_TOKEN_ORCHESTRATION`` 是模型在「该创建令牌了」那一刻真正读到的那段话——
    两处都必须写明取值映射与两个错误码，否则模型照旧参数清单调用、必然漏声明。

与姊妹文件的去重边界：``test_token_first_two_session.py`` 锁两会话 / 同 member / 结构化动作的
通用形状，``test_trip_aggregate_payment.py`` 锁支付组绑定与整车总额；本文件只补 ``provider_id``
这一维，不复述那些断言（仅复用其 FakeBridge 测试替身）。

未改动任何被测实现——仅以假 bridge 替换模块级 ``server._bridge``。
"""

from __future__ import annotations

import inspect
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

# 契约 §9.5 取值表里的平台已接入承接方 + 聚合口径。
_HOTEL_PROVIDER = "redaug"
_AGGREGATE_PROVIDER = "platform"


class ProviderStoreRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 隔离并替换模块级全局状态，测试后恢复。
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = FakeBridge(member_id="m-provider")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    # ── 辅助：取某会话里 create-token 入口卡那次 submit 的 payload ──────────────────────
    def _token_setup_payload(self, session_id: str) -> dict:
        submits = [
            a for a in self.fake.action_calls
            if a[0] == session_id and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        return submits[0][3]

    # ── 辅助：未加任何门店声明时该入口卡 submit 的**完整**基线 payload ───────────────────
    @staticmethod
    def _baseline_payload(amount_cents: int, order_id: str | None = None) -> dict:
        """本改动之前 ``payment.token-setup#submit`` 的逐字 payload（零回归的比对基准）。

        三个必有键来自 ``start_token_creation`` 的固定组装；``order_id`` 非空时多一个
        ``external_transaction_id``。用**完整字典相等**比对，才能同时抓住「少了键」「多了键」
        「值形态变了」三类回归——只断言个别字段抓不住。
        """
        payload = {
            "amount_cents": amount_cents,
            "recipient_name": "",
            "recipient_account": "",
        }
        if order_id:
            payload["external_transaction_id"] = order_id
        return payload

    # ── 透传：声明值原样进 payload，相对基线只多这一个键 ───────────────────────────────
    async def test_declared_provider_reaches_token_setup_payload(self) -> None:
        """``provider_id="redaug"`` → payload 相对基线**只多**一个原样的 ``provider_id`` 键。

        桥层对声明值不做任何改写（不折叠大小写、不 strip、不校验枚举）：合法性的唯一判据是平台侧
        映射表里有没有这条记录。完整字典比对同时守住「不改写其余字段」与「不夹带别的新键」。
        """
        token = await server.start_token_creation(
            amount_cents=42800,
            order_id="hho_X",
            member_id="m-provider",
            provider_id=_HOTEL_PROVIDER,
        )
        payload = self._token_setup_payload(token["session_id"])

        expected = self._baseline_payload(42800, "hho_X")
        # 订单绑定与门店归属是两件独立的事：基线原样保留，只增量一个 provider_id。
        expected["provider_id"] = _HOTEL_PROVIDER
        self.assertEqual(payload, expected)

    # ── 不传则不出现：省略 == 落平台默认门店，且 payload 与基线逐字一致 ──────────────────
    async def test_undeclared_provider_is_absent_from_payload(self) -> None:
        """省略 ``provider_id`` → payload 与改动前的基线**逐字相等**（AC-1.8 后半句）。

        这是向后兼容的既有行为：整个字段省略 → 落平台为该调用方配置的默认门店，不触发任何新错误码。
        用完整字典相等比对来兑现「不声明则零回归」这个承诺。
        """
        token = await server.start_token_creation(
            amount_cents=1000, order_id="hho_Y", member_id="m-provider"
        )
        payload = self._token_setup_payload(token["session_id"])

        self.assertEqual(payload, self._baseline_payload(1000, "hho_Y"))
        self.assertNotIn("provider_id", payload)

    # ── 空值语义：空串 / 纯空白是「已声明但无效」，原样上送、由平台 fail-closed ─────────────
    async def test_blank_provider_is_forwarded_verbatim(self) -> None:
        """``provider_id=""`` 与 ``provider_id="   "`` 都**原样进** payload，不折叠成「未声明」。

        依据契约 §9.5：「不声明」与「声明了未配置的取值」是两件事——整个字段省略 → 落默认门店；
        声明了一个未配置的取值（空串 / 纯空白也算）→ 直接拒付 ``PROVIDER_NOT_CONFIGURED``，平台
        不回落默认门店兜底。所以判据是 ``is not None`` 而**不是**真值判断：若在本层把空串折叠成
        省略，调用方的拼装 bug 就会静默落到默认门店（钱落错收单主体且事后无从发现），而同样的空值
        经 ``agenzo-cli`` 的 ``--provider-id`` 却会被平台拒——两个接入面行为还会不一致。
        入参默认值用 ``None`` 正是为了能区分「没传」与「传了空串」。
        """
        for blank in ("", "   "):
            with self.subTest(provider_id=repr(blank)):
                self.fake.action_calls.clear()
                token = await server.start_token_creation(
                    amount_cents=1000, member_id="m-provider", provider_id=blank
                )
                payload = self._token_setup_payload(token["session_id"])
                self.assertEqual(payload["provider_id"], blank)

    # ── 入参默认值必须是 None（而不是 ""），否则无法区分「没传」与「传了空串」─────────────
    def test_provider_id_parameter_defaults_to_none(self) -> None:
        """签名默认值是 ``None``：这是上面那条空值语义唯一可行的实现前提。

        若默认值写成 ``""``，「省略」与「声明空串」在函数体里完全同形，必然二者取一地错——
        这条断言把实现方式本身钉住，防止被改回真值判断。
        """
        param = inspect.signature(server.start_token_creation).parameters["provider_id"]
        self.assertIsNone(param.default)

    # ── 聚合形态：支付组 + platform 两键齐备 ─────────────────────────────────────────
    async def test_aggregate_declares_platform_alongside_payment_group(self) -> None:
        """聚合令牌：``order_id=<pg_…>`` + ``provider_id="platform"`` 两键齐备、取值正确。

        一次扣款只能落一个门店，所以整车走平台聚合结算门店、不按批内各订单的承接供应商拆分
        （声明某一家供应商会被平台以 ``PROVIDER_MISMATCH`` 拒）。
        """
        token = await server.start_token_creation(
            amount_cents=48000,
            order_id="pg_01M3TRIPCART7QK2",
            member_id="m-provider",
            provider_id=_AGGREGATE_PROVIDER,
        )
        payload = self._token_setup_payload(token["session_id"])

        self.assertEqual(payload["provider_id"], _AGGREGATE_PROVIDER)
        self.assertEqual(payload["external_transaction_id"], "pg_01M3TRIPCART7QK2")

    # ── 否定护栏：start_payment 不是声明点 ──────────────────────────────────────────
    async def test_start_payment_is_not_a_declaration_point(self) -> None:
        """``start_payment`` **既没有**该入参、**也不**在 ``payment.setup#submit`` 的 payload 里带它。

        依据 design §2.4「刻意不加的两处声明点」：``scenarios["pay-setup"]`` 与
        ``ui["payment.setup"]`` 都不加 ``provider_id``——它们是「宿主交回 ``payment_method_id`` 的
        多品牌路由入口」，自己不发起扣款。真正发起独立支付的是 ``payment.pay-setup``（在本仓经
        ``act(...)`` 驱动，payload 原样透传）。在这里再加一个声明点会让同一笔付款有两个真相，
        违反契约 §9.5「只声明一次」。
        """
        self.assertNotIn(
            "provider_id", inspect.signature(server.start_payment).parameters
        )

        payment = await server.start_payment(
            amount_cents=4433, recipient_name="Richard Chen", member_id="m-provider"
        )
        sid = payment["session_id"]
        submits = [
            a for a in self.fake.action_calls
            if a[0] == sid and a[1] == "payment.setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        self.assertNotIn("provider_id", submits[0][3])

    # ── 文案契约：start_token_creation 的 docstring（模型唯一能看到的工具说明）────────────
    def test_start_token_creation_docstring_documents_provider_routing(self) -> None:
        """docstring 必须给全取值映射与两个错误码，否则等于加了个没人会用的参数。

        逐字对齐契约 §9.5 的取值表：四个平台取值 + 三个订单号前缀 + 「自己申报的标识须先提交平台
        配置后才生效」+ 「不声明 = 落默认门店」+ 「只声明一次」+ 两个 409 错误码。
        """
        doc = server.start_token_creation.__doc__ or ""

        self.assertIn("provider_id", doc)
        # 契约 §9.5 取值表：四个取值与它们绑定的订单号前缀。
        for value in ("redaug", "flink", "elife", "platform"):
            self.assertIn(value, doc)
        for prefix in ("hho_", "ffo_", "rio_"):
            self.assertIn(prefix, doc)
        # 两个错误码字面量（AC-11.2：全链路一致）。
        self.assertIn("PROVIDER_MISMATCH", doc)
        self.assertIn("PROVIDER_NOT_CONFIGURED", doc)
        # 自己申报的标识：须先提交平台、由平台配置后才生效。
        self.assertIn("FILE IT WITH", doc)
        # 不声明 = 落默认门店（向后兼容的既有行为）。
        self.assertIn("DEFAULT store", doc)
        # 声明是**推荐**而非协议强制：契约 §9.5 只规定「声明了就强校验」，省略仍合法。
        self.assertIn("SHOULD declare it", doc)
        self.assertNotIn("MUST declare provider_id", doc)
        # 「省略」≠「声明空串」：必须明确告诉模型用不到就别传，而不是填空串。
        self.assertIn("OMITTING THE ARGUMENT ENTIRELY IS VALID", doc)
        self.assertIn("do NOT pass an EMPTY", doc)
        # 只声明一次：之后的扣款入口从令牌读，重述只校验、不改写。
        self.assertIn("DECLARE IT ONCE", doc)
        # 声明的是供应商标识，不是门店号——门店号不出现在本仓任何一处。
        self.assertNotIn("store_code", doc)
        self.assertNotIn("sign_key", doc)

    # ── 文案契约：运行时软引导（模型在「该创建令牌了」那一刻读到的那段话）────────────────────
    def test_default_token_orchestration_lists_provider_id(self) -> None:
        """``_DEFAULT_TOKEN_ORCHESTRATION`` 的 MINT SECOND 一段必须把 ``provider_id`` 列进参数清单。

        它由 ``act()`` 以 ``default_orchestration`` 键附在结果上，是模型真正照着调用的那份参数
        清单；只改 docstring 而漏改这里，模型会照旧清单调用、必然漏声明。
        """
        advice = server._DEFAULT_TOKEN_ORCHESTRATION

        self.assertIn("provider_id", advice)
        self.assertIn("2) MINT SECOND", advice)
        # 前缀 → 承接供应商的映射，和「声明错 / 未开通就在创建这一步被拒」的后果。
        for value in ("redaug", "flink", "elife"):
            self.assertIn(value, advice)
        self.assertIn("PROVIDER_MISMATCH", advice)
        self.assertIn("PROVIDER_NOT_CONFIGURED", advice)
        # 模态与契约 §9.5 一致：声明是推荐、省略仍合法（只是会落默认门店）——不得写成协议强制。
        self.assertIn("SHOULD declare it", advice)
        self.assertNotIn("MUST declare provider_id", advice)

    # ── 文案契约：guide() 的驾驶法则 ────────────────────────────────────────────────
    async def test_guide_documents_provider_store_routing(self) -> None:
        """``guide()`` 必须在令牌主路径与聚合支付两段都写明 ``provider_id``。

        ``guide()`` 是模型读到的驾驶法则：主路径段给取值映射与两个错误码，聚合段给
        「必须 ``platform``、一次扣款只落一个门店」。
        """
        text = await server.guide()

        self.assertIn("provider_id", text)
        for value in ("redaug", "flink", "elife", "platform"):
            self.assertIn(value, text)
        self.assertIn("ACQUIRING STORE ROUTING", text)
        self.assertIn("PROVIDER_MISMATCH", text)
        self.assertIn("PROVIDER_NOT_CONFIGURED", text)
        # 单订单付款：声明是推荐、省略仍合法（契约 §9.5 只规定「声明了就强校验」）。
        self.assertIn("SHOULD declare it", text)
        # 聚合口径：一次扣款只落一个门店，不按子订单的承接供应商拆分。这里的 MUST 有契约 §26.2 背书
        # （聚合令牌必须声明 platform，声明单一供应商会被拒），与上面单订单的 SHOULD 不矛盾。
        self.assertIn('MUST declare provider_id="platform"', text)
        self.assertIn("ONE acquiring store", text)
        # 门店号不出现在驾驶法则里（AC-1.9 / AC-10.3）。
        self.assertNotIn("store_code", text)
        self.assertNotIn("sign_key", text)

    # ── 文案契约：act() 指出 §12 / §23 的声明点，并排除退款卡 ─────────────────────────
    def test_act_docstring_points_at_standalone_declaration_points(self) -> None:
        """``act`` 的 docstring 必须说明独立支付 / 独立按令牌扣款两张**入口卡**的 payload 可带
        ``provider_id``，重述须与令牌一致，且退款卡不接受该字段。

        这两条链路今天就能带该字段（``act`` 把 payload 原样交给 bridge，无字段白名单），缺的只是
        「模型不知道可以带」。
        """
        doc = server.act.__doc__ or ""

        self.assertIn("payment.pay-setup#submit", doc)
        self.assertIn("payment.charge-token-setup#submit", doc)
        self.assertIn("provider_id", doc)
        # charge-token 上是重述：必须与令牌一致，不一致则拒付，且绝不改写令牌。
        self.assertIn("RESTATEMENT", doc)
        self.assertIn("PROVIDER_MISMATCH", doc)
        # 退款卡不接受该字段，给了也不生效。
        self.assertIn("REFUND entry card does NOT accept", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
