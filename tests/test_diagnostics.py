import contextlib
import io
import json
import unittest

from asu.createai import BridgeError
from codex_asu import diagnose


class DiagnosticsTests(unittest.TestCase):
    def test_failed_models_does_not_block_minimal_requests_or_expose_text(self):
        class API:
            base_url = "https://example.invalid/v1"
            calls = []

            def models(self):
                raise BridgeError("ASU HTTP 500 at /models.", 500)

            def open(self, path, body):
                self.calls.append((path, body))
                if path == "/responses":
                    return io.BytesIO(json.dumps({"output": [{"content": [{"text": "private response"}]}]}).encode())
                if body.get("stream"):
                    return io.BytesIO(b'data: {"choices":[{"delta":{"content":"private response"}}]}\n\ndata: [DONE]\n\n')
                return io.BytesIO(b'{"choices":[{"message":{"content":"private response"}}]}')
        api = API()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(diagnose(api, "defaults"))
        self.assertEqual(len(api.calls), 3)
        self.assertEqual(set(api.calls[0][1]), {"model", "messages"})
        self.assertEqual(set(api.calls[1][1]), {"model", "messages", "stream"})
        self.assertNotIn("private response", output.getvalue())
        self.assertEqual(output.getvalue().count("  PASS"), 3)

    def test_http_200_with_stream_error_is_failure(self):
        class API:
            base_url = "https://example.invalid/v1"

            def models(self):
                return {"data": []}

            def open(self, path, body):
                if body.get("stream"):
                    return io.BytesIO(b'data: {"error":{"message":"private internal error"}}\n\n')
                return io.BytesIO(b'{"error":{"message":"private internal error"}}')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(diagnose(API(), "defaults"))
        self.assertEqual(output.getvalue().count("  FAIL:"), 3)
        self.assertNotIn("private internal error", output.getvalue())


if __name__ == "__main__":
    unittest.main()
