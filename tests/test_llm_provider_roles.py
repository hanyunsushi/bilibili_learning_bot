import base64
import unittest
from unittest.mock import AsyncMock, Mock, patch

from xingye_bot.llm import ModelClient
from xingye_bot.settings import BotSettings


class LlmProviderRolesTest(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        settings = BotSettings(
            api_key="unified-key",
            base_url="https://unified.example/v1",
            models={"chat": "chat-model", "image": "image-model", "embedding": "embed-model"},
            fallback_models={},
            providers={
                "chat": {"api_key": "chat-key", "base_url": "https://chat.example/v1", "model": "chat-model"},
                "image": {"api_key": "image-key", "base_url": "https://image.example/v1", "model": "image-model"},
                "embedding": {"api_key": "embed-key", "base_url": "https://embed.example/v1", "model": "embed-model"},
            },
        )
        state = Mock()
        state.record_cost = Mock()
        return ModelClient(settings, state)

    @patch("httpx.AsyncClient")
    async def test_chat_uses_chat_provider_credentials_and_base_url(self, async_client_cls):
        post = AsyncMock(return_value=Mock(status_code=200, json=Mock(return_value={
            "choices": [{"message": {"content": "OK"}}],
        })))
        async_client_cls.return_value.__aenter__.return_value.post = post

        result = await self._client().chat([{"role": "user", "content": "hi"}])

        self.assertEqual(result, "OK")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://chat.example/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer chat-key")
        self.assertEqual(kwargs["json"]["model"], "chat-model")

    @patch("httpx.AsyncClient")
    async def test_embedding_uses_embedding_provider_credentials_and_base_url(self, async_client_cls):
        post = AsyncMock(return_value=Mock(status_code=200, json=Mock(return_value={
            "data": [{"embedding": [0.1, 0.2]}],
        })))
        async_client_cls.return_value.__aenter__.return_value.post = post

        vector = await self._client().embedding("hello")

        self.assertEqual(vector, [0.1, 0.2])
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://embed.example/v1/embeddings")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer embed-key")
        self.assertEqual(kwargs["json"]["model"], "embed-model")

    @patch("httpx.AsyncClient")
    async def test_image_generation_uses_image_provider_credentials_and_base_url(self, async_client_cls):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
        post = AsyncMock(return_value=Mock(status_code=200, json=Mock(return_value={
            "data": [{"b64_json": png}],
        })))
        async_client_cls.return_value.__aenter__.return_value.post = post

        result = await self._client().generate_image("a logo")

        self.assertEqual(result["model"], "image-model")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://image.example/v1/images/generations")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer image-key")
        self.assertEqual(kwargs["json"]["model"], "image-model")


if __name__ == "__main__":
    unittest.main()
