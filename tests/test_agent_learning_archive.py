import shutil
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

if "colorama" in sys.modules:
    fore = getattr(sys.modules["colorama"], "Fore", types.SimpleNamespace())
    for name in (
        "WHITE", "GREEN", "RED", "YELLOW", "CYAN", "BLUE", "MAGENTA",
        "BLACK", "RESET",
        "LIGHTBLUE_EX", "LIGHTBLACK_EX", "LIGHTMAGENTA_EX", "LIGHTCYAN_EX",
        "LIGHTYELLOW_EX", "LIGHTGREEN_EX", "LIGHTRED_EX", "LIGHTWHITE_EX",
    ):
        if not hasattr(fore, name):
            setattr(fore, name, "")

import new_agent


class LearningArchiveFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili-archive-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_video_archive_writes_fallback_when_ai_summary_fails(self):
        category_dir = self.tmp / "AI工具"
        category_dir.mkdir(parents=True)
        classifier = Mock()
        classifier.classify_content.return_value = "AI工具"
        classifier.get_or_create_folder.return_value = str(category_dir)
        classifier.show_category_structure = Mock()

        brain = new_agent.AgentBrain.__new__(new_agent.AgentBrain)
        brain.classifier = classifier
        brain.kb_search = None
        brain.write_learning_log = Mock()
        brain._call_ai_with_retry = AsyncMock(side_effect=RuntimeError("blocked"))

        subtitle = "这是一段用于测试知识归档保底能力的字幕。" * 12

        with patch.object(new_agent, "AI_SUBTITLE_VERIFY_ENABLED", False), \
             patch.object(new_agent, "ModelClient", None), \
             patch.object(new_agent.openai.ChatCompletion, "create", side_effect=RuntimeError("blocked")):
            ok = await brain.learn_from_video(
                "BV1TEST0001",
                "测试归档视频",
                "测试UP",
                "https://www.bilibili.com/video/BV1TEST0001",
                subtitle,
                "AI工具",
                video_desc="测试简介",
                score=new_agent.LEARN_MIN_SCORE,
            )

        self.assertTrue(ok)
        files = list(category_dir.glob("*.md"))
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")
        self.assertIn("AI 总结暂不可用", content)
        self.assertIn("原始可学内容摘录", content)
        self.assertIn("测试归档视频", content)

    def test_classifier_uses_topic_suggestion_without_extra_ai_call(self):
        metadata_file = self.tmp / "metadata.json"
        knowledge_dir = self.tmp / "KnowledgeBase"
        knowledge_dir.mkdir()

        with patch.object(new_agent, "KB_METADATA_FILE", str(metadata_file)), \
             patch.object(new_agent, "KNOWLEDGE_BASE_DIR", str(knowledge_dir)):
            classifier = new_agent.KnowledgeBaseClassifier()
            classifier._find_best_category = Mock(side_effect=AssertionError("should not call classifier AI"))

            category = classifier.classify_content(
                "测试视频",
                "这是一段测试内容" * 80,
                "BV1TEST0002",
                topic_suggestion="足球战术分析",
            )

        self.assertEqual(category, "足球战术分析")
        classifier._find_best_category.assert_not_called()

    async def test_knowledge_verify_uses_model_client_before_legacy_openai(self):
        class FakeModelClient:
            def __init__(self, settings, state):
                pass

            async def chat(self, messages, model_role="chat", purpose="chat"):
                return '{"overall_reliable": true, "overall_score": 0.91, "issues": [], "supplements": [], "recommend_rewrite": false}'

        with patch.object(new_agent, "ModelClient", FakeModelClient), \
             patch.object(new_agent, "load_modular_settings", Mock(return_value=object())), \
             patch.object(new_agent, "BotState", Mock(return_value=object())), \
             patch.object(new_agent.openai.ChatCompletion, "create", side_effect=AssertionError("legacy openai should not be called")):
            result = await new_agent.verify_knowledge_with_ai("测试知识", "测试视频", [])

        self.assertTrue(result["overall_reliable"])
        self.assertEqual(result["overall_score"], 0.91)

    def test_lock_file_with_current_pid_is_treated_as_stale_before_acquire(self):
        lock_file = self.tmp / "bot.lock"
        lock_file.write_text(str(os.getpid()), encoding="utf-8")

        old_lock_file = new_agent._BOT_LOCK_FILE
        old_acquired = new_agent._bot_lock_acquired
        try:
            new_agent._BOT_LOCK_FILE = str(lock_file)
            new_agent._bot_lock_acquired = False

            acquired = new_agent._acquire_bot_lock()

            self.assertTrue(acquired)
            self.assertEqual(lock_file.read_text(encoding="utf-8"), str(os.getpid()))
        finally:
            new_agent._release_bot_lock()
            new_agent._BOT_LOCK_FILE = old_lock_file
            new_agent._bot_lock_acquired = old_acquired

    def test_lock_file_with_non_bot_pid_is_treated_as_stale_before_acquire(self):
        lock_file = self.tmp / "bot.lock"
        lock_file.write_text("1", encoding="utf-8")

        old_lock_file = new_agent._BOT_LOCK_FILE
        old_acquired = new_agent._bot_lock_acquired
        try:
            new_agent._BOT_LOCK_FILE = str(lock_file)
            new_agent._bot_lock_acquired = False

            acquired = new_agent._acquire_bot_lock()

            self.assertTrue(acquired)
            self.assertEqual(lock_file.read_text(encoding="utf-8"), str(os.getpid()))
        finally:
            new_agent._release_bot_lock()
            new_agent._BOT_LOCK_FILE = old_lock_file
            new_agent._bot_lock_acquired = old_acquired


if __name__ == "__main__":
    unittest.main()
