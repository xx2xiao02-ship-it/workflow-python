from __future__ import annotations

import unittest

from workflow_1256.aishuch_transport import AishuchHTTPClient, AishuchTransportError
from workflow_1256.create_image_task import CreateImageTaskRequest
from workflow_1256.get_task_result import GetTaskResultRequest


class AishuchTransportTests(unittest.TestCase):
    def test_create_image_payload_and_auth_are_mapped(self) -> None:
        calls = []

        def requester(method, url, headers, payload, timeout):
            calls.append((method, url, dict(headers), payload, timeout))
            return {"data": [{"task_id": "task-1"}]}

        client = AishuchHTTPClient("secret", requester=requester)
        result = client.create_image(
            CreateImageTaskRequest(
                api_key="Bearer request-key",
                prompt="画面",
                n=1,
                size="auto",
                resolution="1k",
                image_urls=["https://example.com/ref.png"],
                official_fallback=False,
            )
        )
        self.assertEqual(result["data"][0]["task_id"], "task-1")
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/images/generations"))
        self.assertEqual(calls[0][2]["Authorization"], "Bearer request-key")
        self.assertEqual(calls[0][3]["image_urls"], ["https://example.com/ref.png"])

    def test_get_task_uses_task_id_and_preserves_raw_response(self) -> None:
        calls = []

        def requester(method, url, headers, payload, timeout):
            calls.append((method, url, dict(headers), payload))
            return {"data": {"status": "processing"}}

        client = AishuchHTTPClient("secret", requester=requester)
        result = client.get_task(GetTaskResultRequest(task_id="task-1", api_key="key"))
        self.assertEqual(result["data"]["status"], "processing")
        self.assertEqual(calls[0][0], "GET")
        self.assertTrue(calls[0][1].endswith("/tasks/task-1"))
        self.assertIsNone(calls[0][3])

    def test_empty_key_is_rejected(self) -> None:
        with self.assertRaises(AishuchTransportError):
            AishuchHTTPClient("")


if __name__ == "__main__":
    unittest.main()
