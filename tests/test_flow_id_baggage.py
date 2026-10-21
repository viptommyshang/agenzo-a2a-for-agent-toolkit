"""``flow_id`` 的铸造、跨会话复用与出站 ``baggage`` 头。

本桥是链路的最外层发起方，也是**唯一知道「订单会话与支付会话属于同一次用户任务」的角色**
——支付会话是它代为开的。所以 flow_id 在这里铸造一次，两个会话带同一个值。

为什么不能用现有的 id 代替：``session_id``（A2A ``contextId``）每个会话一个，一次订单天然
跨两个（订单会话 + 独立的支付会话，后者铸 Network Token 用，刻意隔离以免冲掉订单的
``flow_state``）；``trace_id`` 每个对话轮次一个，实测一次订酒店产生 11 个。

不连编排器：用 httpx MockTransport 捕获实际发出的请求头。
"""

from __future__ import annotations

import os
import sys

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import unittest  # noqa: E402

import httpx  # noqa: E402

from agenzo_a2a_for_agent_toolkit.a2a import A2ABridge  # noqa: E402
from agenzo_a2a_for_agent_toolkit.flow import (  # noqa: E402
    FLOW_ID_BAGGAGE_KEY,
    FLOW_ID_PREFIX,
    build_baggage,
    is_valid_flow_id,
    new_flow_id,
)


class _Cfg:
    """最小配置替身（只含 A2ABridge 构造与发请求用到的字段）。"""

    BASE_URL = "https://orch.test"
    AGENT_ID = "base-orchestrator"
    MEMBER_ID = "prod-user-019"
    API_KEY = "k"
    HTTP_TIMEOUT = 5.0
    STREAM = False
    DEBUG = False
    DEBUG_MAXLEN = 20000
    DEBUG_BUFFER = 50


def _make_bridge(captured: list[httpx.Headers]) -> A2ABridge:
    """构造一个请求被 MockTransport 截下的 bridge，并跳过注册 / 换 token。"""

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"kind": "task"}})

    bridge = A2ABridge(_Cfg())
    bridge._token = "tok"
    bridge._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return bridge


class FlowIdShapeTest(unittest.TestCase):
    """铸造与校验的形状契约（与 py-commons 的权威定义保持一致）。"""

    def test_minted_flow_id_is_prefixed(self) -> None:
        """``flw_`` 前缀与业务 ID 的既有约定同款（``hho_`` / ``ffo_`` / ``ptk_``）。

        价值不只是好认：trace_id / request_id / session_id 全是裸 32 位 hex，grep 出一串
        hex 根本分不清是哪一类。
        """
        value = new_flow_id()

        self.assertTrue(value.startswith(FLOW_ID_PREFIX))
        self.assertEqual(len(value), len(FLOW_ID_PREFIX) + 32)
        self.assertTrue(is_valid_flow_id(value))

    def test_build_baggage_refuses_bad_values(self) -> None:
        """不合规就不发头，而不是「尽力修一修」。该值会进下游日志与子进程环境变量。"""
        for bad in (
            None,
            "",
            "a3f9c1d84b7e4f2a9c6d0e8b5a2f7c13",  # 裸 hex，缺前缀
            "flw_short",
            "flw_has space",
            "flw_has\nnewline",  # 换行能伪造出一整行假日志
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(build_baggage(bad))

    def test_build_baggage_emits_the_namespaced_member(self) -> None:
        flow_id = new_flow_id()

        self.assertEqual(build_baggage(flow_id), f"{FLOW_ID_BAGGAGE_KEY}={flow_id}")


class FlowReuseTest(unittest.TestCase):
    """跨会话复用 —— 本方案的核心。"""

    def test_book_starts_a_new_flow(self) -> None:
        """``book()`` 的语义是「开一段新会话」→ 强制换新。

        复用的话，同一个进程里先后办的两件事会被并进同一条旅程，按 flow_id 查会捞出不
        相干的订单。
        """
        bridge = _make_bridge([])

        first = bridge.start_new_flow()
        second = bridge.start_new_flow()

        self.assertNotEqual(first, second)
        self.assertEqual(bridge.flow_id, second)

    def test_payment_session_reuses_the_booking_flow(self) -> None:
        """支付入口沿用当前 flow_id —— 这正是本方案要解决的那件事。

        订单会话与支付会话的 ``session_id`` 必然不同，只有 flow_id 能把两段并成一次任务。
        """
        bridge = _make_bridge([])
        booking_flow = bridge.start_new_flow()

        self.assertEqual(bridge.ensure_flow(), booking_flow)
        self.assertEqual(bridge.flow_id, booking_flow)

    def test_ensure_flow_mints_when_there_is_no_task_yet(self) -> None:
        """调用方没先 book 就直接开支付（独立收款 / 单独铸 token）时才铸新的。"""
        bridge = _make_bridge([])

        self.assertIsNone(bridge.flow_id)
        minted = bridge.ensure_flow()

        self.assertTrue(is_valid_flow_id(minted))
        self.assertEqual(bridge.ensure_flow(), minted)  # 第二次不再换


class OutboundHeaderTest(unittest.IsolatedAsyncioTestCase):
    """出站头：A2A 回合与工具端点都要带上。"""

    async def test_a2a_turns_carry_the_baggage_header(self) -> None:
        captured: list[httpx.Headers] = []
        bridge = _make_bridge(captured)
        flow_id = bridge.start_new_flow()

        await bridge.send_text("ctx_1", "book a hotel")

        self.assertEqual(captured[0]["baggage"], f"{FLOW_ID_BAGGAGE_KEY}={flow_id}")

    async def test_both_sessions_send_the_same_flow_id(self) -> None:
        """端到端的那条不变量：订单会话与支付会话的出站头里 flow_id 一致。"""
        captured: list[httpx.Headers] = []
        bridge = _make_bridge(captured)
        bridge.start_new_flow()

        await bridge.send_text("ctx_booking", "book a hotel")
        bridge.ensure_flow()  # start_payment / start_token_creation 走这条
        await bridge.send_text("ctx_payment", "Create a network token.")

        self.assertEqual(captured[0]["baggage"], captured[1]["baggage"])

    async def test_utility_endpoints_carry_it_too(self) -> None:
        """工具端点（地理编码 / 时间解析）同样带上，避免「A2A 带了、工具忘了带」的逐点遗漏。"""
        captured: list[httpx.Headers] = []
        bridge = _make_bridge(captured)
        flow_id = bridge.start_new_flow()

        await bridge.call_tool(
            "/orch-public/tools/resolve-location", {"address": "Orchard Road"}
        )

        self.assertEqual(captured[0]["baggage"], f"{FLOW_ID_BAGGAGE_KEY}={flow_id}")

    async def test_no_baggage_before_any_task_starts(self) -> None:
        """没有 flow_id 就不加这个头（不发空头让下游判空）。"""
        captured: list[httpx.Headers] = []
        bridge = _make_bridge(captured)

        await bridge.send_text("ctx_1", "hello")

        self.assertNotIn("baggage", captured[0])

    async def test_exchanges_record_the_flow_id(self) -> None:
        """``inspect()`` 要能直接回答「这笔的 flow_id 是什么」，不必翻服务器日志反推。"""
        bridge = _make_bridge([])
        flow_id = bridge.start_new_flow()

        await bridge.send_text("ctx_1", "book a hotel")

        self.assertEqual(bridge.recent_exchanges()[-1]["flow_id"], flow_id)


if __name__ == "__main__":
    unittest.main()


class BridgeAttributeContractTest(unittest.TestCase):
    """``member_id`` / ``flow_id`` 必须是**属性**，读出来是值而不是方法对象。

    这是一次真实事故的回归锁：加 ``flow_id`` 时把紧邻的 ``@property`` 装饰器截给了它，
    ``member_id`` 变成裸方法，于是 ``server._result()`` 把**绑定方法对象**塞进返回值，
    FastMCP 序列化时报 `Unable to serialize unknown type: <class 'method'>`，订酒店直接失败。

    测试当时没拦住：两个 FakeBridge 替身里 ``member_id`` 本来就带 @property，只给替身补了
    ``flow_id``——替身是对的、真实类是错的，于是回归被掩盖。所以这条**只打真实类**，
    并且断言「能 JSON 序列化」，而不是断言「等于某个值」。
    """

    def _bridge(self) -> A2ABridge:
        return _make_bridge([])

    def test_member_id_and_flow_id_read_as_values(self) -> None:
        bridge = self._bridge()

        self.assertIsInstance(bridge.member_id, str)
        self.assertIsNone(bridge.flow_id)  # 还没开始任务

        bridge.start_new_flow()
        self.assertIsInstance(bridge.flow_id, str)

    def test_tool_result_fields_are_json_serializable(self) -> None:
        """照 server._result() 的取法取字段，必须能过 json.dumps。

        FastMCP 会序列化工具返回值，任何漏掉 @property 的字段都会在这里暴露。
        """
        import json

        bridge = self._bridge()
        bridge.start_new_flow()

        json.dumps({"member_id": bridge.member_id, "flow_id": bridge.flow_id})

    def test_no_public_attribute_reads_back_as_a_method(self) -> None:
        """兜底扫描：凡是 server 侧按字段读的那几个，都不能是方法对象。"""
        import inspect as _inspect

        bridge = self._bridge()
        for name in ("member_id", "flow_id", "buffer_capacity"):
            with self.subTest(attr=name):
                self.assertFalse(
                    _inspect.ismethod(getattr(bridge, name)),
                    f"{name} 读出来是方法对象——@property 漏了",
                )
