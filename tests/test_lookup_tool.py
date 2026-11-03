"""``lookup`` —— 只读信息查询入口（2026-10-28 新增）。

被测对象：``agenzo_a2a_for_agent_toolkit.server`` 的 ``lookup`` MCP 工具，以及它与既有
``book`` 的分工。

为什么是**新增**而不是把 ``book`` 改名：``book`` 这个名字已经进了对外交付文档
（``docs/a2a-docs/index.html`` 的 MCP 工具表与 ``autoApprove`` 示例），改名会牵动那份文档的
一次发版，也会打断所有既有调用方。新增一个入口则零破坏，而且两个 tool 的描述互斥
（事务 vs 只读查询），模型选起来比"一个万能入口 + 一长段别被名字误导"更容易选对。

覆盖的四件事：
  1. ``lookup`` 与 ``book`` **同时存在**（回归"新增没被做成改名"）；
  2. ``lookup`` 开自己的会话，``kind == "lookup"``，与 booking / payment 两类隔离；
  3. ``lookup`` 调 ``ensure_flow`` 而**不是** ``start_new_flow``——一次旁路查询不得换掉
     进行中订单的 ``flow_id``（否则"按 flow_id 查一笔订单全链路"当场失效）；
  4. 请求原文整体透传，不做摘要或裁剪。

只依赖标准库 ``unittest``（与同目录其它用例一致，子仓的 pytest 也能直接收集本文件）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402

_FLOW = "flw_00000000000000000000000000000001"


def _task_json(state: str = "input-required") -> str:
    """最小可解析的 A2A 阻塞式 JSON-RPC 响应。"""
    task = {"kind": "task", "status": {"state": state}, "history": []}
    return json.dumps({"jsonrpc": "2.0", "id": "test", "result": task})


class FakeBridge:
    """只实现被测工具会用到的接口；零网络交互。``flow_calls`` 是本文件的核心观测点。"""

    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str]] = []   # (context_id, text)
        self.set_member_calls: list[str] = []
        self.flow_calls: list[str] = []               # "start_new" / "ensure"
        self._flow_id: str | None = None

    @property
    def member_id(self) -> str:
        return "m-default"

    async def set_member(self, member_id: str) -> None:
        self.set_member_calls.append(member_id)

    def start_new_flow(self) -> str:
        self.flow_calls.append("start_new")
        self._flow_id = _FLOW + "x"
        return self._flow_id

    def ensure_flow(self) -> str:
        self.flow_calls.append("ensure")
        if self._flow_id is None:
            self._flow_id = _FLOW
        return self._flow_id

    @property
    def flow_id(self) -> str | None:
        return self._flow_id

    async def send_text(self, context_id: str, text: str) -> tuple[int, str]:
        self.text_calls.append((context_id, text))
        return 200, _task_json()


class LookupToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.fake = FakeBridge()
        server._bridge = self.fake  # type: ignore[assignment]
        server._sessions.clear()

    def tearDown(self) -> None:
        server._bridge = None
        server._sessions.clear()

    # ── 1. 新增,不是改名 ────────────────────────────────────────────────────
    def test_both_lookup_and_book_are_registered(self) -> None:
        """两个入口必须同时存在。

        ``book`` 消失就意味着这次改动被做成了改名——那会打断对外文档
        （``docs/a2a-docs/index.html`` 的工具表）与所有既有调用方。
        """
        names = {t.name for t in asyncio.run(server.mcp.list_tools())}
        self.assertIn("lookup", names)
        self.assertIn("book", names)

    def test_lookup_description_sends_transactions_to_book(self) -> None:
        """两个 tool 的描述必须互斥指路,否则模型在重叠语义里乱选。"""
        tools = {t.name: (t.description or "") for t in asyncio.run(server.mcp.list_tools())}
        self.assertIn("book", tools["lookup"])          # lookup 指出事务走 book
        self.assertIn("READ-ONLY", tools["lookup"])
        self.assertIn("lookup", tools["book"])          # book 指出查询走 lookup

    def test_lookup_description_does_not_promise_a_confirmation_gate(self) -> None:
        """描述不得声称「付费前会停下来确认」—— data 域始终没有那道门。

        实测事故（2026-10-29）：本描述一度写着 "The orchestrator shows the capability's unit
        price and stops for confirmation before the paid step"。而 ``data-aisa`` schema 没有
        任何 ``confirm_action``：付费调用不会被拦住等回传，调用方于是合理地怀疑是自己的
        ``autoApprove`` 把确认卡跳过了。

        **描述对调用方撒谎，比少写一句更糟**：它把"钱会被拦一下"的错觉卖给了上层 agent，
        而真正该在调用前征求用户同意的恰恰是那个 agent。

        **2026-10-30 卡片化后这条仍然成立，但多了一道结构闸**：schema 加了 ``ui`` 段与
        ``flows.plans``，仍然**没有** ``confirm_action``（不存在"是否确认"的往返），可付费的
        ``query`` 步 ``requires operation_id``，而该值只能由 ``data.capability-detail`` 卡的
        ``run-query`` 动作带入 —— 于是付费前必然经过那张带单价的只读卡。描述必须讲清这个
        区别：没有确认卡，但有唯一的付费入口，单价在那张卡上。
        """
        desc = next(
            (t.description or "") for t in asyncio.run(server.mcp.list_tools()) if t.name == "lookup"
        )
        self.assertNotIn("stops for confirmation", desc)
        # 事实①：没有确认卡 —— 不许再承诺有
        self.assertIn("no confirmation card", desc)
        # 事实②：付费只有一个入口，且必经带单价的 detail 卡
        self.assertIn("data.capability-detail", desc)
        self.assertIn("run-query", desc)
        self.assertIn("price_usd", desc)
        # 事实③：征求用户同意的责任仍在调用方
        self.assertIn("Ask the user first", desc)
        # 同源的第二处假话：曾写 "Drive the returned card with act(...) (e.g. to confirm a
        # priced query)" —— 暗示有确认卡。卡片化后有卡可驱动了，但仍没有"确认卡"。
        self.assertNotIn("to confirm a priced query", desc)

    def test_lookup_description_states_the_domain_is_card_driven(self) -> None:
        """必须告知本域是卡片驱动 —— 否则调用方会在 ``text`` 里刮答案，或不知道怎么推进。

        **2026-10-30 事实反转**：本域原先刻意不声明 ``ui`` 段（A2A 为 NL-first、一问一答），
        描述据此写着 "renders no cards" / "cards: []" / "nothing to drive with act(...)"。
        现在 schema 声明了 5 张卡与三步 plan，那套说法**全部变成假话** —— 调用方照它写就会
        忽略真正下发的卡片、去 ``text`` 里做字符串刮取。

        所以这里既钉新事实（卡片驱动、用 act 推进、卡片序列），也反向锁住旧说法不许回来。
        """
        desc = next(
            (t.description or "") for t in asyncio.run(server.mcp.list_tools()) if t.name == "lookup"
        )
        # 新事实：卡片驱动
        self.assertIn("card-driven", desc)
        self.assertIn("act(session_id, ...)", desc)
        for component in (
            "data.lookup", "data.capability-list", "data.capability-detail", "data.query-result"
        ):
            self.assertIn(component, desc)
        # text 是降级渲染，不是取结构化值的地方
        self.assertIn("fallback for", desc)
        # 反向锁：卡片化前的三句旧话不许再出现
        self.assertNotIn("renders no cards", desc)
        self.assertNotIn("cards: []", desc)
        self.assertNotIn("nothing to drive with", desc)

    # ── 2. 会话隔离 ─────────────────────────────────────────────────────────
    async def test_lookup_opens_its_own_session_kind(self) -> None:
        out = await server.lookup("what's the bitcoin price in USD")

        sid = out["session_id"]
        self.assertEqual(server._sessions[sid]["kind"], "lookup")
        self.assertEqual(server._sessions[sid]["context_id"], sid)
        # 与 book 的会话是两个独立 context
        booked = await server.book("book a hotel near the Bund")
        self.assertNotEqual(sid, booked["session_id"])
        self.assertEqual(server._sessions[booked["session_id"]]["kind"], "booking")

    # ── 3. flow_id:旁路查询不得换掉进行中订单的旅程 ────────────────────────
    async def test_lookup_reuses_the_current_flow_instead_of_minting(self) -> None:
        """``ensure_flow``,不是 ``start_new_flow``。

        场景:用户正在订酒店,中途顺口问一句汇率。若 lookup 在这里铸新 flow_id,那笔订单
        后续的步骤就跟它前面的步骤断在两条旅程上。
        """
        await server.book("book a hotel near the Bund")
        flow_after_book = self.fake.flow_id

        await server.lookup("今天的美元兑人民币汇率")

        self.assertEqual(self.fake.flow_calls, ["start_new", "ensure"])
        self.assertEqual(self.fake.flow_id, flow_after_book)  # 旅程没被换掉

    async def test_lookup_mints_a_flow_when_there_is_no_task_yet(self) -> None:
        """独立查询（没有在途订单）时 ensure_flow 自己会铸一个,查询仍可关联。"""
        await server.lookup("latest domestic news")

        self.assertEqual(self.fake.flow_calls, ["ensure"])
        self.assertIsNotNone(self.fake.flow_id)

    async def test_book_still_starts_a_new_flow(self) -> None:
        """回归:``book`` 的"开一段新旅程"语义没被这次改动动过。"""
        await server.book("book a flight")
        await server.book("book a hotel")

        self.assertEqual(self.fake.flow_calls, ["start_new", "start_new"])

    # ── 4. 原文透传 ─────────────────────────────────────────────────────────
    async def test_lookup_passes_the_request_through_verbatim(self) -> None:
        request = "查一下特斯拉去年的营收,顺便看看它最新的财报日期"
        out = await server.lookup(request)

        self.assertEqual(len(self.fake.text_calls), 1)
        context_id, text = self.fake.text_calls[0]
        self.assertEqual(text, request)
        self.assertEqual(context_id, out["session_id"])

    async def test_lookup_forwards_member_id_when_given(self) -> None:
        await server.lookup("bitcoin price", member_id="m-lookup")
        self.assertEqual(self.fake.set_member_calls, ["m-lookup"])

    async def test_lookup_does_not_touch_member_when_omitted(self) -> None:
        await server.lookup("bitcoin price")
        self.assertEqual(self.fake.set_member_calls, [])


if __name__ == "__main__":
    unittest.main()
