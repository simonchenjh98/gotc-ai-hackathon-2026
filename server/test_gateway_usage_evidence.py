"""Real local HTTP requests retain provider usage evidence through ledger writes."""
import json
import unittest
from unittest import mock

import test_gateway_media as media_tests
from test_keys import FakeUpstream


class UsageEvidenceTests(unittest.TestCase):
    setUp = media_tests.GatewayIntegrationTests.setUp
    tearDown = media_tests.GatewayIntegrationTests.tearDown
    team = media_tests.GatewayIntegrationTests.team
    team_client = media_tests.GatewayIntegrationTests.team_client
    enabled_key = media_tests.GatewayIntegrationTests.enabled_key
    call = media_tests.GatewayIntegrationTests.call
    enable_catalog = media_tests.GatewayIntegrationTests.enable_catalog

    def catalog_key(self):
        _, _, key = self.enable_catalog()
        self.service.keys.config["openai"]["models"]["GLM-5"] = [0] * 5
        return key

    def ledger(self):
        with self.service.db() as db:
            return dict(db.execute("SELECT * FROM api_usage ORDER BY id DESC LIMIT 1").fetchone())

    def submit(self, key, usage, *, stream=False, media=False):
        data = {"choices": [{"message": {"content": "ok"}}], "usage": usage}
        if stream:
            reply = b"data: " + json.dumps(data).encode() + b"\n\ndata: [DONE]\n\n"
            FakeUpstream.reply = (200, "text/event-stream", reply)
        else:
            FakeUpstream.reply = (200, "application/json", json.dumps(data).encode())
        path = "/v1/embeddings" if media else "/v1/chat/completions"
        body = {"model": "bge-m3", "input": ["hello"] * 30} if media else {
            "model": "GLM-5", "messages": [{"role": "user", "content": "hello"}]}
        if not media:
            body["stream"] = stream
        status, raw = self.call(path, body, key)
        self.assertEqual(status, 200, raw)
        return self.ledger()

    def test_total_minus_output_uses_same_validated_split_for_charge_and_statistics(self):
        key = self.catalog_key()
        for stream in (False, True):
            with self.subTest(stream=stream):
                row = self.submit(key, {"total_tokens": 100, "completion_tokens": 40}, stream=stream)
                self.assertEqual(row["cost_micro"], 960)  # 60 input * 4 + 40 output * 18
                self.assertEqual((row["input_tokens"], row["output_tokens"]), (60, 40))
                self.assertEqual((row["billing_status"], row["estimated"]), ("settled", 0))

    def test_partial_or_inconsistent_evidence_is_pending_for_chat_and_media(self):
        key = self.catalog_key()
        invalid = ({"completion_tokens": 40}, {"total_tokens": 100},
                   {"total_tokens": 30, "completion_tokens": 40},
                   {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 101}})
        for media in (False, True):
            for stream in (False, True):
                for usage in invalid:
                    with self.subTest(media=media, stream=stream, usage=usage):
                        row = self.submit(key, usage, media=media, stream=stream)
                        self.assertEqual((row["billing_status"], row["cost_micro"]), ("pending", 0))
                        self.assertEqual(row["estimated"], 0)

    def test_inclusive_cache_usage_is_not_double_counted_in_the_ledger(self):
        key = self.catalog_key()
        row = self.submit(key, {"total_tokens": 100, "completion_tokens": 40,
                                "prompt_tokens_details": {"cached_tokens": 20}})
        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]), (40, 20, 40))
        self.assertEqual(row["cost_micro"], 896)  # 40*4 + 20*0.8 + 40*18
        self.assertEqual(row["billing_status"], "settled")

    def test_later_empty_openai_stream_usage_does_not_erase_confirmed_counts(self):
        key = self.catalog_key()
        events = [{"usage": {"prompt_tokens": 60, "completion_tokens": 40}}, {"usage": {}}, {"usage": None}]
        raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
        FakeUpstream.reply = (200, "text/event-stream", raw)
        status, _ = self.call("/v1/chat/completions", {"model": "GLM-5", "messages": [], "stream": True}, key)
        self.assertEqual(status, 200)
        row = self.ledger()
        self.assertEqual((row["input_tokens"], row["output_tokens"], row["cost_micro"]), (60, 40, 960))
        self.assertEqual((row["billing_status"], row["estimated"]), ("settled", 0))

    def test_authoritative_provider_cost_still_overrides_partial_token_evidence(self):
        key = self.catalog_key()
        original = FakeUpstream.end_headers

        def headers(provider):
            provider.send_header("Inference-Cost", "0.123")
            provider.send_header("Inference-Currency", "CNY")
            original(provider)

        with mock.patch.object(FakeUpstream, "end_headers", headers):
            row = self.submit(key, {"completion_tokens": 40})
        self.assertEqual((row["cost_micro"], row["billing_status"]), (123000, "settled"))

    def test_anthropic_input_cache_and_output_events_merge_without_inventing_input(self):
        key = self.catalog_key()
        self.service.keys.config["anthropic"]["models"]["GLM-5"] = [0] * 5
        for stream in (False, True):
            for missing_input, total_only in ((False, False), (True, False), (True, True)):
                with self.subTest(stream=stream, missing_input=missing_input, total_only=total_only):
                    usage = {"total_tokens": 120} if total_only else {"cache_read_input_tokens": 20}
                    if not missing_input:
                        usage["input_tokens"] = 60
                    if stream:
                        events = [
                            {"type": "message_start", "message": {"usage": {**usage, "output_tokens": 1}}},
                            {"type": "message_delta", "usage": {"output_tokens": 40}},
                            {"type": "message_delta", "usage": {"output_tokens": 0, "cache_read_input_tokens": 0}},
                            {"type": "message_stop"},
                        ]
                        raw = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
                        FakeUpstream.reply = (200, "text/event-stream", raw)
                    else:
                        FakeUpstream.reply = (200, "application/json", json.dumps({"usage": {**usage, "output_tokens": 40}}).encode())
                    status, raw = self.call("/v1/messages", {"model": "GLM-5", "messages": [], "stream": stream}, key)
                    self.assertEqual(status, 200, raw)
                    row = self.ledger()
                    if missing_input:
                        self.assertEqual((row["billing_status"], row["cost_micro"]), ("pending", 0))
                    else:
                        self.assertEqual((row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]), (60, 20, 40))
                        self.assertEqual((row["cost_micro"], row["billing_status"]), (976, "settled"))

    def test_non_gitee_missing_usage_keeps_existing_estimate(self):
        _, _, key = self.enabled_key()
        FakeUpstream.reply = (200, "application/json", b'{"choices":[{"message":{"content":"hello"}}]}')
        status, _ = self.call("/v1/chat/completions", {"model": "cheap", "messages": []}, key)
        self.assertEqual(status, 200)
        row = self.ledger()
        self.assertEqual((row["currency"], row["estimated"], row["billing_status"]), ("USD", 1, "settled"))
        self.assertGreater(row["cost_micro"], 0)

    def test_gitee_missing_null_or_malformed_usage_stays_pending(self):
        key = self.catalog_key()
        for fields in ({}, {"usage": None}, {"usage": "invalid"}, {"usage": {}}):
            with self.subTest(fields=fields):
                FakeUpstream.reply = (200, "application/json", json.dumps({
                    "choices": [{"message": {"content": "hello"}}], **fields}).encode())
                status, _ = self.call("/v1/chat/completions", {"model": "GLM-5", "messages": []}, key)
                self.assertEqual(status, 200)
                row = self.ledger()
                self.assertEqual((row["currency"], row["billing_status"], row["cost_micro"]), ("CNY", "pending", 0))
                self.assertEqual((row["input_tokens"], row["output_tokens"], row["estimated"]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
