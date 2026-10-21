"""``flow_id`` —— 一次用户任务的关联键，经 W3C ``baggage`` 头贯穿全链路。

要解决的问题：链路上现有的两个 id 都串不起一次用户任务。

- ``trace_id`` 的粒度是**一个对话轮次**。实测一次订酒店产生 11 个 trace，没有哪一个
  覆盖全程。
- ``session_id``（= A2A ``contextId``）的粒度是**一个会话**。一次订单天然跨两个：订单
  会话 + 独立的支付会话（铸 Network Token 用，刻意隔离以免冲掉订单的 ``flow_state``）。

**本桥是唯一知道这两个会话属于同一次任务的角色**——支付会话是它代为开的。所以 flow_id
在这里铸造，并在订单会话与支付会话上带同一个值；下游（orchestrator → CLI → platform →
ledger，含异步记账）照着往下传。

为什么不复用 py-commons 的实现
==============================
权威定义在 ``agenzo_py_commons.telemetry.propagation``（``FLOW_ID_PREFIX`` /
``new_flow_id`` / ``build_baggage`` / ``flow_id_from_baggage``）。本包刻意只依赖
mcp / httpx / python-dotenv / uvicorn，要能被集成方直接 pip 装，不该为了两个十几行的
纯函数引入一条走私有 registry 的依赖。故这里保留一份最小副本，**只含铸造与出站构造**
（本桥是链路起点，不需要解析入站值）。

``FLOW_ID_PREFIX`` 与字符集若在 py-commons 侧调整，这里必须同步——不同步的症状是
本桥发出的头被下游判为非法而静默丢弃，只在集成时才暴露。
"""

from __future__ import annotations

import re
import uuid

__all__ = [
    "BAGGAGE_HEADER",
    "FLOW_ID_BAGGAGE_KEY",
    "FLOW_ID_PREFIX",
    "build_baggage",
    "is_valid_flow_id",
    "new_flow_id",
]

#: W3C Baggage 的 HTTP 头名。
BAGGAGE_HEADER = "baggage"

#: flow_id 在 baggage 里的成员名（``agenzo.`` 命名空间，避免与第三方成员撞名）。
FLOW_ID_BAGGAGE_KEY = "agenzo.flow_id"

#: 字面前缀，与业务 ID 的既有约定同款（``hho_`` / ``ffo_`` / ``rio_`` / ``ptk_`` / ``chg_``）。
#: trace_id / request_id / session_id 都是裸 32 位 hex，带前缀后在日志里一眼可辨。
FLOW_ID_PREFIX = "flw_"

#: 合法形状：前缀 + 8~60 个 ``[A-Za-z0-9_-]``（总长 12~64）。与 py-commons 保持一致。
_FLOW_ID_RE = re.compile(r"\Aflw_[A-Za-z0-9_-]{8,60}\Z")


def new_flow_id() -> str:
    """铸一个新的 ``flow_id``：前缀 + uuid4 hex（共 36 字符）。"""
    return f"{FLOW_ID_PREFIX}{uuid.uuid4().hex}"


def is_valid_flow_id(value: str | None) -> bool:
    """``value`` 是否是可安全传播的 ``flow_id``。"""
    return bool(value) and _FLOW_ID_RE.match(value) is not None


def build_baggage(flow_id: str | None) -> str | None:
    """生成 ``baggage`` 头值；无 ``flow_id`` 或不合规时返回 ``None``（调用方据此不加头）。"""
    if not is_valid_flow_id(flow_id):
        return None
    return f"{FLOW_ID_BAGGAGE_KEY}={flow_id}"
