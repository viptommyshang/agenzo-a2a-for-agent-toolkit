"""结构护栏：``@mcp.tool()`` 必须全部位于 ``if __name__ == "__main__":`` 入口块**之前**。

为什么需要这条（2026-11-02 真实踩过）：
四个 ``lookup_*`` 工具被加在了入口块**之后**。``python -m agenzo_a2a_for_agent_toolkit.server``
执行模块时，到入口块就进 ``main()`` → ``mcp.run()`` 阻塞 serve-forever，后面的 ``@mcp.tool()``
**永远不会执行**，于是 MCP 宿主只看到 14 个工具，而源码里有 18 个。

这个 bug 对常规手段隐形：
  · ``import`` 这个模块时 ``__name__`` 不是 ``"__main__"``，入口块被跳过、模块一路执行到底，
    所有装饰器都注册 —— 所以"import 后数函数"会得到 18，是假阳性；
  · 全部单测都走 import，111 个测试全过，照样放过了它。

能真正发现它的只有带 stdio 握手的 ``tools/list``，而那要起子进程、依赖 uv、慢。所以这里用
AST 做静态位置检查：零依赖、确定、够快。

需要端到端复核时，用 ``mcp.client.stdio.stdio_client`` 按 MCP 宿主配置里那条命令起一个子进程，
``await session.initialize()`` 之后 ``await session.list_tools()``，数返回的工具数 ——
**不要**用 ``import …server`` 后数函数来代替，那正是上面说的假阳性。
"""

from __future__ import annotations

import ast
import pathlib

SERVER_PY = (
    pathlib.Path(__file__).resolve().parents[1] / "agenzo_a2a_for_agent_toolkit" / "server.py"
)


def _parse() -> ast.Module:
    return ast.parse(SERVER_PY.read_text(encoding="utf-8"))


def _is_main_guard(node: ast.stmt) -> bool:
    """识别 ``if __name__ == "__main__":``。"""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """识别 ``@mcp.tool()``（调用形式）与 ``@mcp.tool``（裸形式）。"""
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def _tool_defs(tree: ast.Module) -> list[tuple[str, int]]:
    return [
        (node.name, node.lineno)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_mcp_tool_decorator(d) for d in node.decorator_list)
    ]


def test_main_guard_exists_and_is_the_last_module_statement() -> None:
    """入口块必须是模块体的最后一条语句 —— 它下面不能再有任何模块级代码。"""
    tree = _parse()
    guards = [(i, n) for i, n in enumerate(tree.body) if _is_main_guard(n)]
    assert guards, "server.py 里找不到 if __name__ == '__main__': 入口块"
    assert len(guards) == 1, f"入口块出现了 {len(guards)} 次，应当只有一处"

    index, guard = guards[0]
    trailing = tree.body[index + 1 :]
    assert not trailing, (
        "入口块之后还有模块级语句，它们在 `python -m` 运行时永远不会执行"
        f"（mcp.run() 已阻塞）：{[(type(n).__name__, n.lineno) for n in trailing]}。"
        f"入口块在第 {guard.lineno} 行，把这些代码移到它之前。"
    )


def test_every_mcp_tool_is_defined_before_the_main_guard() -> None:
    """所有 @mcp.tool() 都要在入口块之前定义，否则 `python -m` 跑起来时注册不上。"""
    tree = _parse()
    guards = [n for n in tree.body if _is_main_guard(n)]
    assert guards, "server.py 里找不到 if __name__ == '__main__': 入口块"
    guard_lineno = guards[0].lineno

    late = [(name, lineno) for name, lineno in _tool_defs(tree) if lineno > guard_lineno]
    assert not late, (
        f"这些工具定义在入口块（第 {guard_lineno} 行）之后，`python -m …server` 运行时"
        f"不会注册给 MCP 宿主，宿主的 tools/list 里看不到它们：{late}"
    )


def test_all_known_tools_are_registered_at_module_level() -> None:
    """四个 lookup_* 与既有工具都应被 @mcp.tool() 装饰在模块顶层。

    这条是上面位置检查的补充：位置对但漏了装饰器，同样不会出现在 tools/list 里。
    """
    names = {name for name, _ in _tool_defs(_parse())}
    expected = {
        "discover",
        "configure",
        "book",
        "lookup",
        "send_message",
        "act",
        "poll",
        "start_payment",
        "start_token_creation",
        "open_url",
        "resolve_location",
        "resolve_pickup_time",
        "guide",
        "inspect",
        "lookup_categories",
        "lookup_search",
        "lookup_describe",
        "lookup_query",
    }
    assert expected <= names, f"缺少工具注册：{sorted(expected - names)}"
