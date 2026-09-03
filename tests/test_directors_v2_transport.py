from __future__ import annotations

import json
import unittest

from workflow_1256.directors_v2 import run_directors_v2
from workflow_1256.directors_v2_transport import (
    DEFAULT_ARK_MODEL,
    DEFAULT_GPT_MODEL,
    DirectorsV2HTTPTransport,
    DirectorsV2TransportConfigError,
)


class DirectorsV2HTTPTransportTests(unittest.TestCase):
    def test_missing_credentials_are_rejected_before_network(self) -> None:
        with self.assertRaises(DirectorsV2TransportConfigError):
            DirectorsV2HTTPTransport(ark_api_key="", gpt_api_key="secret")

    def test_source_logic_and_two_http_payloads_are_connected(self) -> None:
        text = "".join(f"第{i}句。" for i in range(1, 9))
        route = {"dir": "view_dir", "sub": "emo_dir", "why": "synthetic", "hint": []}
        director = {
            "d": {
                "core": "核心判断",
                "tone": "克制",
                "emo": "先疑问后行动",
                "goal": "让观众理解并行动",
                "open": "先抛问题",
                "spine": "问题到行动",
                "arc": ["hook", "proof", "turn", "why", "method", "end"],
                "expression_domains": ["work", "mindset"],
                "variation_focus": ["viewpoint", "rhythm"],
                "rule": "每段推进一个点",
            },
            "seg": [
                {"u": [f"u{i:02d}"], "r": "move", "g": f"推进{i}", "l": "new", "n": "reality", "v": ["scene"]}
                for i in range(1, 9)
            ],
        }
        calls: list[tuple[str, dict, dict, float]] = []

        def requester(url, headers, payload, timeout):
            calls.append((url, dict(headers), dict(payload), timeout))
            if len(calls) == 1:
                return {"output_text": json.dumps(route, ensure_ascii=False)}
            return {"choices": [{"message": {"content": json.dumps(director, ensure_ascii=False)}}]}

        transport = DirectorsV2HTTPTransport(
            ark_api_key="ark-test",
            gpt_api_key="gpt-test",
            requester=requester,
        )
        result = run_directors_v2({"text": text}, transport=transport)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["segments"]), 8)
        self.assertEqual([item["segment_index"] for item in result["segment_beats"]], list(range(8)))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2]["model"], DEFAULT_ARK_MODEL)
        self.assertEqual(calls[0][2]["thinking"], {"type": "disabled"})
        self.assertEqual(calls[1][2]["model"], DEFAULT_GPT_MODEL)
        self.assertEqual(calls[1][2]["messages"][0]["role"], "system")
        self.assertEqual(calls[1][2]["messages"][1]["role"], "user")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer ark-test")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer gpt-test")


if __name__ == "__main__":
    unittest.main()
