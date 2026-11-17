"""桥层：代下单 ``checkout`` / ``checkout_status`` 工具。

锁住：
- ``checkout`` 开新会话，以**结构化动作** ``checkout.setup#submit`` 发送链接、收货信息和卡（卡不走自然语言）；
- ``checkout_status`` 以 ``checkout.lookup#submit`` 发 task_id；
- ``checkout.confirm#confirm`` 不被当成扣款结算动作（不附令牌/默认卡软引导）。
"""

from __future__ import annotations

import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402
from test_token_first_two_session import FakeBridge, _task_json  # noqa: E402

_SHIPPING = {
    "email": "buyer@example.com", "first_name": "Test", "last_name": "Buyer", "phone": "2125550123",
    "country": "US", "address1": "20 W 34th St", "city": "New York", "state": "NY", "zip_code": "10001",
}
# 测试卡号运行时拼出，代码里不出现完整卡号字面量。
_CARD = {"number": "4111" + "1" * 12, "expiry": "12/30", "cvv": "123", "name": "Test Buyer"}


def _confirm_card() -> dict:
    return {
        "component": "checkout.confirm",
        "kind": "confirm",
        "data": {"merchant": "merchant.example.com", "cardLast4": "1111", "shipping": _SHIPPING},
        "actions": [
            {"role": "confirm", "id": "confirm", "dispatch": "agent"},
            {"role": "action", "id": "edit", "dispatch": "agent"},
            {"role": "action", "id": "cancel", "dispatch": "agent", "ends_flow": True},
        ],
    }


def _await_card() -> dict:
    return {
        "component": "checkout.await",
        "kind": "status",
        "data": {"task_id": "ckout_abc", "state": "accepted"},
        "actions": [{"role": "action", "id": "poll", "dispatch": "agent", "carries": ["task_id"]}],
    }


class CheckoutToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        server._action_decls.clear()
        server._form_prefills.clear()
        self.fake = FakeBridge(member_id="member-a")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)
        server._action_decls.clear()
        server._form_prefills.clear()

    async def test_checkout_sends_structured_setup_submit(self) -> None:
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD, "order-1")
        self.assertEqual(self.fake.text_calls, [])  # 卡不走自然语言
        _ctx, comp, action, payload = self.fake.action_calls[-1]
        self.assertEqual((comp, action), ("checkout.setup", "submit"))
        self.assertEqual(payload["checkout_url"], "https://merchant.example.com/cart/1:1")
        self.assertEqual(payload["shipping"]["zip_code"], "10001")
        self.assertEqual(payload["card"]["number"], _CARD["number"])
        self.assertEqual(out["cards"][-1]["component"], "checkout.confirm")
        self.assertEqual(self.fake.flow_calls, ["start_new"])

    async def test_checkout_passes_through_caller_idempotency_key(self) -> None:
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD, idempotency_key=" order-42 ")
        # 原样透传，不 strip、不改写
        self.assertEqual(self.fake.action_calls[-1][3]["idempotency_key"], " order-42 ")

    async def test_checkout_resend_keeps_same_key(self) -> None:
        for _ in range(2):
            self.fake.queue_action_response(200, _task_json([_confirm_card()]))
            await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD, idempotency_key="order-42")
        keys = [c[3]["idempotency_key"] for c in self.fake.action_calls]
        self.assertEqual(keys, ["order-42", "order-42"])

    async def test_checkout_rejects_missing_or_blank_idempotency_key(self) -> None:
        with self.assertRaises(TypeError):  # 不传：必填参数，没有默认值
            await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD)  # type: ignore[call-arg]
        for blank in ("", "   ", "\t\n"):
            out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD, idempotency_key=blank)
            self.assertIn("idempotency_key", out["error"])
        self.assertEqual(self.fake.action_calls, [])  # 没有发任何动作
        self.assertEqual(self.fake.flow_calls, [])  # 也没有开新流程

    async def test_confirm_is_not_treated_as_money_settle(self) -> None:
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD, "order-1")
        self.fake.queue_action_response(200, _task_json([_await_card()]))
        res = await server.act(out["session_id"], "checkout.confirm", "confirm", {})
        for key in ("default_orchestration", "payment_warning", "evo_orchestration"):
            self.assertNotIn(key, res)
        self.assertEqual(res["cards"][-1]["component"], "checkout.await")

    async def test_checkout_status_sends_lookup(self) -> None:
        self.fake.queue_action_response(200, _task_json([_await_card()]))
        out = await server.checkout_status("ckout_abc")
        _ctx, comp, action, payload = self.fake.action_calls[-1]
        self.assertEqual((comp, action, payload), ("checkout.lookup", "submit", {"task_id": "ckout_abc"}))
        self.assertEqual(out["cards"][-1]["component"], "checkout.await")


if __name__ == "__main__":
    unittest.main()


class TraceRedactionTest(unittest.TestCase):
    def test_debug_record_masks_card_but_wire_body_untouched(self) -> None:
        from agenzo_a2a_for_agent_toolkit import config as cfg
        from agenzo_a2a_for_agent_toolkit.a2a import A2ABridge

        bridge = A2ABridge(cfg)
        body = {"params": {"message": {"parts": [{"kind": "data", "data": {
            "component": "checkout.setup", "action": "submit", "payload": {"card": dict(_CARD)}}}]}}}
        rec = bridge._record(kind="action", transport="send", method="POST", url="u",
                             request_body=body, status=200, response_text="{}")
        logged = rec["request"]["params"]["message"]["parts"][0]["data"]["payload"]["card"]
        self.assertEqual(logged["number"], "****" + _CARD["number"][-4:])
        self.assertEqual((logged["cvv"], logged["expiry"]), ("***", "***"))
        wire = body["params"]["message"]["parts"][0]["data"]["payload"]["card"]
        self.assertEqual(wire["number"], _CARD["number"])
