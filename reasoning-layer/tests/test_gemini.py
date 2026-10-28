import json
import unittest
from unittest.mock import patch

import httpx
from google import genai
from google.genai import errors, types

from reasoning.contracts import generation_schema
from reasoning.gemini import GeminiProvider
from reasoning.planner import GenerationError


class GeminiTests(unittest.TestCase):
    @patch("google.genai.Client")
    def test_sdk_request_uses_json_instructions_and_developer_api(self, client_class):
        client = client_class.return_value.__enter__.return_value
        client.models.generate_content.return_value = types.GenerateContentResponse(candidates=[
            types.Candidate(finish_reason="STOP", content=types.Content(parts=[
                types.Part(text='{"status":"infeasible","reasons":["No capability"]}')
            ]))
        ])
        provider = GeminiProvider("test-not-a-real-key", model="test-model", timeout_ms=5000)
        schema = generation_schema()
        response = provider.generate(system="system", prompt="mission", schema=schema)
        self.assertEqual(json.loads(response)["status"], "infeasible")
        client_class.assert_called_once()
        config = client_class.call_args.kwargs
        self.assertFalse(config["vertexai"])
        self.assertEqual(config["http_options"].timeout, 5000)
        self.assertEqual(config["http_options"].retry_options.attempts, 1)
        self.assertEqual(config["http_options"].base_url, "https://generativelanguage.googleapis.com")
        call = client.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["contents"], "mission")
        self.assertTrue(call["config"].system_instruction.startswith("system\n"))
        self.assertIn(json.dumps(schema), call["config"].system_instruction)
        self.assertIsNone(call["config"].response_schema)
        self.assertEqual(call["config"].max_output_tokens, 2048)
        self.assertIsNone(call["config"].tools)

    @patch("google.genai.Client")
    def test_raw_api_error_is_not_exposed(self, client_class):
        client = client_class.return_value.__enter__.return_value
        client.models.generate_content.side_effect = errors.ClientError(
            403, {"error": {"message": "URL contains sensitive-api-key-in-error", "code": 403}}
        )
        with self.assertRaises(GenerationError) as caught:
            GeminiProvider("sensitive-api-key-in-error").generate(system="x", prompt="x", schema={})
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("sensitive-api-key", str(caught.exception))

    @patch("google.genai.Client")
    def test_truncated_and_blocked_outputs_are_rejected(self, client_class):
        client = client_class.return_value.__enter__.return_value
        for candidates in ([], [types.Candidate(finish_reason="MAX_TOKENS")],
                           [types.Candidate(finish_reason="SAFETY")]):
            with self.subTest(candidates=candidates):
                client.models.generate_content.return_value = types.GenerateContentResponse(candidates=candidates)
                with self.assertRaises(GenerationError):
                    GeminiProvider("test-key").generate(system="x", prompt="x", schema={})

    def test_missing_key_fails_without_request(self):
        with self.assertRaises(ValueError):
            GeminiProvider(" ")

    @patch("google.genai.Client")
    def test_oversized_prompt_fails_before_client_is_created(self, client_class):
        with self.assertRaises(GenerationError):
            GeminiProvider("test-key", max_prompt_chars=1000).generate(
                system="s", prompt="x" * 1001, schema={}
            )
        client_class.assert_not_called()

    def test_real_sdk_serialization_and_quota_error_without_network(self):
        real_client = genai.Client
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {
                "code": 429, "message": "Quota exhausted", "status": "RESOURCE_EXHAUSTED"
            }})

        def client_factory(**kwargs):
            kwargs["http_options"].client_args = {"transport": httpx.MockTransport(respond)}
            return real_client(**kwargs)

        with patch("google.genai.Client", side_effect=client_factory):
            with self.assertRaises(GenerationError) as caught:
                GeminiProvider("test-key").generate(
                    system="rules", prompt="mission", schema=generation_schema()
                )
        self.assertIn("429", str(caught.exception))
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 2048)
        instructions = payload["systemInstruction"]["parts"][0]["text"]
        self.assertIn(json.dumps(generation_schema()), instructions)
        self.assertNotIn("responseSchema", payload["generationConfig"])
        self.assertNotIn("responseFormat", payload["generationConfig"])
        self.assertNotIn("tools", payload)
