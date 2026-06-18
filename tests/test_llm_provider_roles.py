import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path
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
    async def test_chat_injects_runtime_prompt_skills(self, async_client_cls):
        data_dir = Path(tempfile.mkdtemp(prefix="bili-llm-prompt-skills-"))
        try:
            (data_dir / "web_personas.json").write_text(json.dumps({
                "active": "学习搭子",
                "items": {"学习搭子": {"name": "学习搭子"}},
            }, ensure_ascii=False), encoding="utf-8")
            (data_dir / "web_prompt_skills.json").write_text(json.dumps({
                "items": [
                    {"name": "全局风格", "scope": "global", "content": "所有回复先给结论。"},
                    {"name": "人格补丁", "scope": "persona", "persona": "学习搭子", "content": "用苏格拉底式追问。"},
                    {"name": "其他人格", "scope": "persona", "persona": "旁观者", "content": "不应出现。"},
                ]
            }, ensure_ascii=False), encoding="utf-8")
            post = AsyncMock(return_value=Mock(status_code=200, json=Mock(return_value={
                "choices": [{"message": {"content": "OK"}}],
            })))
            async_client_cls.return_value.__aenter__.return_value.post = post

            with patch("xingye_bot.llm.DATA_DIR", data_dir):
                await self._client().chat([{"role": "user", "content": "hi"}])

            payload = post.call_args.kwargs["json"]
            joined = "\n".join(str(message.get("content", "")) for message in payload["messages"])
            self.assertIn("项目 Prompt Skills", joined)
            self.assertIn("所有回复先给结论。", joined)
            self.assertIn("用苏格拉底式追问。", joined)
            self.assertNotIn("不应出现。", joined)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

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
