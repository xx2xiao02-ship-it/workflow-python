from __future__ import annotations

import json
import unittest

from workflow_1256.directors_v2 import run_directors_v2
from workflow_1256.directors_v2_plugin_transport import (
    DirectorsV2PluginConfigError,
    DirectorsV2PluginHTTPTransport,
    DirectorsV2PluginTransportError,
)


EXPECTED = {
    "director_plan": {
        "arc": ["hook"],
        "core": "核心",
        "director_type": "view_dir",
        "emo": "情绪",
        "expression_domains": ["work"],
        "goal": "目标",
        "open": "开场",
        "rule": "规则",
        "spine": "主线",
        "tone": "语气",
        "variation_focus": ["viewpoint"],
    },
    "ok": True,
    "segment_beats": [
        {
            "beats": [{"expression_need": "reality", "relation": "new", "route_candidates": ["scene"]}],
            "rhythm": "hook",
            "segment_goal": "目标",
            "segment_index": 0,
            "segment_text": "结果",
        }
    ],
    "segments": ["结果"],
}


class CurrentDirectorsV2PluginTransportTests(unittest.TestCase):
    def test_missing_key_is_rejected(self) -> None:
        with self.assertRaises(DirectorsV2PluginConfigError):
            DirectorsV2PluginHTTPTransport(api_key="")

    def test_text_branch_uses_current_source_contract_and_maps_output_5_5(self) -> None:
        calls = []

        def requester(url, headers, payload, timeout):
            calls.append((url, dict(headers), dict(payload), timeout))
            return {
                "status_code": 200,
                "json": {"choices": [{"message": {"content": json.dumps(EXPECTED, ensure_ascii=False)}}]},
                "text": "ok",
            }

        transport = DirectorsV2PluginHTTPTransport(
            api_key="test-key",
            system_prompt="输出 8364 导演 JSON",
            requester=requester,
            sleeper=lambda _: None,
        )
        result = run_directors_v2({"text": "输入文案"}, transport=transport)
        self.assertEqual(result, EXPECTED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["model"], "gpt-5.5")
        self.assertEqual(calls[0][2]["messages"][0]["content"], "输出 8364 导演 JSON")
        self.assertEqual(calls[0][2]["messages"][1]["content"], "输入文案")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer test-key")

    def test_retry_matches_source_status_list(self) -> None:
        calls = []

        def requester(_url, _headers, _payload, _timeout):
            calls.append(1)
            if len(calls) < 3:
                return {"status_code": 503, "text": "temporary", "json": None}
            return {"status_code": 200, "text": "ok", "json": {"choices": [{"message": {"content": "done"}}]}}

        transport = DirectorsV2PluginHTTPTransport(
            api_key="test-key", requester=requester, sleeper=lambda _: None
        )
        result = transport._call_text("system", "user")
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 3)

    def test_stop_on_error_disables_same_channel_retry(self) -> None:
        calls = []

        def requester(_url, _headers, _payload, _timeout):
            calls.append(1)
            return {"status_code": 503, "text": "temporary", "json": None}

        transport = DirectorsV2PluginHTTPTransport(
            api_key="test-key",
            failover_strategy="stop_on_error",
            requester=requester,
            sleeper=lambda _: None,
        )
        result = transport._call_text("system", "user")
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1)

    def test_plain_text_output_is_not_claimed_as_8364_director_result(self) -> None:
        def requester(_url, _headers, _payload, _timeout):
            return {"status_code": 200, "text": "ok", "json": {"choices": [{"message": {"content": "普通文本"}}]}}

        transport = DirectorsV2PluginHTTPTransport(api_key="test-key", requester=requester)
        with self.assertRaises(DirectorsV2PluginTransportError):
            run_directors_v2({"text": "输入"}, transport=transport)


if __name__ == "__main__":
    unittest.main()
