"""Billing regression tests use only a frozen public catalog; no network/API spend."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

from gateway_pricing import Catalog, PricingError, quote


class PricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog()

    def snapshot(self, model, path="/v1/chat/completions", body=None):
        return self.catalog.resolve(model, path, body)

    def test_catalog_has_every_known_model_but_does_not_invent_missing_prices(self):
        self.assertEqual(len(self.catalog.model_ids(include_unavailable=True)), 214)
        self.assertEqual(len(self.catalog.model_ids()), 211)
        for model in ("VoxCPM2", "VoxCPM2-Pro", "comfyui"):
            self.assertFalse(self.catalog.model_info(model)["available"])
            with self.assertRaises(PricingError):
                self.snapshot(model, "/v1/audio/speech")

    def test_namespace_case_matching_and_path_enforcement(self):
        self.assertEqual(self.snapshot("glm/GLM-5")["model"], "GLM-5")
        with self.assertRaises(PricingError):
            self.snapshot("GLM-5", "/v1/images/generations")
        with self.assertRaises(PricingError):
            self.snapshot("unknown")

    def test_snapshot_is_independent_and_json_serializable(self):
        import json
        first = self.snapshot("GLM-5")
        first["operations"].clear()
        second = json.loads(json.dumps(self.snapshot("GLM-5")))
        self.assertEqual(len(second["operations"]), 1)

    def test_tier_boundary_whole_order_not_marginal(self):
        s = self.snapshot("GLM-5")
        self.assertEqual(quote(s, usage=[32000, 1000, 0, 0, 0])["cost_micro"], 146000)
        self.assertEqual(quote(s, usage=[32001, 1000, 0, 0, 0])["cost_micro"], 214006)

    def test_cache_is_part_of_context_and_not_double_charged(self):
        s = self.snapshot("GLM-5")
        r = quote(s, usage={"prompt_tokens": 40000, "completion_tokens": 1000,
                            "prompt_tokens_details": {"cached_tokens": 10000}})
        self.assertEqual(r["cost_micro"], 214000)  # 30k*6 + 10k*1.2 + 1k*22
        self.assertEqual(r["details"]["tier"], [32000, None])
        a = quote(s, usage={"input_tokens": 30000, "output_tokens": 1000,
                            "cache_read_input_tokens": 10000})
        self.assertEqual(a["cost_micro"], r["cost_micro"])

    def test_cache_write_ttls_share_published_moark_rate(self):
        s = self.snapshot("Kimi-K2.5")
        normalized = quote(s, usage=[1000, 100, 1000, 2000, 500])
        anthropic = quote(s, usage={"input_tokens": 1000, "output_tokens": 100,
                                   "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 1500,
                                   "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                                      "ephemeral_1h_input_tokens": 500}})
        self.assertEqual(normalized["cost_micro"], 13700)
        self.assertEqual(anthropic["cost_micro"], normalized["cost_micro"])

    def test_invalid_cache_counters_are_unsettled(self):
        s = self.snapshot("GLM-5")
        r = quote(s, usage={"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 101}})
        self.assertTrue(r["pending"])
        self.assertIsNone(r["cost_micro"])

    def test_off_peak_boundaries_and_timezone(self):
        s = self.snapshot("deepseek-v4.1-flash")
        for local, expected in (("08:59:59", 0.5), ("09:00:00", 1), ("12:00:00", 0.5),
                                ("14:00:00", 1), ("18:00:00", 0.5), ("23:59:59", 0.5), ("00:00:00", 0.5)):
            at = "2026-11-07T" + local + "+08:00"
            r = quote(s, usage=[1000, 100, 0, 1000, 0], started_at=at)
            self.assertEqual(r["cost_micro"], round(2840 * expected), at)
        utc = datetime(2026, 11, 7, 1, tzinfo=timezone.utc)
        self.assertEqual(quote(s, usage=[1000, 100, 0, 1000, 0], started_at=utc)["cost_micro"], 2840)
        self.assertTrue(quote(s, usage=[1000, 0, 0, 0, 0])["pending"])
        self.assertTrue(quote(s, usage=[1000, 0, 0, 0, 0], started_at="2026-11-07T09:00:00")["pending"])

    def test_inactive_off_peak_map_does_not_override_cache_discount(self):
        s = self.snapshot("Kimi-K2.5")
        r = quote(s, usage=[0, 0, 0, 1000000, 0], started_at="2026-11-07T02:00:00+08:00")
        self.assertEqual(r["cost_micro"], 800000)
        self.assertFalse(r["details"]["off_peak"])

    def test_authoritative_header_wins_and_explicit_zero_is_real(self):
        s = self.snapshot("GLM-5")
        r = quote(s, usage=[1000, 1000, 0, 0, 0], headers={"Inference-Cost": "0.1234567"})
        self.assertEqual(r["cost_micro"], 123457)
        self.assertEqual(quote(s, headers={"inference-cost": "0"})["cost_micro"], 0)
        self.assertTrue(quote(s, headers={"Inference-Cost": "NaN"})["pending"])
        self.assertTrue(quote(s, headers={"Inference-Cost": "-1"})["pending"])
        self.assertTrue(quote(s, headers={"Inference-Cost": "1", "Inference-Currency": "USD"})["pending"])

    def test_clients_cannot_supply_costs(self):
        s = self.snapshot("GLM-5")
        r = quote(s, request={"price": 0, "cost": 0, "Inference-Cost": 0})
        self.assertTrue(r["pending"])

    def test_tasks_only_settle_at_terminal_state(self):
        s = self.snapshot("HappyHorse-1.0", "/v1/async/videos/generations")
        for status in ("waiting", "in_progress"):
            self.assertTrue(quote(s, response={"task_id": "x", "status": status, "price": 0})["pending"])
        self.assertEqual(quote(s, response={"task_id": "x", "status": "success", "price": "3.6"})["cost_micro"], 3600000)
        self.assertEqual(quote(s, response={"task_id": "x", "status": "cancelled", "price": 0})["cost_micro"], 0)
        self.assertTrue(quote(s, response={"task_id": "x", "status": "failure"})["pending"])

    def test_image_size_selects_correct_operation_and_counts_returned_images(self):
        request = {"size": "1536x1536", "n": 4}
        s = self.snapshot("flux-1-schnell", "/v1/images/generations", request)
        r = quote(s, request, {"data": [{"url": "a"}, {"url": "b"}]})
        self.assertEqual(r["cost_micro"], 20000)
        self.assertFalse(r["estimated"])
        self.assertEqual(r["details"]["operation_ids"], [63])

    def test_per_call_does_not_become_per_image(self):
        s = self.snapshot("flux-1-schnell", "/v1/images/generations", {"size": "2048x2048"})
        r = quote(s, response={"data": [{"url": "a"}, {"url": "b"}]})
        self.assertEqual(r["cost_micro"], 12000)
        self.assertEqual(r["details"]["unit"], "call")

    def test_embedding_batch_selects_free_or_paid_operation(self):
        path = "/v1/embeddings"
        small = self.snapshot("bge-m3", path, {"input": ["x"] * 25})
        large = self.snapshot("bge-m3", path, {"input": ["x"] * 26})
        self.assertEqual(quote(small, usage={"prompt_tokens": 1000})["cost_micro"], 0)
        self.assertEqual(quote(large, usage={"prompt_tokens": 1000})["cost_micro"], 70)
        self.assertTrue(quote(self.snapshot("bge-m3", path), usage={"prompt_tokens": 1000})["pending"])

    def test_video_resolution_multiplies_the_base_price(self):
        s = self.snapshot("HappyHorse-1.0", "/v1/async/videos/generations")
        r = quote(s, request={"resolution": "720P"}, response={"duration": 5})
        self.assertEqual(r["cost_micro"], 3600000)
        s = self.snapshot("Duix-Avatar", "/v1/async/videos/audio-video-to-video")
        r = quote(s, response={"duration": 20, "width": 1920, "height": 1080})
        self.assertEqual(r["cost_micro"], 6000000)

    def test_document_missing_page_count_never_charged_as_zero(self):
        s = self.snapshot("MinerU2.5", "/v1/async/documents/parse")
        self.assertTrue(quote(s, response={"output": {"markdown": "..."}})["pending"])
        r = quote(s, response={"page_count": 5})
        self.assertEqual(r["cost_micro"], 100000)

    def test_tts_text_estimate_is_labeled_and_binary_size_is_not_usage(self):
        s = self.snapshot("TeleTTS-Mandarin", "/v1/audio/speech")
        r = quote(s, request={"input": "hello"}, response={"_binary": {"bytes": 10000}})
        self.assertEqual(r["cost_micro"], 1400)
        self.assertTrue(r["estimated"])
        self.assertTrue(quote(s, response={"_binary": {"bytes": 10000}})["pending"])

    def test_speech_seconds_use_measured_duration(self):
        s = self.snapshot("TeleASR-MultiDialect", "/v1/async/audio/transcriptions")
        r = quote(s, response={"duration": 10})
        self.assertEqual(r["cost_micro"], 8000)
        self.assertFalse(r["estimated"])

    def test_lora_cost_requires_billed_amount(self):
        s = self.snapshot("flux-1-schnell", "/v1/images/generations", {"size": "1536x1536"})
        request = {"lora_weights": [{"url": "https://example.invalid/lora"}]}
        self.assertTrue(quote(s, request, {"data": [{"url": "a"}]})["pending"])
        self.assertEqual(quote(s, request, headers={"Inference-Cost": "0.1"})["cost_micro"], 100000)

    def test_unknown_policy_is_pending_not_free(self):
        s = self.snapshot("GLM-5")
        s["operations"][0]["billing_policy"]["type"] = "future_policy"
        self.assertTrue(quote(s, usage=[1000, 1000, 0, 0, 0])["pending"])

    def test_explicit_compute_uses_specified_not_average_rate(self):
        model = "Sinong1.0-32B"
        ordinary = self.snapshot(model)
        compute = ordinary["operations"][0]["compute"]
        specific = self.catalog.resolve(model, "/v1/chat/completions", headers={"X-Compute-Id": compute})
        self.assertEqual(quote(ordinary, usage=[1000, 1000, 0, 0, 0])["cost_micro"], 16000)
        self.assertEqual(quote(specific, usage=[1000, 1000, 0, 0, 0])["cost_micro"], 4000)

    def test_unknown_billing_units_and_negative_quantities_are_pending(self):
        s = self.snapshot("MinerU2.5", "/v1/async/documents/parse")
        self.assertTrue(quote(s, response={"page_count": -1})["pending"])
        s["operations"][0]["unit"] = "future_unit"
        self.assertTrue(quote(s, response={"page_count": 1})["pending"])

    def test_controlnet_variant_does_not_use_basic_image_price(self):
        body = {"control_image": "https://example.invalid/image.png"}
        s = self.snapshot("z-image-turbo", "/v1/images/generations", body)
        self.assertEqual([o["id"] for o in s["operations"]], [541])
        self.assertEqual(quote(s, body, {"data": [{"url": "a"}]})["cost_micro"], 100000)

    def test_separate_trusted_quantities_preserve_privacy_without_client_spoofing(self):
        s = self.snapshot("TeleTTS-Mandarin", "/v1/audio/speech")
        body = {"_billing_text_characters": 5, "_gateway_measured": {"characters": 5}, "trusted_quantities": {"characters": 5}}
        self.assertTrue(quote(s, request=body)["pending"])
        r = quote(s, trusted_quantities={"characters": 5})
        self.assertEqual(r["cost_micro"], 1400)
        self.assertTrue(r["estimated"])
        r = quote(s, trusted_quantities={"characters": 5}, response={"usage": {"characters": 10}})
        self.assertEqual(r["cost_micro"], 2800)
        self.assertFalse(r["estimated"])

    def test_extreme_upstream_cost_cannot_overflow_the_ledger(self):
        s = self.snapshot("GLM-5")
        for value in ("1e99", "1e20", "Infinity"):
            self.assertTrue(quote(s, headers={"Inference-Cost": value})["pending"])

    def test_both_cost_currency_headers_are_supported_and_conflicts_are_unsettled(self):
        s = self.snapshot("GLM-5")
        self.assertEqual(quote(s, headers={"Inference-Cost": "1", "Inference-Cost-Currency": "CNY"})["cost_micro"], 1000000)
        self.assertTrue(quote(s, headers={"Inference-Cost": "1", "Inference-Cost-Currency": "USD"})["pending"])
        self.assertTrue(quote(s, headers={"Inference-Cost": "1", "Inference-Currency": "CNY", "Inference-Cost-Currency": "USD"})["pending"])
        self.assertTrue(quote(s, response={"currency": "CNY"}, headers={"Inference-Cost": "1", "Inference-Currency": "USD"})["pending"])


if __name__ == "__main__":
    unittest.main()
