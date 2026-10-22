"""部署配置端点与可选 Embeddings 调用边界。"""

import unittest

from model_api import ChatCompletionsClient, LLMConfig, ModelServiceError


class LLMConfigTests(unittest.TestCase):
    def test_config_keeps_explicit_api_paths_and_hides_key(self):
        config = LLMConfig(base_url="https://example.com/v1/", api_key="test-secret",
                           chat_path="/chat/completions", embedding_model="embed-model")
        self.assertEqual(config.chat_url, "https://example.com/v1/chat/completions")
        self.assertEqual(config.embeddings_url, "https://example.com/v1/embeddings")
        self.assertNotIn("test-secret", repr(config))
        self.assertEqual(LLMConfig(base_url=config.chat_url).chat_url, config.chat_url)

    def test_invalid_paths_and_embedding_model_are_rejected(self):
        for values in ({"chat_path": "chat/completions"}, {"chat_path": "//example.com/path"},
                       {"chat_path": "/chat?key=secret"}, {"embeddings_path": "/embed#fragment"},
                       {"embedding_model": ""}, {"embedding_model": 3}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LLMConfig(**values)


class EmbeddingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_client_honors_custom_endpoint_without_suffix_guessing(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

        client = ChatCompletionsClient(
            LLMConfig(base_url="https://example.com/api", chat_path="/completions-custom"),
            transport=transport,
        )
        await client(system_prompt="system", user_prompt="user", response_format={
            "type": "json_schema", "name": "example", "schema": {"type": "object"}})
        self.assertEqual(calls[0]["url"], "https://example.com/api/completions-custom")

    async def test_embedding_requires_explicit_model_without_network_fallback(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return {}

        with self.assertRaises(NotImplementedError):
            await ChatCompletionsClient(transport=transport).embed_query("陶瓷")
        self.assertEqual(calls, [])

    async def test_embedding_uses_configured_model_and_checks_numeric_vector(self):
        calls = []

        async def transport(**kwargs):
            calls.append(kwargs)
            return {"data": [{"index": 0, "embedding": [0.1, -0.2, 0]}]}

        client = ChatCompletionsClient(
            LLMConfig(embedding_model="embedding-model"), transport=transport)
        self.assertEqual(await client.embed_query(" 陶瓷 "), (0.1, -0.2, 0.0))
        self.assertEqual(calls[0]["payload"], {
            "model": "embedding-model", "input": "陶瓷", "encoding_format": "float"})
        self.assertTrue(calls[0]["url"].endswith("/embeddings"))

    async def test_embedding_rejects_empty_zero_nonfinite_or_malformed_results(self):
        cases = ({}, {"data": []}, {"data": [{"index": 1, "embedding": [1]}]},
                 {"data": [{"embedding": []}]}, {"data": [{"embedding": [0, 0]}]},
                 {"data": [{"embedding": [True]}]}, {"data": [{"embedding": [float("inf")]}]},
                 {"data": [{"embedding": ["0.2"]}]})
        for value in cases:
            async def transport(**kwargs):
                return value
            client = ChatCompletionsClient(LLMConfig(embedding_model="embed"), transport=transport)
            with self.subTest(value=value), self.assertRaises(ModelServiceError):
                await client.embed_query("陶瓷")


if __name__ == "__main__":
    unittest.main()
