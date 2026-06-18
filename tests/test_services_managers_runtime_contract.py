import asyncio
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "colorama" not in sys.modules:
    colorama = types.ModuleType("colorama")
    colorama.Fore = types.SimpleNamespace(WHITE="", GREEN="", RED="", YELLOW="", CYAN="", BLUE="", MAGENTA="")
    colorama.Style = types.SimpleNamespace(RESET_ALL="")
    colorama.init = lambda *args, **kwargs: None
    sys.modules["colorama"] = colorama

from services import managers


class RuntimeManagersContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili-managers-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bot_diary_manager_generates_entries_from_events(self):
        diary_file = self.tmp / "bot_diary.json"
        with patch.object(managers, "BOT_DIARY_FILE", str(diary_file)):
            diary = managers.BotDiaryManager()
            entry = asyncio.run(diary.generate_from_events(
                [{"type": "video_processed", "title": "测试视频", "score": 8.0, "actions": ["点赞"]}],
                "默认人格",
                "平静",
            ))

            self.assertEqual(entry["type"], "auto")
            self.assertIn("测试视频", entry["content"])
            self.assertEqual(diary.list_entries(limit=1)[0]["title"], entry["title"])

    def test_self_evolution_manager_reflects_and_marks_applied(self):
        evolution_file = self.tmp / "self_evolution.json"
        with patch.object(managers, "SELF_EVOLUTION_FILE", str(evolution_file)):
            evolution = managers.SelfEvolutionManager()
            item = asyncio.run(evolution.reflect(
                [{"type": "video_processed", "title": "测试视频", "score": 8.0}],
                "默认人格",
                "好奇",
                diary_entries=[],
            ))

            self.assertIn("id", item)
            self.assertIn("parsed", item)
            self.assertFalse(item.get("applied"))
            evolution.mark_applied(item["id"])
            self.assertTrue(evolution.get_items(limit=1)[0]["applied"])

    def test_user_profile_manager_adjusts_affinity_by_delta(self):
        profile_file = self.tmp / "user_profiles.json"
        with patch.object(managers, "USER_PROFILES_FILE", str(profile_file)):
            profiles = managers.UserProfileManager()
            profiles.adjust_affinity("up::1", "测试UP", 3, "点赞视频")

            profile = profiles.get_profile("up::1")
            self.assertEqual(profile["name"], "测试UP")
            self.assertGreater(profile["affinity"], 0)
            self.assertEqual(profile["last_reason"], "点赞视频")

    def test_persona_manager_applies_active_persona_evolution(self):
        persona_file = self.tmp / "personas.json"
        with patch.object(managers, "PERSONAS_FILE", str(persona_file)):
            personas = managers.PersonaManager()
            personas.add_persona("默认人格", {"name": "AI小助手", "style": "热情", "system_prompt": ""})
            personas.set_active_persona("默认人格")

            evolved = personas.evolve_active_persona(
                style_delta="更克制",
                relationship_delta="保持边界",
                new_rule="优先记录真实触发条件",
            )

            self.assertEqual(evolved["style_delta"], "更克制")
            self.assertIn("优先记录真实触发条件", evolved["rules"])
            self.assertEqual(evolved["evolution_history"][-1]["relationship_delta"], "保持边界")


if __name__ == "__main__":
    unittest.main()
