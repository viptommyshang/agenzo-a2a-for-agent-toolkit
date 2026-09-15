"""工具输出瘦身单测（denylist 字段投影 + 不透明 token 句柄化）。

覆盖对象：``a2a.normalize_task`` 的投影，以及 ``server`` 侧会话内 token vault 的往返。

要点（对应设计的低风险三原则）：
  - **carries-safe**：投影只丢重内容字段（媒体/长文/政策/行李表），绝不丢 action ``carries`` 点名的
    id/token 字段；长 token 是「换句柄」（可往返）而非「丢弃」，所以推进流程所需字段一个不少。
  - **round-trippable**：长不透明 token 在模型可见视图里显示为短句柄 ``@tokN``；``act`` 提交前由会话
    vault 换回真实 token 上 wire —— 编排器始终收到真实值。
  - **瘦的是模型视图**：关闭开关（heavy_keys=frozenset / 无 mint）即回到原样全量，向后兼容。

说明：子仓未安装 pytest / pytest-asyncio，本测试仅依赖标准库 ``unittest``
（``IsolatedAsyncioTestCase`` 原生支持 async），亦可被 pytest 直接收集。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from collections import deque
from typing import Any

# 让本文件无论以 python / unittest / pytest 运行都能导入子仓包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenzo_a2a_for_agent_toolkit import config as cfg  # noqa: E402
from agenzo_a2a_for_agent_toolkit import server  # noqa: E402
from agenzo_a2a_for_agent_toolkit.a2a import normalize_task  # noqa: E402

# 一个 >=64 字符、纯 token 字符集的不透明串（模拟 product_token / pt_…）。
LONG_TOKEN = "pt_" + "Ab9_" * 40  # 163 chars
LONG_TOKEN2 = "pt_" + "Zz0-" * 40


# ─────────────────────────────────────────────────────────────────────────────
# 卡片/任务构造助手
# ─────────────────────────────────────────────────────────────────────────────
def _task(
    cards: list[dict[str, Any]] | None = None,
    texts: list[str] | None = None,
    state: str = "input-required",
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for t in texts or []:
        parts.append({"kind": "text", "text": t})
    for c in cards or []:
        parts.append({"kind": "data", "data": c})
    history = [{"role": "agent", "parts": parts}] if parts else []
    return {"kind": "task", "status": {"state": state}, "history": history}


def _rpc(task: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": "t", "result": task})


def _offer_card(token: str = LONG_TOKEN) -> dict[str, Any]:
    """flight.offer-list：offer 携 carries 字段 product_token(长) + 决策字段 + 重字段 baggage_rules。"""
    return {
        "component": "flight.offer-list",
        "kind": "list",
        "data": {
            "offers": [
                {
                    "product_token": token,
                    "total_sale_price": 122,
                    "currency": "USD",
                    "ticketing_airline": "3U",
                    "cabin_class": ["economy"],
                    "baggage_rules": [{"check_in": {"weight": 20}, "blob": "x" * 200}],
                }
            ]
        },
        "actions": [
            {"role": "item", "id": "select", "dispatch": "agent", "carries": ["product_token"]},
            {"role": "item", "id": "load-more", "dispatch": "agent", "carries": ["product_token"]},
        ],
    }


def _hotel_detail_card() -> dict[str, Any]:
    """hotel.detail：大量媒体/长文/房型图片(重字段) + carries 字段 product_token。"""
    return {
        "component": "hotel.detail",
        "kind": "detail",
        "data": {
            "hotelId": 123,
            "hotel": {
                "hotel_id": 123,
                "hotel_name": "The Test House",
                "intro": "y" * 800,
                "appearance_image": "https://img/x.jpg",
                "images": [{"url": f"https://img/{i}.jpg"} for i in range(25)],
                "facilities": ["f"] * 30,
                "rooms": [
                    {"room_id": 1, "room_name": "R1", "images": [{"url": "u"} for _ in range(12)]}
                ],
            },
            "rooms": [
                {
                    "product_token": LONG_TOKEN,
                    "room_name": "Royal",
                    "tips": [{"title": "t", "text": "z" * 400}],
                }
            ],
        },
        "actions": [{"role": "item", "id": "select-room", "dispatch": "agent", "carries": ["product_token"]}],
    }


def _confirm_card() -> dict[str, Any]:
    """flight.booking-confirm：confirm 动作零 carries(token 在服务端);data.productToken 纯展示。"""
    return {
        "component": "flight.booking-confirm",
        "kind": "confirm",
        "data": {"totalAmount": 84, "currency": "USD", "productToken": LONG_TOKEN},
        "actions": [{"role": "confirm", "id": "confirm", "dispatch": "agent"}],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 纯 normalize_task 投影行为
# ─────────────────────────────────────────────────────────────────────────────
class ProjectionTest(unittest.TestCase):
    def test_heavy_keys_dropped_decision_and_carries_kept(self) -> None:
        counter = {"n": 0}

        def mint(_real: str) -> str:
            counter["n"] += 1
            return f"@tok{counter['n']}"

        out = normalize_task(_task(cards=[_offer_card()]), mint_handle=mint)
        offer = out["cards"][0]["data"]["offers"][0]
        # 重字段丢弃
        self.assertNotIn("baggage_rules", offer)
        # 决策字段保留
        self.assertEqual(offer["total_sale_price"], 122)
        self.assertEqual(offer["ticketing_airline"], "3U")
        # carries 字段(product_token)保留 —— 只是换成句柄，未被丢弃
        self.assertEqual(offer["product_token"], "@tok1")
        # 动作契约(carries)完好
        select = next(a for a in out["cards"][0]["actions"] if a["id"] == "select")
        self.assertEqual(select["carries"], ["product_token"])

    def test_flight_fare_rules_dropped_decision_and_carries_kept(self) -> None:
        """flight.offer-list 每 offer 的运价改退规则(refund_rules/change_rules/*_original_text)是
        选择无关的冗长政策文本 —— 应被投影丢弃;而决策字段(价格/航司/舱位)与 carries(product_token)
        必须保留(token 换句柄、非丢弃)。"""
        offer = {
            "product_token": LONG_TOKEN,
            "total_sale_price": 106,
            "currency": "USD",
            "ticketing_airline": "MU",
            "cabin_class": ["economy"],
            "identifier": "MU5111",
            "price_key_ready": True,
            # 冗长、选择无关的政策文本(应丢)
            "refund_rules": [{"rules": [{"fee": 100, "text": "z" * 300}]}],
            "change_rules": [{"rules": [{"fee": 50, "text": "y" * 300}]}],
            "refund_original_text": "退票规则原文 " + "x" * 400,
            "change_original_text": "改期规则原文 " + "w" * 400,
            "baggage_rules": [{"check_in": {"weight": 20}, "blob": "b" * 200}],
        }
        card = {
            "component": "flight.offer-list",
            "kind": "list",
            "data": {"offers": [offer]},
            "actions": [{"role": "item", "id": "select", "dispatch": "agent", "carries": ["product_token"]}],
        }

        def mint(_real: str) -> str:
            return "@tok1"

        out = normalize_task(_task(cards=[card]), mint_handle=mint)
        got = out["cards"][0]["data"]["offers"][0]
        # 冗长政策/行李字段丢弃
        for k in ("refund_rules", "change_rules", "refund_original_text", "change_original_text", "baggage_rules"):
            self.assertNotIn(k, got, f"{k} 应被投影丢弃")
        # 决策/选择字段保留
        self.assertEqual(got["total_sale_price"], 106)
        self.assertEqual(got["ticketing_airline"], "MU")
        self.assertEqual(got["identifier"], "MU5111")
        self.assertEqual(got["price_key_ready"], True)
        # carries 字段(product_token)保留 —— 换句柄、未丢
        self.assertEqual(got["product_token"], "@tok1")

    def test_short_ids_pass_through(self) -> None:
        card = {
            "component": "hotel.order-detail",
            "kind": "detail",
            "data": {"order_id": "hho_ISO1", "charge_no": "chg_ABC123", "amount_minor": 4280},
            "actions": [{"id": "pay", "carries": ["order_id"]}],
        }

        def mint(_r: str) -> str:  # 不该被调用
            raise AssertionError("short id must not be handle-ized")

        out = normalize_task(_task(cards=[card]), mint_handle=mint)
        data = out["cards"][0]["data"]
        self.assertEqual(data["order_id"], "hho_ISO1")
        self.assertEqual(data["charge_no"], "chg_ABC123")
        self.assertEqual(data["amount_minor"], 4280)

    def test_same_token_same_handle(self) -> None:
        seen: dict[str, str] = {}

        def mint(real: str) -> str:
            if real not in seen:
                seen[real] = f"@tok{len(seen) + 1}"
            return seen[real]

        card = _offer_card()
        card["data"]["offers"].append(dict(card["data"]["offers"][0]))  # 同 token 出现两次
        out = normalize_task(_task(cards=[card]), mint_handle=mint)
        offers = out["cards"][0]["data"]["offers"]
        self.assertEqual(offers[0]["product_token"], offers[1]["product_token"])
        self.assertEqual(len(seen), 1)

    def test_text_is_scrubbed_to_handle(self) -> None:
        def mint(_r: str) -> str:
            return "@tok1"

        task = _task(
            cards=[_offer_card()],
            texts=[f"[Flight Offer List]\n  - Product Token: {LONG_TOKEN}"],
        )
        out = normalize_task(task, mint_handle=mint)
        self.assertNotIn(LONG_TOKEN, out["text"])
        self.assertIn("@tok1", out["text"])

    def test_no_mint_keeps_tokens_verbatim_but_still_drops_heavy(self) -> None:
        out = normalize_task(_task(cards=[_offer_card()]))  # 无 mint_handle
        offer = out["cards"][0]["data"]["offers"][0]
        self.assertEqual(offer["product_token"], LONG_TOKEN)  # token 原样
        self.assertNotIn("baggage_rules", offer)  # 重字段仍丢弃(默认 denylist)

    def test_empty_heavy_keys_disables_dropping(self) -> None:
        out = normalize_task(_task(cards=[_offer_card()]), heavy_keys=frozenset())
        offer = out["cards"][0]["data"]["offers"][0]
        self.assertIn("baggage_rules", offer)  # 关闭丢弃 → 原样全量

    def test_projection_reduces_size(self) -> None:
        raw = _hotel_detail_card()
        full = normalize_task(_task(cards=[raw]), heavy_keys=frozenset())
        slim = normalize_task(_task(cards=[raw]), mint_handle=lambda _r: "@tok1")
        full_len = len(json.dumps(full["cards"][0]["data"], ensure_ascii=False))
        slim_len = len(json.dumps(slim["cards"][0]["data"], ensure_ascii=False))
        self.assertLess(slim_len, full_len // 2)  # 至少砍掉一半

    def test_booking_confirm_zero_carries(self) -> None:
        out = normalize_task(_task(cards=[_confirm_card()]), mint_handle=lambda _r: "@tok1")
        card = out["cards"][0]
        # 展示用长 token 句柄化，不影响 confirm 动作(零 carries)
        self.assertEqual(card["data"]["productToken"], "@tok1")
        self.assertEqual(card["data"]["totalAmount"], 84)
        confirm = card["actions"][0]
        self.assertEqual(confirm["id"], "confirm")
        self.assertNotIn("carries", confirm)  # 零 carries：服务端持有 token


# ─────────────────────────────────────────────────────────────────────────────
# server 侧会话内 token vault 往返
# ─────────────────────────────────────────────────────────────────────────────
class FakeBridge:
    """记录被测工具对 A2A bridge 的调用；不做网络交互。"""

    def __init__(self, member_id: str = "m-test") -> None:
        self._member_id = member_id
        self.action_calls: list[tuple[str, str, str, dict]] = []
        self._text_responses: deque[tuple[int, str]] = deque()
        self._action_responses: deque[tuple[int, str]] = deque()

    @property
    def member_id(self) -> str:
        return self._member_id

    async def set_member(self, member_id: str) -> None:
        if (member_id or "").strip():
            self._member_id = member_id.strip()

    def queue_text_response(self, status: int, raw: str) -> None:
        self._text_responses.append((status, raw))

    def queue_action_response(self, status: int, raw: str) -> None:
        self._action_responses.append((status, raw))

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        if self._text_responses:
            return self._text_responses.popleft()
        return 200, _rpc(_task())

    async def send_action(
        self, context_id: str, component: str, action: str, payload: dict[str, Any] | None
    ) -> tuple[int, str]:
        self.action_calls.append((context_id, component, action, dict(payload or {})))
        if self._action_responses:
            return self._action_responses.popleft()
        return 200, _rpc(_task())


class VaultRoundTripTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        self._orig_vaults = dict(server._token_vaults)
        self._orig_flag = cfg.PROJECT_TOOL_OUTPUT
        server._sessions.clear()
        server._token_vaults.clear()
        cfg.PROJECT_TOOL_OUTPUT = True
        self.fake = FakeBridge()
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)
        server._token_vaults.clear()
        server._token_vaults.update(self._orig_vaults)
        cfg.PROJECT_TOOL_OUTPUT = self._orig_flag

    async def test_handle_minted_then_resolved_to_real_token_on_act(self) -> None:
        # book 的响应里带 offer-list 卡（含长 product_token）→ normalize 铸句柄 @tok1
        self.fake.queue_text_response(200, _rpc(_task(cards=[_offer_card()])))
        booking = await server.book("book a flight from Shanghai to Beijing")
        sid = booking["session_id"]
        shown = booking["cards"][0]["data"]["offers"][0]["product_token"]
        self.assertEqual(shown, "@tok1")  # 模型看到的是句柄，不是 800 字符 blob

        # 模型照它看到的句柄提交 select → act 提交前应换回真实 token 上 wire
        await server.act(sid, "flight.offer-list", "select", {"product_token": "@tok1"})
        _ctx, comp, act_id, sent = self.fake.action_calls[-1]
        self.assertEqual((comp, act_id), ("flight.offer-list", "select"))
        self.assertEqual(sent["product_token"], LONG_TOKEN)  # 编排器收到真实 token

    async def test_unknown_or_absent_handle_passes_through(self) -> None:
        # 未铸任何句柄的会话：payload 原样透传，不误伤
        booking = await server.book("book a flight")
        sid = booking["session_id"]
        await server.act(sid, "flight.offer-list", "select", {"product_token": "pt_plain_value"})
        sent = self.fake.action_calls[-1][3]
        self.assertEqual(sent["product_token"], "pt_plain_value")

    async def test_projection_off_keeps_full_payload(self) -> None:
        cfg.PROJECT_TOOL_OUTPUT = False
        self.fake.queue_text_response(200, _rpc(_task(cards=[_offer_card()])))
        booking = await server.book("book a flight")
        offer = booking["cards"][0]["data"]["offers"][0]
        self.assertEqual(offer["product_token"], LONG_TOKEN)  # 关闭时原样
        self.assertIn("baggage_rules", offer)


if __name__ == "__main__":
    unittest.main()
