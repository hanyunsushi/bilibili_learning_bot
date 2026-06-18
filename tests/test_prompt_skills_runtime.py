import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from xingye_bot.prompt_skills import inject_runtime_prompt_skills, load_prompt_skills, runtime_prompt_skill_block


class PromptSkillsRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili-prompt-runtime-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_runtime_prompt_skills(self):
        (self.tmp / "web_personas.json").write_text(json.dumps({
            "active": "学习搭子",
            "items": {"学习搭子": {"name": "学习搭子"}},
        }, ensure_ascii=False), encoding="utf-8")
        (self.tmp / "web_prompt_skills.json").write_text(json.dumps({
            "items": [
                {"name": "全局风格", "scope": "global", "content": "所有回复先给结论。"},
                {"name": "人格补丁", "scope": "persona", "persona": "学习搭子", "content": "用苏格拉底式追问。"},
                {"name": "其他人格", "scope": "persona", "persona": "旁观者", "content": "不应出现。"},
            ]
        }, ensure_ascii=False), encoding="utf-8")

    def test_loads_global_and_active_persona_prompt_skills(self):
        self._write_runtime_prompt_skills()

        items = load_prompt_skills(data_dir=self.tmp)

        self.assertEqual([item["name"] for item in items], ["全局风格", "人格补丁"])
        block = runtime_prompt_skill_block(data_dir=self.tmp)
        self.assertIn("所有回复先给结论。", block)
        self.assertIn("用苏格拉底式追问。", block)
        self.assertNotIn("不应出现。", block)

    def test_injects_prompt_skill_block_once(self):
        self._write_runtime_prompt_skills()
        messages = [{"role": "system", "content": "原系统提示"}, {"role": "user", "content": "hi"}]

        once = inject_runtime_prompt_skills(messages, data_dir=self.tmp)
        twice = inject_runtime_prompt_skills(once, data_dir=self.tmp)

        self.assertEqual(len([m for m in twice if "项目 Prompt Skills" in str(m.get("content", ""))]), 1)
        self.assertEqual(twice[0]["content"], "原系统提示")
        self.assertIn("项目 Prompt Skills", twice[1]["content"])

    def test_legacy_interaction_service_openai_calls_inject_prompt_skills(self):
        self._write_runtime_prompt_skills()
        if "colorama" not in sys.modules:
            colorama = types.ModuleType("colorama")
            colorama.Fore = types.SimpleNamespace(WHITE="", GREEN="", RED="", YELLOW="", CYAN="", BLUE="", MAGENTA="")
            colorama.Style = types.SimpleNamespace(RESET_ALL="")
            sys.modules["colorama"] = colorama
        if "openai" not in sys.modules:
            openai = types.ModuleType("openai")
            openai.ChatCompletion = types.SimpleNamespace(create=lambda **kwargs: None)
            sys.modules["openai"] = openai
        from services import interaction_service

        captured = {}

        def fake_create(**kwargs):
            captured["messages"] = kwargs["messages"]
            return Mock(choices=[Mock(message=Mock(content="OK"))])

        with patch.object(interaction_service, "DATA_DIR", str(self.tmp)), \
             patch.object(interaction_service.openai.ChatCompletion, "create", side_effect=fake_create):
            interaction_service._chat_completion_create_with_prompt_skills(
                model="chat-model",
                messages=[{"role": "user", "content": "hi"}],
            )

        joined = "\n".join(str(message.get("content", "")) for message in captured["messages"])
        self.assertIn("项目 Prompt Skills", joined)
        self.assertIn("所有回复先给结论。", joined)
        self.assertIn("用苏格拉底式追问。", joined)
        self.assertNotIn("不应出现。", joined)


if __name__ == "__main__":
    unittest.main()
