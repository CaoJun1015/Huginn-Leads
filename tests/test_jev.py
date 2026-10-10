from dataclasses import replace
from pathlib import Path
import json
import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from huginn_leads.config import Settings
from huginn_leads.jev import ClassifyBody, JevError, classify_message


class JevTests(unittest.TestCase):
    def setUp(self):
        root = Path(".")
        self.settings = Settings(root, root, root, root, root, "http://127.0.0.1:18080",
                                 typesafe_api_key="test-secret-key")
        self.body = ClassifyBody(text="收两台笔记本", context="采购讨论", categories={
            "purchase": "求购", "other": "其他或不确定",
        })
        self.result = {
            "model": "jev-1.13.0",
            "answers": {"category": {"type": "choice", "choice": "purchase",
                                     "probabilities": {"purchase": 0.95, "other": 0.05},
                                     "confidence": 0.9}},
            "usage": {"input_tokens": 100, "output_tokens": 0},
        }

    def call(self, *, status=200, raw=None, error=None):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = json.dumps(self.result).encode() if raw is None else raw
        connection.request.side_effect = error
        with patch("huginn_leads.jev.HTTPSConnection", return_value=connection) as constructor:
            try:
                return classify_message(self.settings, self.body)
            finally:
                constructor.assert_called_once_with("api.typesafe.ai", timeout=20)
                connection.close.assert_called_once()
                args, kwargs = connection.request.call_args
                self.assertEqual(args, ("POST", "/v1/systemone"))
                payload = json.loads(kwargs["body"])
                self.assertEqual(payload["state"], {"message": self.body.text, "context": self.body.context})
                self.assertEqual(payload["questions"]["category"]["criteria"], self.body.categories)
                self.assertEqual(payload["model"], self.settings.typesafe_model)
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret-key")

    def test_classification_contract_and_review(self):
        result = self.call()
        self.assertEqual(result.category, "purchase")
        self.assertFalse(result.needs_review)
        self.assertEqual(result.usage.input_tokens, 100)
        self.body = self.body.model_copy(update={"confidence_threshold": 0.95})
        self.assertTrue(self.call().needs_review)
        self.body = ClassifyBody(text="还是那个")
        self.result["answers"]["category"].update(
            choice="uncertain", confidence=1.0,
            probabilities={key: float(key == "uncertain") for key in self.body.categories},
        )
        self.assertTrue(self.call().needs_review)

    def test_missing_key_never_connects(self):
        with patch("huginn_leads.jev.HTTPSConnection") as connection:
            with self.assertRaises(JevError) as raised:
                classify_message(replace(self.settings, typesafe_api_key=""), self.body)
            self.assertEqual(raised.exception.code, "jev_not_configured")
            connection.assert_not_called()
        self.assertNotIn("test-secret-key", repr(self.settings))

    def test_upstream_errors_are_safe_and_not_retried(self):
        for status, expected_status, code in (
            (401, 502, "jev_auth_failed"), (403, 502, "jev_auth_failed"),
            (429, 429, "jev_rate_limited"), (529, 503, "jev_unavailable"),
            (503, 503, "jev_unavailable"), (500, 502, "jev_upstream_error"),
            (302, 502, "jev_upstream_error"),
        ):
            with self.subTest(status=status), self.assertRaises(JevError) as raised:
                self.call(status=status, raw=b"test-secret-key and private message")
            self.assertEqual((raised.exception.status, raised.exception.code), (expected_status, code))
            self.assertNotIn("test-secret-key", str(raised.exception))
        for error, code in ((TimeoutError(), "jev_timeout"), (OSError(), "jev_connection_failed")):
            with self.subTest(code=code), self.assertRaises(JevError) as raised:
                self.call(error=error)
            self.assertEqual(raised.exception.code, code)

    def test_bad_responses_do_not_become_classifications(self):
        for raw in (b"not json", b"{}", b"x" * (128 * 1024 + 1)):
            with self.subTest(raw=raw[:20]), self.assertRaises(JevError) as raised:
                self.call(raw=raw)
            self.assertEqual(raised.exception.code, "jev_invalid_response")
        for changes in (
            {"confidence": 1.1}, {"confidence": float("nan")},
            {"choice": "invented"}, {"choice": "other"},
            {"probabilities": {"purchase": 0.5, "other": 0.1}},
            {"probabilities": {"purchase": 0.95, "invented": 0.05}},
        ):
            previous = dict(self.result["answers"]["category"])
            self.result["answers"]["category"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(JevError):
                self.call()
            self.result["answers"]["category"] = previous

    def test_input_boundaries(self):
        for changes in (
            {"text": "   "}, {"text": "x" * 8001}, {"context": "x" * 4001},
            {"categories": {"only": "one"}}, {"categories": {"a": " ", "b": "ok"}},
            {"categories": {str(i): "category" for i in range(51)}},
            {"confidence_threshold": -0.1}, {"confidence_threshold": float("nan")},
            {"api_key": "untrusted"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ClassifyBody.model_validate({"text": "message", **changes})


if __name__ == "__main__":
    unittest.main()
