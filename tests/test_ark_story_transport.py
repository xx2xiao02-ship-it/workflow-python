from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_1256.ark_story_transport import (
    ArkStoryConfig,
    ArkStoryConfigError,
    ArkStoryFailoverHTTPTransport,
    ArkStoryHTTPTransport,
    ArkStoryTransportError,
    load_story_auth_from_document,
)


class ArkStoryTransportTests(unittest.TestCase):
    def test_calls_responses_api_with_disabled_thinking(self) -> None:
        received = []

        def requester(url, headers, payload, timeout):
            received.append((url, headers, payload, timeout))
            return {"output_text": json.dumps({"ok": True})}

        result = ArkStoryHTTPTransport(
            ArkStoryConfig(api_key="redacted-test-key", model="seed-2.1-turbo"), requester=requester
        )("系统提示", "用户提示")
        self.assertEqual(result, '{"ok": true}')
        self.assertEqual(received[0][2]["thinking"], {"type": "disabled"})
        self.assertEqual(received[0][2]["max_output_tokens"], 3200)
        self.assertEqual(received[0][2]["model"], "seed-2.1-turbo")

    def test_run_text_honors_small_model_request_overrides(self) -> None:
        received = []

        def requester(url, headers, payload, timeout):
            received.append((url, headers, payload, timeout))
            return {"output_text": '{"status":"PASS","directives":[]}'}

        result = ArkStoryHTTPTransport(
            ArkStoryConfig(api_key="redacted-test-key", model="story-default"),
            requester=requester,
        ).run_text(
            system_prompt="系统提示",
            prompt="用户提示",
            model="doubao-seed-2.0-mini",
            temperature=0.0,
            max_tokens=400,
            timeout=20,
        )

        self.assertEqual(result, '{"status":"PASS","directives":[]}')
        self.assertEqual(received[0][2]["model"], "doubao-seed-2.0-mini")
        self.assertEqual(received[0][2]["temperature"], 0.0)
        self.assertEqual(received[0][2]["max_output_tokens"], 400)
        self.assertEqual(received[0][3], 20)

    def test_supports_chat_completions_response_for_api2_fallback(self) -> None:
        received = []

        def requester(url, headers, payload, timeout):
            received.append((url, headers, payload, timeout))
            return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}

        result = ArkStoryHTTPTransport(
            ArkStoryConfig(
                api_key="redacted-test-key",
                model="seed-2.1-turbo",
                api_url="https://ark.example.test/api/v3/chat/completions",
            ),
            requester=requester,
        )("系统提示", "用户提示")
        self.assertEqual(result, '{"ok": true}')
        self.assertEqual(received[0][2]["messages"][0]["content"], "系统提示")
        self.assertEqual(received[0][2]["messages"][1]["content"], "用户提示")
        self.assertEqual(received[0][2]["max_tokens"], 3200)

    def test_rejects_empty_api_key(self) -> None:
        with self.assertRaises(ArkStoryConfigError):
            ArkStoryHTTPTransport(ArkStoryConfig(api_key=""))

    def test_loads_primary_and_backup_auth_from_existing_document(self) -> None:
        document = Path(self._testMethodName + ".md")
        self.addCleanup(lambda: document.unlink(missing_ok=True))
        document.write_text(
            'export ARK\\_API\\_KEY="primary"\nSeed 2.1 pro ep-primary\n'
            'export ARK\\_API\\_KEY="backup"\nSeed 2.1 pro ep-backup\n',
            encoding="utf-8",
        )
        self.assertEqual(load_story_auth_from_document(document), [("primary", "ep-primary"), ("backup", "ep-backup")])

    def test_failover_uses_backup_after_primary_transport_failure(self) -> None:
        primary = ArkStoryHTTPTransport(ArkStoryConfig(api_key="primary"), requester=lambda *_: {"error": {"message": "quota"}})
        backup = ArkStoryHTTPTransport(ArkStoryConfig(api_key="backup"), requester=lambda *_: {"output_text": '{"ok": true}'})
        self.assertEqual(ArkStoryFailoverHTTPTransport([primary, backup])("system", "user"), '{"ok": true}')

    def test_explicit_empty_story_document_disables_legacy_document_fallback(self) -> None:
        names = (
            "STORY_WRITER_AUTH_DOCUMENT",
            "STORY_WRITER_ARK_API_KEY",
            "DIRECTORS_V2_ARK_API_KEY",
            "ARK_API_KEY",
            "STORY_WRITER_ARK_BACKUP_API_KEY",
            "DIRECTORS_V2_ARK_BACKUP_API_KEY",
            "ARK_BACKUP_API_KEY",
        )
        previous = {name: os.environ.get(name) for name in names}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        os.environ["STORY_WRITER_AUTH_DOCUMENT"] = ""
        for name in names[1:]:
            os.environ.pop(name, None)

        with patch(
            "workflow_1256.ark_story_transport.load_story_auth_from_document",
            side_effect=AssertionError("页面凭据模式不应读取历史故事鉴权文档"),
        ):
            with self.assertRaises(ArkStoryConfigError):
                ArkStoryFailoverHTTPTransport.from_runtime_config()

    def test_failover_reports_sanitized_failure_categories(self) -> None:
        def failed_requester(status):
            def request(*_args):
                raise ArkStoryTransportError(
                    f"方舟故事模型 HTTP {status}：secret-provider-detail"
                )

            return request

        primary = ArkStoryHTTPTransport(
            ArkStoryConfig(api_key="primary"),
            requester=failed_requester(401),
        )
        backup = ArkStoryHTTPTransport(
            ArkStoryConfig(api_key="backup"),
            requester=failed_requester(403),
        )
        with self.assertRaisesRegex(ArkStoryTransportError, r"主：HTTP 401；备用1：HTTP 403") as caught:
            ArkStoryFailoverHTTPTransport([primary, backup])("system", "user")
        self.assertNotIn("secret-provider-detail", str(caught.exception))

    def test_review_transport_can_use_a_separate_model_without_exposing_key(self) -> None:
        previous = {name: os.environ.get(name) for name in ("COPY_REVIEW_ARK_API_KEY", "COPY_REVIEW_ARK_MODEL")}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        os.environ["COPY_REVIEW_ARK_API_KEY"] = "review-key-for-test"
        os.environ["COPY_REVIEW_ARK_MODEL"] = "review-model-for-test"
        transport = ArkStoryFailoverHTTPTransport.from_review_runtime_config()
        self.assertEqual(transport.transports[0].config.model, "review-model-for-test")
        self.assertEqual(transport.transports[0].config.api_key, "review-key-for-test")

    def test_content_and_style_roles_can_use_separate_optional_models(self) -> None:
        names = (
            "CONTENT_ANALYZER_ARK_API_KEY", "CONTENT_ANALYZER_ARK_MODEL",
            "STYLE_ADAPTER_ARK_API_KEY", "STYLE_ADAPTER_ARK_MODEL",
        )
        previous = {name: os.environ.get(name) for name in names}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        os.environ["CONTENT_ANALYZER_ARK_API_KEY"] = "content-key-for-test"
        os.environ["CONTENT_ANALYZER_ARK_MODEL"] = "content-model-for-test"
        os.environ["STYLE_ADAPTER_ARK_API_KEY"] = "style-key-for-test"
        os.environ["STYLE_ADAPTER_ARK_MODEL"] = "style-model-for-test"

        content = ArkStoryFailoverHTTPTransport.from_content_runtime_config()
        style = ArkStoryFailoverHTTPTransport.from_style_runtime_config()
        self.assertEqual(content.transports[0].config.model, "content-model-for-test")
        self.assertEqual(content.transports[0].config.api_key, "content-key-for-test")
        self.assertEqual(style.transports[0].config.model, "style-model-for-test")
        self.assertEqual(style.transports[0].config.api_key, "style-key-for-test")

    def test_api_management_language_model_order_adds_api2_credentials(self) -> None:
        names = (
            "STORY_WRITER_AUTH_DOCUMENT",
            "STORY_WRITER_ARK_API_KEY",
            "STORY_WRITER_ARK_BACKUP_API_KEY",
            "CONTENT_ANALYZER_ARK_API_KEY",
            "CONTENT_ANALYZER_ARK_MODEL",
            "API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING",
            "API_MANAGEMENT_LANGUAGE_MODEL_ORDER",
            "VOICE_DIRECTOR_PRIMARY_API_KEY",
            "VOICE_DIRECTOR_BACKUP_API_KEY",
            "VOICE_DIRECTOR_PRIMARY_MODEL",
            "VOICE_DIRECTOR_BACKUP_MODEL",
            "VOICE_DIRECTOR_API_URL",
            "API_MANAGEMENT_MODEL_FALLBACKS_DIRECTOR_SEED21",
            "API_MANAGEMENT_EXTRA_KEYS_DIRECTOR_SEED21",
        )
        previous = {name: os.environ.get(name) for name in names}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        for name in names:
            os.environ.pop(name, None)
        os.environ["STORY_WRITER_ARK_API_KEY"] = "story-api1-key"
        os.environ["CONTENT_ANALYZER_ARK_MODEL"] = "content-api1-model"
        os.environ["API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING"] = json.dumps(
            ["content-api1-model"], ensure_ascii=False
        )
        os.environ["API_MANAGEMENT_LANGUAGE_MODEL_ORDER"] = json.dumps(
            ["story-writing", "director-seed21"], ensure_ascii=False
        )
        os.environ["VOICE_DIRECTOR_PRIMARY_API_KEY"] = "director-api2-primary"
        os.environ["VOICE_DIRECTOR_BACKUP_API_KEY"] = "director-api2-backup"
        os.environ["VOICE_DIRECTOR_PRIMARY_MODEL"] = "director-api2-model"
        os.environ["VOICE_DIRECTOR_BACKUP_MODEL"] = "director-api2-backup-model"
        os.environ["VOICE_DIRECTOR_API_URL"] = "https://ark.example.test/api/v3/chat/completions"
        os.environ["API_MANAGEMENT_MODEL_FALLBACKS_DIRECTOR_SEED21"] = json.dumps(
            ["director-api2-model", "director-api2-backup-model"], ensure_ascii=False
        )

        transport = ArkStoryFailoverHTTPTransport.from_content_runtime_config(
            include_language_model_api_fallback=True,
        )
        self.assertEqual(len(transport.transports), 5)
        self.assertEqual(
            len({item.config.api_key for item in transport.transports}),
            3,
        )
        self.assertEqual(
            transport.transports[-1].config.api_url,
            "https://ark.example.test/api/v3/chat/completions",
        )

        calls = []

        def requester(_url, headers, _payload, _timeout):
            calls.append(headers["Authorization"])
            if len(calls) < len(transport.transports):
                raise ArkStoryTransportError("方舟故事模型 HTTP 403：overdue")
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        transport = ArkStoryFailoverHTTPTransport([
            ArkStoryHTTPTransport(item.config, requester=requester)
            for item in transport.transports
        ])
        self.assertEqual(transport("system", "user"), '{"ok": true}')
        self.assertEqual(calls[-1], "Bearer director-api2-backup")

    def test_api_management_can_explicitly_put_api2_first(self) -> None:
        names = (
            "STORY_WRITER_ARK_API_KEY",
            "CONTENT_ANALYZER_ARK_MODEL",
            "API_MANAGEMENT_LANGUAGE_MODEL_ORDER",
            "VOICE_DIRECTOR_PRIMARY_API_KEY",
            "VOICE_DIRECTOR_PRIMARY_MODEL",
            "VOICE_DIRECTOR_API_URL",
        )
        previous = {name: os.environ.get(name) for name in names}
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        os.environ["STORY_WRITER_ARK_API_KEY"] = "story-api1-key"
        os.environ["CONTENT_ANALYZER_ARK_MODEL"] = "content-api1-model"
        os.environ["API_MANAGEMENT_LANGUAGE_MODEL_ORDER"] = json.dumps(
            ["director-seed21", "story-writing"], ensure_ascii=False
        )
        os.environ["VOICE_DIRECTOR_PRIMARY_API_KEY"] = "director-api2-primary"
        os.environ["VOICE_DIRECTOR_PRIMARY_MODEL"] = "director-api2-model"
        os.environ["VOICE_DIRECTOR_API_URL"] = "https://ark.example.test/api/v3/chat/completions"

        transport = ArkStoryFailoverHTTPTransport.from_content_runtime_config(
            include_language_model_api_fallback=True,
        )
        self.assertEqual(transport.transports[0].config.api_key, "director-api2-primary")
        self.assertTrue(transport.transports[0].config.api_url.endswith("/chat/completions"))

    def test_role_model_slots_are_applied_when_auth_is_shared(self) -> None:
        names = (
            "STORY_WRITER_AUTH_DOCUMENT", "STORY_WRITER_ARK_API_KEY",
            "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY",
            "CONTENT_ANALYZER_ARK_API_KEY", "CONTENT_ANALYZER_ARK_MODEL",
            "STYLE_ADAPTER_ARK_API_KEY", "STYLE_ADAPTER_ARK_MODEL",
            "API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING",
        )
        previous = {name: os.environ.get(name) for name in names}
        document = Path(self._testMethodName + "-auth.md")
        self.addCleanup(document.unlink)
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        document.write_text(
            'export ARK\\_API\\_KEY="shared-primary"\nSeed 2.1 pro ep-shared\n',
            encoding="utf-8",
        )
        os.environ["STORY_WRITER_AUTH_DOCUMENT"] = str(document)
        for name in names[1:]:
            os.environ.pop(name, None)
        os.environ["CONTENT_ANALYZER_ARK_MODEL"] = "content-slot-model"
        os.environ["STYLE_ADAPTER_ARK_MODEL"] = "style-slot-model"
        os.environ["API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING"] = json.dumps(
            ["story-slot-model", "content-slot-model"], ensure_ascii=False
        )

        content = ArkStoryFailoverHTTPTransport.from_content_runtime_config()
        style = ArkStoryFailoverHTTPTransport.from_style_runtime_config()
        self.assertEqual(content.transports[0].config.api_key, "shared-primary")
        self.assertEqual(content.transports[0].config.model, "content-slot-model")
        self.assertEqual(style.transports[0].config.api_key, "shared-primary")
        self.assertEqual(style.transports[0].config.model, "style-slot-model")

    def test_story_model_slot_is_applied_when_auth_is_shared(self) -> None:
        names = (
            "STORY_WRITER_AUTH_DOCUMENT", "STORY_WRITER_ARK_API_KEY",
            "DIRECTORS_V2_ARK_API_KEY", "ARK_API_KEY",
            "STORY_WRITER_ARK_MODEL", "STORY_WRITER_ARK_BACKUP_MODEL",
            "API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING",
        )
        previous = {name: os.environ.get(name) for name in names}
        document = Path(self._testMethodName + "-auth.md")
        self.addCleanup(document.unlink)
        self.addCleanup(lambda: [
            os.environ.__setitem__(name, value) if value is not None else os.environ.pop(name, None)
            for name, value in previous.items()
        ])
        document.write_text(
            'export ARK\\_API\\_KEY="shared-primary"\nSeed 2.1 pro ep-old\n',
            encoding="utf-8",
        )
        os.environ["STORY_WRITER_AUTH_DOCUMENT"] = str(document)
        for name in names[1:]:
            os.environ.pop(name, None)
        os.environ["STORY_WRITER_ARK_MODEL"] = "story-slot-model"
        os.environ["API_MANAGEMENT_MODEL_FALLBACKS_STORY_WRITING"] = json.dumps(
            ["story-slot-model", "content-slot-model"], ensure_ascii=False
        )

        transport = ArkStoryFailoverHTTPTransport.from_runtime_config()
        self.assertEqual(transport.transports[0].config.api_key, "shared-primary")
        self.assertEqual(transport.transports[0].config.model, "story-slot-model")


if __name__ == "__main__":
    unittest.main()
