"""``start_token_creation(dynamic_data_type=…)``：Visa 令牌取 TAVV（缺省）还是 DAVV（动态 CVV）。

透传：给了就原样放进 ``payment.token-setup#submit`` 的 payload 键 ``dynamic_data_type``（与 base 的
create-token ``a2a_input_schema`` 同名）；不给则 payload 与改前逐字一致（没有该键，平台按 TAVV 出）。
另锁 docstring 里的两种用法（用途已说明 → 直接 DAVV；用途不明 → 先问）。仅以假 bridge 替换 ``server._bridge``。
"""

from __future__ import annotations

import inspect
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _PKG_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import unittest  # noqa: E402

from agenzo_a2a_for_agent_toolkit import server  # noqa: E402
from test_token_first_two_session import FakeBridge  # noqa: E402


class DynamicDataTypeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_bridge = server._bridge
        self._orig_sessions = dict(server._sessions)
        server._sessions.clear()
        self.fake = FakeBridge(member_id="m-davv")
        server._bridge = self.fake

    def tearDown(self) -> None:
        server._bridge = self._orig_bridge
        server._sessions.clear()
        server._sessions.update(self._orig_sessions)

    def _payload(self, session_id: str) -> dict:
        submits = [
            a for a in self.fake.action_calls
            if a[0] == session_id and a[1] == "payment.token-setup" and a[2] == "submit"
        ]
        self.assertEqual(len(submits), 1)
        return submits[0][3]

    async def test_davv_is_passed_through(self) -> None:
        token = await server.start_token_creation(amount_cents=5000, member_id="m-davv", dynamic_data_type="DAVV")
        self.assertEqual(self._payload(token["session_id"]).get("dynamic_data_type"), "DAVV")

    async def test_default_omits_the_key(self) -> None:
        token = await server.start_token_creation(amount_cents=5000, member_id="m-davv")
        self.assertEqual(
            self._payload(token["session_id"]),
            {"amount_cents": 5000, "recipient_name": "", "recipient_account": ""},
        )

    async def test_blank_value_is_treated_as_not_given(self) -> None:
        token = await server.start_token_creation(amount_cents=5000, member_id="m-davv", dynamic_data_type="")
        self.assertNotIn("dynamic_data_type", self._payload(token["session_id"]))

    def test_docstring_tells_the_model_when_to_pick_which(self) -> None:
        doc = inspect.getdoc(server.start_token_creation) or ""
        self.assertIn("dynamic_data_type", doc)
        self.assertIn("do NOT ask again", doc)
        self.assertIn("ASK the user first", doc)
        self.assertIn("issued by the card network", doc)
        self.assertIn("just ONE", doc)

    def test_there_is_no_currency_parameter(self) -> None:
        self.assertNotIn("currency", inspect.signature(server.start_token_creation).parameters)

    async def test_no_currency_key_is_ever_sent(self) -> None:
        token = await server.start_token_creation(amount_cents=10000, member_id="m-davv", dynamic_data_type="DAVV")
        self.assertNotIn("currency", self._payload(token["session_id"]))

    def test_docstring_says_dollars_only_and_forbids_conversion(self) -> None:
        doc = inspect.getdoc(server.start_token_creation) or ""
        self.assertIn("AMOUNT IS IN US DOLLARS ONLY", doc)
        self.assertIn("要生成多少美元的？", doc)
        self.assertIn("100港币", doc)
        self.assertIn("do NOT convert it with an exchange rate", doc)
        self.assertIn('include "美元"', doc)
        self.assertNotIn("什么币种", doc)
        self.assertNotIn("currency code", doc)

    def test_docstring_forbids_jargon_toward_the_user(self) -> None:
        doc = inspect.getdoc(server.start_token_creation) or ""
        self.assertIn("TALK TO THE USER IN PLAIN WORDS ONLY", doc)
        for banned in ("DAVV", "TAVV", "cryptogram", "passkey", "FIDO", "card-network name"):
            self.assertIn(banned, doc.split("PLAIN WORDS ONLY", 1)[1].split("USE THIS EVEN", 1)[0])
        self.assertIn("按提示用指纹或面容确认就行", doc)
        self.assertIn("Do not announce the confirmation step in advance", doc)
        self.assertIn("还是在我们这里直接付酒店、机票、打车", doc)


if __name__ == "__main__":
    unittest.main()
