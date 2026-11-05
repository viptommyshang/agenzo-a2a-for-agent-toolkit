"""``lookup_*`` 四件套：细粒度只读查询工具。

## 为什么有这四个工具（而不是只有 ``lookup``）

``lookup`` 把"问一句 → 服务端自己走完三步 → 回一段文本"打成一次调用。它的两个代价是实测出来的：

1. **调用方看不到中间过程**。一次 MCP ``tools/call`` 期间宿主拿不到任何进度 —— Kiro 的
   ``tools/call`` **不带** ``progressToken``（请求里连 ``_meta`` 都没有），而 FastMCP 的
   ``Context.report_progress`` 源码是 ``if progress_token is None: return``，所以进度通知根本不发；
   ``ctx.info()`` 走的 ``notifications/message`` 发出去了，UI 也不渲染。宿主侧能力缺口，服务端改不动。
2. **服务端得先猜这句话属于哪个场景**，而本域（通用信息查询、1781 条能力）的说法穷举不完。猜错就
   走进一条取不到数的路：2026-10-30 ~ 11-02 之间为此反复补关键词与 plan 步骤，每补一轮都出新漏。

四件套把编排权交回调用方：调用方看工具描述自己决定调哪个、什么顺序、参数填什么。宿主天然会显示
每次工具调用，进度问题顺带解决；意图分类与关键词表不再参与。

## 这些测试钉住的实测前提

服务端的推进方式是**实测**确定的（2026-11-02，探针 T1–T6），不是推测：

- 「意图文本 + 尾随 JSON」能一次直达对应的 verb：``{"keyword": …}`` 落 search、
  ``{"operation_id": …}`` 落 describe；
- 卡片位置**可以跳**，不必按顺序走完前面的步；
- 但 ``{"operation_id", "params"}`` **不能**一次直达取数 —— 取数那步在等
  ``data.capability-detail#run-query`` 这个动作（服务端有意设的关卡：先读过参数规格再花钱）。
  所以 ``lookup_query`` 内部走两跳，对调用方仍是一次调用。

下面的测试把"两跳"和"尾随 JSON"这两件事钉住：它们一旦被改成别的推进方式，服务端会静默走到
另一条路上（最坏情况是停在规格卡上永远取不到数），而单测不会自己发现。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any
from unittest import mock

from agenzo_a2a_for_agent_toolkit import server


def _tool_description(name: str) -> str:
    tools = asyncio.run(server.mcp.list_tools())
    return next((t.description or "") for t in tools if t.name == name)


class _FakeBridge:
    """记录每次发送，不触网。"""

    def __init__(self, reply: str | None = None) -> None:
        self.texts: list[tuple[str, str]] = []
        self.actions: list[tuple[str, str, str, dict[str, Any]]] = []
        self.flow_ensured = 0
        self.member: str | None = None
        self._reply = reply or json.dumps({
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "history": [{
                    "role": "agent",
                    "parts": [{"kind": "data", "data": {
                        "component": "data.capability-list", "kind": "list",
                        "data": {"content": {"results": []}}, "actions": [],
                    }}],
                }],
            }
        })

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        self.texts.append((context_id, text))
        return 200, self._reply

    async def send_action(self, context_id: str, component: str, action: str,
                          payload: dict[str, Any] | None) -> tuple[int, str]:
        self.actions.append((context_id, component, action, payload or {}))
        return 200, self._reply

    def ensure_flow(self) -> None:
        self.flow_ensured += 1

    async def set_member(self, member_id: str) -> None:
        self.member = member_id

    @property
    def member_id(self) -> str:
        return self.member or "m-test"

    @property
    def flow_id(self) -> str:
        # ``_result`` 会把它放进每次返回的信封（排障时能直接报出来）
        return "flw_test"


class GranularLookupToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = _FakeBridge()
        patcher = mock.patch.object(server, "_get_bridge", return_value=self.bridge)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 1. 四个工具都注册，且与既有工具并存 ─────────────────────────
    def test_all_four_tools_are_registered_alongside_lookup(self) -> None:
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        for expected in ("lookup_categories", "lookup_search", "lookup_describe", "lookup_query"):
            self.assertIn(expected, names)
        # ``lookup`` 与 ``book`` 保持不动 —— 新增工具不替换既有入口
        self.assertIn("lookup", names)
        self.assertIn("book", names)

    # ── 2. 推进方式：尾随 JSON（实测前提，不能改成别的写法）────────
    def test_search_uses_the_run_query_opening_text(self) -> None:
        """search 的开场必须用命中 run_query 的那句 —— 落错 scenario 会拿回别的卡。"""
        asyncio.run(server.lookup_search("crypto price"))
        _ctx, text = self.bridge.texts[0]
        self.assertTrue(text.startswith(server._OPEN_RUN_QUERY), text[:60])

    def test_describe_uses_the_run_query_opening_text(self) -> None:
        asyncio.run(server.lookup_describe("get_x"))
        _ctx, text = self.bridge.texts[0]
        self.assertTrue(text.startswith(server._OPEN_RUN_QUERY), text[:60])

    def test_search_sends_keyword_as_trailing_json(self) -> None:
        """``lookup_search`` 必须把 keyword 作为**尾随 JSON** 发出。

        服务端解析消息末尾的 JSON 对象作为结构化输入；换成自然语言描述（"search for X"）就等于把
        判断权交回服务端的猜测 —— 那正是四件套要避免的。
        """
        asyncio.run(server.lookup_search("crypto price"))
        self.assertEqual(len(self.bridge.texts), 1)
        _ctx, text = self.bridge.texts[0]
        tail = text[text.index("{"):]
        self.assertEqual(json.loads(tail), {"keyword": "crypto price"})

    def test_search_passes_limit_only_when_not_default(self) -> None:
        asyncio.run(server.lookup_search("news search", limit=5))
        _ctx, text = self.bridge.texts[0]
        self.assertEqual(json.loads(text[text.index("{"):]),
                         {"keyword": "news search", "limit": 5})

    def test_describe_sends_operation_id_as_trailing_json(self) -> None:
        asyncio.run(server.lookup_describe("get_coingecko_simple_price"))
        _ctx, text = self.bridge.texts[0]
        self.assertEqual(json.loads(text[text.index("{"):]),
                         {"operation_id": "get_coingecko_simple_price"})

    def test_categories_opens_then_submits_empty_payload(self) -> None:
        """``lookup_categories`` 走两跳：开场渲染入口卡，再回传 **空 payload** 的 submit。

        实测 2026-11-02（探针 B）：单发开场文本只拿到入口卡，必须再回传 submit 才到类别卡（16 个）。
        payload **必须为空** —— 带上 keyword 就会让 plan 跳过"列类别"直接去搜索
        （``categories`` 步声明了 ``only_if: "!keyword"``）。
        """
        asyncio.run(server.lookup_categories())
        self.assertEqual(len(self.bridge.texts), 1, "第一跳：开场文本")
        _ctx, text = self.bridge.texts[0]
        self.assertEqual(json.loads(text[text.index("{"):]), {})
        self.assertIn(server._OPEN_CATEGORIES, text)
        self.assertEqual(len(self.bridge.actions), 1, "第二跳：submit")
        _c, component, action, payload = self.bridge.actions[0]
        self.assertEqual((component, action), ("data.lookup", "submit"))
        self.assertEqual(payload, {}, "payload 必须为空，否则跳过列类别")

    def test_opening_texts_match_scenario_keywords(self) -> None:
        """开场文本必须**逐字**取自 schema 的 ``intent_keywords`` —— 它决定落哪条 plan。

        实测 2026-11-02：原先 describe 工具写的是 "show the parameter spec for this capability"，
        而 "parameter spec for" 刚从 ``find_capability`` 的关键词表里删掉，于是这句谁都不命中、
        走兜底落到 ``browse_categories``，describe 拿回了**类别卡**。开场文本是契约的一部分。
        """
        import json as _json
        import pathlib as _pathlib

        schema = _json.loads(_pathlib.Path(
            r"e:\work\gitlab-projects\ai-payment\agenzo-agent-orchestrator-base"
            r"\.worktrees\aisa-data-provider\app\schemas\refactor\data-aisa.json"
        ).read_text(encoding="utf-8-sig"))
        scen = schema["scenarios"]
        self.assertIn(server._OPEN_RUN_QUERY,
                      scen["run_query"]["intent_keywords"]["en"],
                      "run_query 的开场文本不在它的关键词表里")
        self.assertIn(server._OPEN_CATEGORIES,
                      scen["browse_categories"]["intent_keywords"]["en"],
                      "browse_categories 的开场文本不在它的关键词表里")

    # ── 3. lookup_query 的两跳（最容易被"优化"掉的那个细节）─────────
    def test_query_walks_the_spec_card_then_posts_run_query(self) -> None:
        """``lookup_query`` 必须**先到规格卡、再回传 run-query**。

        实测（T6）：「文本 + {operation_id, params}」**不能**一次直达取数 —— 取数那步在等
        ``data.capability-detail#run-query``。若有人把这里"优化"成一跳，流程会停在规格卡上，
        调用方永远拿不到数据，而且不会报错。
        """
        params = {"ids": "bitcoin", "vs_currencies": "usd"}
        asyncio.run(server.lookup_query("get_coingecko_simple_price", params))

        # 第一跳：尾随 JSON 到规格卡
        self.assertEqual(len(self.bridge.texts), 1, "第一跳必须是尾随 JSON 的文本消息")
        _ctx, text = self.bridge.texts[0]
        self.assertEqual(json.loads(text[text.index("{"):]),
                         {"operation_id": "get_coingecko_simple_price", "params": params})

        # 第二跳：回传规格卡的 run-query
        self.assertEqual(len(self.bridge.actions), 1, "第二跳必须回传 run-query 动作")
        _ctx2, component, action, payload = self.bridge.actions[0]
        self.assertEqual(component, "data.capability-detail")
        self.assertEqual(action, "run-query")
        self.assertEqual(payload, {"operationId": "get_coingecko_simple_price", "params": params})

    def test_query_two_hops_share_one_context(self) -> None:
        """两跳必须在**同一个** contextId 里 —— 换会话等于把规格卡那一步丢掉。"""
        asyncio.run(server.lookup_query("get_x", {"a": 1}))
        self.assertEqual(self.bridge.texts[0][0], self.bridge.actions[0][0])

    def test_query_stops_after_first_hop_on_error(self) -> None:
        """第一跳失败（如 operation_id 不存在）就不该再发取数动作 —— 否则白花一次钱。"""
        class _FailFirst(_FakeBridge):
            async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
                self.texts.append((context_id, text))
                return 400, json.dumps({"error": {"code": "INVALID_REQUEST"}})

        bridge = _FailFirst()
        with mock.patch.object(server, "_get_bridge", return_value=bridge):
            asyncio.run(server.lookup_query("no_such_op", {}))
        self.assertEqual(len(bridge.texts), 1)
        self.assertEqual(len(bridge.actions), 0, "第一跳失败后不得发起取数")

    # ── 4. 会话语义：旁路 flow、独立 context ────────────────────────
    def test_each_tool_ensures_flow_rather_than_starting_a_new_one(self) -> None:
        """查询是**旁路**：沿用当前 flow_id，不开新旅程。

        若在这里 ``start_new_flow``，用户在订酒店途中问一句汇率就会把订单的前后步骤断在两条旅程上，
        「按 flow_id 查一笔订单的全链路」当场失效。
        """
        for call in (
            lambda: server.lookup_categories(),
            lambda: server.lookup_search("crypto price"),
            lambda: server.lookup_describe("get_x"),
        ):
            self.bridge.flow_ensured = 0
            asyncio.run(call())
            self.assertEqual(self.bridge.flow_ensured, 1)

    def test_sessions_are_kind_lookup_and_isolated_from_book(self) -> None:
        out = asyncio.run(server.lookup_search("crypto price"))
        sid = out["session_id"]
        self.assertEqual(server._sessions[sid]["kind"], "lookup")

    def test_member_id_override_is_applied(self) -> None:
        asyncio.run(server.lookup_search("crypto price", member_id="prod-user-9"))
        self.assertEqual(self.bridge.member, "prod-user-9")

    # ── 5. 描述不得撒谎（本仓踩过两次，都靠反向断言抓到）───────────
    def test_search_description_demands_english_and_explains_why(self) -> None:
        """``keyword`` 必须英文，且描述要讲明后果 —— 这是本域最常见的失败。

        实测 2026-11-02：抽取器把 keyword 填成中文公司名，``search`` 返回 **0 条**候选，链路断掉。
        现在编排权在调用方，这条约束只能靠工具描述传达，所以它必须写明白。
        """
        desc = _tool_description("lookup_search")
        self.assertIn("MUST BE ENGLISH", desc)
        self.assertIn("ZERO hits", desc)
        # 还要讲明"具体主体名不放 keyword"——那正是抽取器搞混的地方
        self.assertIn("never the specific subject", desc.lower().replace("**", "")
                      if "never the specific subject" in desc.lower().replace("**", "")
                      else desc)

    def test_describe_description_warns_that_wrong_params_still_bill(self) -> None:
        desc = _tool_description("lookup_describe")
        self.assertIn("CASE-SENSITIVE", desc)
        self.assertIn("still billed", desc)

    def test_query_description_states_it_is_billed_and_passthrough(self) -> None:
        desc = _tool_description("lookup_query")
        self.assertIn("BILLED PER CALL", desc)
        self.assertIn("passed through unchanged", desc)
        # cost_usd 是成本真相
        self.assertIn("cost_usd", desc)
        # 读结果的那条约定（url 常是首页，内容在摘要字段里）
        self.assertIn("summary field", desc)

    def test_free_tools_say_they_are_free(self) -> None:
        for name in ("lookup_categories", "lookup_search", "lookup_describe"):
            self.assertIn("FREE", _tool_description(name), f"{name} 应标明免费")

    def test_no_tool_claims_a_confirmation_gate(self) -> None:
        """四件套都不得声称"付费前会停下来确认" —— 没有那道门。

        ``lookup`` 的描述曾经这么写过，调用方据此以为钱会被拦一下。这里反向锁住。
        """
        for name in ("lookup_categories", "lookup_search", "lookup_describe", "lookup_query"):
            desc = _tool_description(name)
            self.assertNotIn("stops for confirmation", desc)
            self.assertNotIn("confirmation gate", desc)


if __name__ == "__main__":
    unittest.main()
