import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xingye_bot import settings as settings_module


class SettingsProviderRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili-settings-provider-test-"))
        self.config_file = self.tmp / "config.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load_with(self, config, env=None):
        settings_module.write_runtime_config(config)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BILI_AI_")
        } | (env or {})
        with patch.dict(os.environ, env, clear=True):
            return settings_module.load_settings()

    def test_role_providers_can_use_separate_keys_base_urls_and_models(self):
        with patch.object(settings_module, "DATA_DIR", self.tmp), patch.object(settings_module, "CONFIG_FILE", self.config_file):
            loaded = self._load_with({
                "api": {"unified_api_key": "unified-key", "unified_base_url": "https://unified.example/v1"},
                "models": {"chat": "chat-default", "embedding": "embed-default"},
                "providers": {
                    "chat": {"api_key": "chat-key", "base_url": "https://chat.example/v1", "model": "chat-model"},
                    "embedding": {"api_key": "embed-key", "base_url": "https://embed.example/v1", "model": "embed-model"},
                    "image": {"api_key": "image-key", "base_url": "https://image.example/v1", "model": "image-model"},
                },
            })

        self.assertEqual(loaded.providers["chat"]["api_key"], "chat-key")
        self.assertEqual(loaded.providers["chat"]["base_url"], "https://chat.example/v1")
        self.assertEqual(loaded.providers["chat"]["model"], "chat-model")
        self.assertEqual(loaded.providers["embedding"]["api_key"], "embed-key")
        self.assertEqual(loaded.providers["image"]["base_url"], "https://image.example/v1")
        self.assertTrue(loaded.configured)

    def test_missing_role_provider_falls_back_to_unified_api_and_role_model(self):
        with patch.object(settings_module, "DATA_DIR", self.tmp), patch.object(settings_module, "CONFIG_FILE", self.config_file):
            loaded = self._load_with({
                "api": {"unified_api_key": "unified-key", "unified_base_url": "https://unified.example/v1"},
                "models": {"fast": "fast-model"},
            })

        self.assertEqual(loaded.provider_for_role("fast")["api_key"], "unified-key")
        self.assertEqual(loaded.provider_for_role("fast")["base_url"], "https://unified.example/v1")
        self.assertEqual(loaded.provider_for_role("fast")["model"], "fast-model")

    def test_role_provider_env_overrides_are_supported(self):
        with patch.object(settings_module, "DATA_DIR", self.tmp), patch.object(settings_module, "CONFIG_FILE", self.config_file):
            loaded = self._load_with(
                {"api": {"unified_api_key": "unified-key", "unified_base_url": "https://unified.example/v1"}},
                {
                    "BILI_AI_EMBEDDING_API_KEY": "env-embed-key",
                    "BILI_AI_EMBEDDING_BASE_URL": "https://env-embed.example/v1",
                    "BILI_AI_EMBEDDING_MODEL": "env-embed-model",
                },
            )

        self.assertEqual(loaded.provider_for_role("embedding")["api_key"], "env-embed-key")
        self.assertEqual(loaded.provider_for_role("embedding")["base_url"], "https://env-embed.example/v1")
        self.assertEqual(loaded.provider_for_role("embedding")["model"], "env-embed-model")

    def test_configured_role_provider_wins_over_default_environment_values(self):
        with patch.object(settings_module, "DATA_DIR", self.tmp), patch.object(settings_module, "CONFIG_FILE", self.config_file):
            loaded = self._load_with(
                {
                    "api": {"unified_api_key": "unified-key", "unified_base_url": "https://unified.example/v1"},
                    "providers": {
                        "chat": {"api_key": "chat-key", "base_url": "https://chat.example/v1", "model": "chat-model"},
                    },
                },
                {
                    "BILI_AI_BASE_URL": "https://env-default.example/v1",
                    "BILI_AI_MODEL_CHAT": "env-chat-model",
                    "BILI_AI_CHAT_MODEL": "env-role-chat-model",
                },
            )

        self.assertEqual(loaded.provider_for_role("chat")["api_key"], "chat-key")
        self.assertEqual(loaded.provider_for_role("chat")["base_url"], "https://chat.example/v1")
        self.assertEqual(loaded.provider_for_role("chat")["model"], "chat-model")

    def test_public_config_does_not_expose_provider_api_keys(self):
        settings = settings_module.BotSettings(
            api_key="unified-key",
            base_url="https://unified.example/v1",
            models={"chat": "chat-model"},
            providers={"chat": {"api_key": "secret-chat-key", "base_url": "https://chat.example/v1", "model": "chat-model"}},
        )

        public = settings_module.public_config(settings)

        self.assertTrue(public["providers"]["chat"]["configured"])
        self.assertEqual(public["providers"]["chat"]["base_url"], "https://chat.example/v1")
        self.assertEqual(public["providers"]["chat"]["model"], "chat-model")
        self.assertNotIn("api_key", public["providers"]["chat"])
        self.assertNotIn("secret-chat-key", repr(public))


if __name__ == "__main__":
    unittest.main()
