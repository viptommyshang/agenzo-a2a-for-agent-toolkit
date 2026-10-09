"""桥层：入口 form 卡的标量预填字段在提交时被兜底回传（修「列禁用卡」丢失 list_statuses 的缺口）。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``_remember_card_actions``（记下 form 卡 ``data``
的标量预填）与 ``act``（为调用方漏传的预填字段兜底）。

背景（线上实测复现）：编排器把抽取结果（如 ``list_statuses="ACTIVE,DISABLED,PENDING,FAILED,EXPIRED"``）
预填进入口 form 卡 ``payment.method-view`` 的 ``data``，但表单是**无状态**的——驱动 agent 提交时若发空
payload，编排器 capture 步回落字段默认 ``ACTIVE``，停用卡被过滤掉（只回 1 张 ACTIVE 卡）。桥在提交时
用上一张 form 卡的标量预填为漏传字段兜底（调用方显式值永远优先），闭合该缺口。只兜底**标量**、跳过
数组/对象（如 ``ride.search`` 的 ``required_fields``/``missing_required`` 这类展示用元信息）。

仅依赖标准库 ``unittest.IsolatedAsyncioTestCase``，复用姊妹测试的 ``FakeBridge`` / ``_task_json``。
"""

from __future__ import annotations

import os
import sys
import unittest

# 让本文件无论以 `python tests/xxx.py`、`python -m unittest` 还是 `pytest` 运行，都能导入子仓包
# 以及同目录的姊妹测试模块（复用其 FakeBridge / _task_json 测试替身）。
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402
from test_token_first_two_session import FakeBridge, _task_json  # noqa: E402

_ALL = "ACTIVE,DISABLED,PENDING,FAILED,EXPIRED"


def _method_view_card(list_statuses: str) -> dict:
    """``list-methods`` 入口 form 卡：data 预填 ``list_statuses``（标量）。"""
    return {
        "component": "payment.method-view",
        "kind": "form",
        "data": {"list_statuses": list_statuses},
        "actions": [{"id": "submit", "dispatch": "agent", "role": "submit"}],
    }


def _method_list_card() -> dict:
    """列表卡（非 form）：data 是 ``methods`` 数组，select-method 的 carries 取自所选行、非卡 data。"""
    return {
        "component": "payment.method-list",
        "kind": "list",
        "data": {"methods": []},
        "actions": [{"id": "select-method", "role": "item", "carries": ["id", "payment_brand"]}],
    }


def _ride_search_card() -> dict:
    """ride.search 入口 form 卡：data 全是展示用数组（非提交字段），不应被当作预填兜底。"""
    return {
        "component": "ride.search",
        "kind": "form",
        "data": {"required_fields": ["pickupName"], "missing_required": ["pickupName"]},
        "actions": [{"id": "submit", "role": "submit"}],
    }


class FormPrefillEchoTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        server._action_decls.clear()
        server._form_prefills.clear()
        self.fake = FakeBridge(member_id="test-user-036")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)
        server._action_decls.clear()
        server._form_prefills.clear()

    async def _book_method_view(self, list_statuses: str) -> str:
        self.fake.queue_text_response(200, _task_json([_method_view_card(list_statuses)]))
        booked = await server.book("列出我名下所有的卡，包括禁用的", member_id="test-user-036")
        return booked["session_id"]

    async def test_empty_submit_echoes_prefilled_list_statuses(self) -> None:
        """抽取器预填全状态串、agent 提交空 payload → 桥兜底回传全状态串（复现并修复截图缺口）。"""
        sid = await self._book_method_view(_ALL)
        self.fake.queue_action_response(200, _task_json([_method_list_card()]))
        await server.act(sid, "payment.method-view", "submit", {})
        _ctx, comp, _action, payload = self.fake.action_calls[-1]
        self.assertEqual(comp, "payment.method-view")
        self.assertEqual(payload.get("list_statuses"), _ALL)

    async def test_agent_value_overrides_prefill(self) -> None:
        """调用方显式给 list_statuses=DISABLED → 覆盖预填，不被全状态串顶掉。"""
        sid = await self._book_method_view(_ALL)
        self.fake.queue_action_response(200, _task_json([_method_list_card()]))
        await server.act(sid, "payment.method-view", "submit", {"list_statuses": "DISABLED"})
        _ctx, _comp, _action, payload = self.fake.action_calls[-1]
        self.assertEqual(payload.get("list_statuses"), "DISABLED")

    async def test_array_display_meta_not_echoed(self) -> None:
        """ride.search 的 data 全是展示用数组 → 桥不回填，空提交仍是空 payload。"""
        self.fake.queue_text_response(200, _task_json([_ride_search_card()]))
        booked = await server.book("打车", member_id="test-user-036")
        sid = booked["session_id"]
        self.fake.queue_action_response(200, _task_json([]))
        await server.act(sid, "ride.search", "submit", {})
        _ctx, _comp, _action, payload = self.fake.action_calls[-1]
        self.assertNotIn("required_fields", payload)
        self.assertNotIn("missing_required", payload)
        self.assertEqual(payload, {})

    async def test_non_form_list_card_not_treated_as_prefill(self) -> None:
        """列表卡（kind=list）不被记为 form 预填：对其发 select-method 空 payload 不被注入任何字段。"""
        self.fake.queue_text_response(200, _task_json([_method_list_card()]))
        booked = await server.book("列卡", member_id="test-user-036")
        sid = booked["session_id"]
        self.fake.queue_action_response(200, _task_json([]))
        await server.act(sid, "payment.method-list", "select-method", {})
        _ctx, _comp, _action, payload = self.fake.action_calls[-1]
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
