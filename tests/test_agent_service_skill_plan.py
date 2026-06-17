import unittest
import sys
import types


if "colorama" not in sys.modules:
    colorama = types.ModuleType("colorama")
    colorama.Fore = types.SimpleNamespace(
        WHITE="",
        GREEN="",
        RED="",
        YELLOW="",
        CYAN="",
        BLUE="",
        MAGENTA="",
    )
    colorama.Style = types.SimpleNamespace(RESET_ALL="")
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

    def test_prompt_skills_are_returned_with_run_metadata(self):
        runner = AgentSkillRunner()
        run = __import__("asyncio").run(runner.run_goal(
            "学习 Python 入门",
            skill="write_memory",
            prompt_skills=[
                {"name": "全局学习风格", "scope": "global", "content": "用苏格拉底式追问。"},
                {"name": "人格补丁", "scope": "persona", "persona": "学习搭子", "content": "口吻更温和。"},
            ],
        ))

        self.assertEqual(run["prompt_skills"][0]["name"], "全局学习风格")
        self.assertEqual(run["prompt_skills"][1]["persona"], "学习搭子")
        self.assertEqual(run["results"][0]["step"]["prompt_skill_count"], 2)


if __name__ == "__main__":
    unittest.main()
