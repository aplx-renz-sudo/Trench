import unittest
from unittest.mock import patch

import trch_providers


class ProviderAdapterTests(unittest.TestCase):
    def test_mistral_uses_openai_compatible_chat_api(self):
        response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(trch_providers, "_request_json", return_value=response) as request:
            result = trch_providers.chat(
                "mistral", "mistral-small-latest", [{"role": "user", "content": "hi"}],
                environ={"MISTRAL_API_KEY": "test-key"},
            )

        self.assertEqual(result, "hello")
        self.assertEqual(request.call_args.args[0], "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(request.call_args.args[2]["Authorization"], "Bearer test-key")

    def test_kimi_alias_uses_moonshot_api(self):
        response = {"choices": [{"message": {"content": "hello"}}]}
        with patch.object(trch_providers, "_request_json", return_value=response) as request:
            result = trch_providers.chat(
                "kimi/moonshot", None, [{"role": "user", "content": "hi"}],
                environ={"MOONSHOT_API_KEY": "test-key"},
            )

        self.assertEqual(result, "hello")
        self.assertEqual(request.call_args.args[0], "https://api.moonshot.ai/v1/chat/completions")
        self.assertEqual(request.call_args.args[1]["model"], "moonshot-v1-8k")

    def test_anthropic_sends_system_separately(self):
        response = {"content": [{"type": "text", "text": "hello"}]}
        with patch.object(trch_providers, "_request_json", return_value=response) as request:
            result = trch_providers.chat(
                "claude", None,
                [{"role": "system", "content": "be concise"}, {"role": "user", "content": "hi"}],
                environ={"ANTHROPIC_API_KEY": "test-key"},
            )

        self.assertEqual(result, "hello")
        self.assertEqual(request.call_args.args[1]["system"], "be concise")

    def test_ollama_uses_local_server_without_cli(self):
        response = {"message": {"content": "hello"}}
        with patch.object(trch_providers, "_request_json", return_value=response) as request:
            result = trch_providers.chat(
                "ollama", "qwen2.5", [{"role": "user", "content": "hi"}],
                environ={"OLLAMA_HOST": "localhost:11434"},
            )

        self.assertEqual(result, "hello")
        self.assertEqual(request.call_args.args[0], "http://localhost:11434/api/chat")

    def test_missing_provider_key_is_reported(self):
        with self.assertRaisesRegex(trch_providers.ProviderError, "OPENAI_API_KEY"):
            trch_providers.chat("openai", None, [{"role": "user", "content": "hi"}], environ={})

    def test_compatible_provider_reports_missing_key(self):
        with self.assertRaisesRegex(trch_providers.ProviderError, "TRCH_OPENAI_COMPAT_API_KEY"):
            trch_providers.chat(
                "compatible", "custom-model", [{"role": "user", "content": "hi"}],
                environ={"TRCH_OPENAI_COMPAT_BASE_URL": "https://example.test/v1"},
            )


if __name__ == "__main__":
    unittest.main()