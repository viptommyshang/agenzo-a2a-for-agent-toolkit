"""桥层：代下单 ``checkout`` / ``checkout_status`` 工具。

锁住：
- ``checkout`` 开新会话，以**结构化动作** ``checkout.setup#submit`` 发送链接、收货信息和卡（卡不走自然语言）；
- ``checkout_status`` 以 ``checkout.lookup#submit`` 发 task_id；
- ``checkout.confirm#confirm`` 不被当成扣款结算动作（不附令牌/默认卡软引导）。
"""

from __future__ import annotations

import inspect
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
        out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD)
        self.assertEqual(self.fake.text_calls, [])  # 卡不走自然语言
        _ctx, comp, action, payload = self.fake.action_calls[-1]
        self.assertEqual((comp, action), ("checkout.setup", "submit"))
        self.assertEqual(payload["checkout_url"], "https://merchant.example.com/cart/1:1")
        self.assertEqual(payload["shipping"]["zip_code"], "10001")
        self.assertEqual(payload["card"]["number"], _CARD["number"])
        self.assertEqual(out["cards"][-1]["component"], "checkout.confirm")
        self.assertEqual(self.fake.flow_calls, ["start_new"])

    async def test_checkout_generates_a_unique_key_per_call_and_returns_it(self) -> None:
        keys = []
        for _ in range(2):
            self.fake.queue_action_response(200, _task_json([_confirm_card()]))
            out = await server.checkout("https://www.merchant.example.com/cart/1:1", _SHIPPING, _CARD)
            sent = self.fake.action_calls[-1][3]["idempotency_key"]
            self.assertEqual(out["idempotency_key"], sent)  # 返回里带出同一个键
            self.assertTrue(sent.startswith("merchant-example-com-"))
            keys.append(sent)
        self.assertNotEqual(keys[0], keys[1])  # 同卡同链接同地址，两次也是两个键
        for k in keys:
            self.assertNotIn("1111", k)

    def test_checkout_no_longer_takes_an_idempotency_key_parameter(self) -> None:
        self.assertNotIn("idempotency_key", inspect.signature(server.checkout).parameters)

    async def test_act_resubmit_keeps_the_generated_key(self) -> None:
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD)
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        await server.act(out["session_id"], "checkout.setup", "submit", {"checkout_url": "https://merchant.example.com/cart/1:1"})
        self.assertEqual(self.fake.action_calls[-1][3]["idempotency_key"], out["idempotency_key"])
        # 模型自己编的键也不会生效：以工具生成的为准
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        await server.act(out["session_id"], "checkout.setup", "submit", {"idempotency_key": "made-up-1"})
        self.assertEqual(self.fake.action_calls[-1][3]["idempotency_key"], out["idempotency_key"])

    async def test_created_at_is_shown_as_local_time(self) -> None:
        from datetime import datetime, timezone

        epoch = datetime(2026, 11, 19, 9, 9, 59, tzinfo=timezone.utc).timestamp()
        local = datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M")
        card = {
            "component": "checkout.result", "kind": "detail", "actions": [],
            "data": {
                "task_id": "ckout_abc", "state": "completed", "success": True, "status": "payment-reached",
                "replayed": True, "created_at": epoch,
                "errors": ["This is the result of the order submitted at 2026-11-19T09:09:59+00:00 with the same "
                           "idempotency key; no new order was placed this time."],
            },
        }
        self.fake.queue_action_response(200, _task_json([card]))
        out = await server.checkout_status("ckout_abc")
        data = out["cards"][-1]["data"]
        self.assertEqual(data["created_at"], local)
        self.assertIn(f"submitted at {local} with", data["errors"][0])
        self.assertNotIn("+00:00", data["errors"][0])

    async def test_confirm_is_not_treated_as_money_settle(self) -> None:
        self.fake.queue_action_response(200, _task_json([_confirm_card()]))
        out = await server.checkout("https://merchant.example.com/cart/1:1", _SHIPPING, _CARD)
        self.fake.queue_action_response(200, _task_json([_await_card()]))
        res = await server.act(out["session_id"], "checkout.confirm", "confirm", {})
        for key in ("default_orchestration", "payment_warning", "evo_orchestration"):
            self.assertNotIn(key, res)
        self.assertEqual(res["cards"][-1]["component"], "checkout.await")

    def test_checkout_docstring_keeps_card_data_out_of_the_key_and_explains_the_result(self) -> None:
        doc = inspect.getdoc(server.checkout) or ""
        self.assertIn("GENERATED BY THIS TOOL", doc)
        self.assertIn("NEVER make one up", doc)
        self.assertNotIn("REQUIRED, chosen by YOU", doc)
        self.assertIn("LOCAL time (never", doc)
        self.assertIn("errors[0]", doc)
        self.assertIn("order_id`` present", doc)
        self.assertIn("NOT retry", doc)
        self.assertIn("ONLY when the order was really placed", " ".join(doc.split()))
        self.assertIn("IDEMPOTENCY_CONFLICT", doc)
        self.assertIn("``replayed: true``", doc)
        self.assertIn("这次没有重新下单", doc)
        self.assertNotIn("cardLast4", doc)

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
