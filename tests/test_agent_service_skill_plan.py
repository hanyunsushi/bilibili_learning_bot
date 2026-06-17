import unittest
import sys
import types


if "colorama" not in sys.modules:
    colorama = types.ModuleType("colorama")
    colorama.Fore = types.SimpleNamespace()
    colorama.Style = types.SimpleNamespace()
    sys.modules["colorama"] = colorama

from services.agent_service import AgentSkillRunner


class AgentServiceSkillPlanTest(unittest.TestCase):
    def _actions_for(self, skill):
        runner = AgentSkillRunner()
        return [step["action"] for step in runner._make_plan("学习 Python 入门", skill=skill)]

    def test_full_plan_runs_search_watch_and_summarize(self):
        self.assertEqual(self._actions_for("full_plan"), ["search", "watch", "summarize"])

    def test_search_skill_only_searches(self):
        self.assertEqual(self._actions_for("search_bilibili_videos"), ["search"])

    def test_watch_skill_searches_then_watches(self):
        self.assertEqual(self._actions_for("watch_bilibili_videos"), ["search", "watch"])

    def test_memory_skill_only_summarizes(self):
        self.assertEqual(self._actions_for("write_memory"), ["summarize"])

    def test_unknown_skill_falls_back_to_full_plan(self):
        self.assertEqual(self._actions_for("unknown"), ["search", "watch", "summarize"])


if __name__ == "__main__":
    unittest.main()
