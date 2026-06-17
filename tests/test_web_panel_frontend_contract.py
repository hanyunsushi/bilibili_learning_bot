import re
import unittest
import ast
import tempfile
import shutil
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch


def _read_default_html():
    module = ast.parse(Path("web_panel.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_DEFAULT_HTML":
                    return ast.literal_eval(node.value)
    raise AssertionError("_DEFAULT_HTML not found")


class WebPanelFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _read_default_html()

    def test_sidebar_uses_black_svg_icons_and_hidden_scrollbar(self):
        html = self.html

        self.assertIn(".sb-nav{", html)
        self.assertIn("scrollbar-width:none", html)
        self.assertIn(".sb-nav::-webkit-scrollbar{display:none}", html)
        self.assertIn(".nav-ico svg{width:16px;height:16px;stroke:currentColor", html)
        self.assertIn("function navIcon(name)", html)
        self.assertIn('data-icon="dash"', html)

        nav_markup = re.search(r"<nav class=\"sb-nav\">([\s\S]*?)</nav>", html)
        self.assertIsNotNone(nav_markup)
        self.assertNotRegex(nav_markup.group(1), r"<span class=\"ic\">[^<]+</span>")

    def test_sidebar_active_and_hover_are_black_without_accent_edge(self):
        html = self.html

        self.assertIn(".ni:hover{background-color:var(--fg);color:var(--surface)", html)
        self.assertIn(".ni.ac{background-color:var(--fg);color:var(--surface)", html)
        self.assertIn(".ni:hover .nav-ico,.ni.ac .nav-ico{background:rgba(255,255,255,.12);color:var(--surface);border-color:rgba(255,255,255,.18)}", html)
        self.assertNotIn("box-shadow:inset 3px 0 0 var(--accent)", html)

    def test_config_page_has_visual_editor_and_logo_controls(self):
        html = self.html

        self.assertIn('id="confVisual"', html)
        self.assertIn('id="siteLogoText"', html)
        self.assertIn('id="siteLogoSvg"', html)
        self.assertIn('id="siteLogoFile"', html)
        self.assertIn('id="siteLogoPreview"', html)
        self.assertIn("loadLogoSvgFile", html)
        self.assertIn("safeLogoSvg", html)
        self.assertIn("logoDataUri", html)
        self.assertIn("applySiteLogo", html)
        self.assertIn('id="siteLogoMark">{{SITE_LOGO_MARK}}</div>', html)
        self.assertIn("syncVisualConfigFromJson", html)
        self.assertIn("syncJsonFromVisualConfig", html)
        self.assertIn('<link rel="apple-touch-icon" href="{{SITE_ICON_DATA}}">', html)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', html)

    def test_config_auto_refresh_respects_dirty_editing_state(self):
        html = self.html

        self.assertIn("var _configDirty=false", html)
        self.assertIn("markConfigDirty", html)
        self.assertIn("if(_configDirty)return", html)
        self.assertIn("配置有未保存修改，已暂停自动刷新", html)
        self.assertRegex(html, r"function rf_conf\(\)\{[\s\S]*?loadConf\(true\)")

    def test_standalone_pages_share_site_logo_and_ios_icons(self):
        source = Path("web_panel.py").read_text(encoding="utf-8")

        self.assertIn("def _site_head_tags", source)
        self.assertIn("def _apply_site_chrome", source)
        self.assertIn('<link rel="apple-touch-icon" href="{site["icon_data"]}">', source)
        self.assertIn('<link rel="icon" href="{site["icon_data"]}">', source)
        self.assertGreaterEqual(source.count("{{SITE_HEAD_TAGS}}"), 3)
        self.assertEqual(source.count('<div class="auth-logo">{{SITE_LOGO_MARK}}</div>'), 3)
        self.assertIn("return _apply_site_chrome(r\"\"\"<!DOCTYPE html>", source)

    def test_persona_and_agent_management_are_merged(self):
        html = self.html

        self.assertNotIn("人格管理</button>", html)
        self.assertIn("Agent 管理</button>", html)
        self.assertIn('id="agentGoal"', html)
        self.assertIn('id="agentMode"', html)
        self.assertIn('id="agentSkillSelect"', html)
        self.assertIn("agentSkillCatalog", html)
        self.assertIn("renderAgentSkillHelp", html)
        self.assertIn('id="agentPersonaSelect"', html)
        self.assertIn('id="agentSettingsGrid"', html)
        self.assertIn("renderAgentSettings", html)

    def test_agent_page_has_prompt_skill_upload_controls(self):
        html = self.html

        self.assertIn('id="promptSkillScope"', html)
        self.assertIn('id="promptSkillPersona"', html)
        self.assertIn('id="promptSkillName"', html)
        self.assertIn('id="promptSkillFile"', html)
        self.assertIn('id="promptSkillContent"', html)
        self.assertIn('id="promptSkillList"', html)
        self.assertIn("loadPromptSkillFile", html)
        self.assertIn("savePromptSkill", html)
        self.assertIn("rf_prompt_skills", html)
        self.assertIn("deletePromptSkill", html)
        self.assertIn("/api/prompt-skills", html)

    def test_agent_skill_endpoint_persists_selected_skill(self):
        source = Path("web_panel.py").read_text(encoding="utf-8")

        self.assertIn("skill = (body.get('skill') or 'full_plan').strip()", source)
        self.assertIn("skill=skill", source)
        self.assertIn("Agent技能已排队: {skill}", source)
        self.assertIn("prompt_skills=prompt_skills", source)

    def test_agent_manual_mode_starts_project_skill_runner(self):
        import web_panel

        web_panel.app.config.update(TESTING=True)
        data_dir = Path(tempfile.mkdtemp(prefix="bili-web-agent-test-"))
        config_file = data_dir / "config.json"
        try:
            with (
                patch.object(web_panel, "_start_agent_skill_thread") as start_thread,
                patch.object(web_panel, "_select_prompt_skills", return_value=[{"name": "全局学习风格", "scope": "global", "content": "慢一点讲。"}]),
                patch.object(web_panel, "DATA_DIR", data_dir),
                patch.object(web_panel, "CONFIG_FILE", config_file),
            ):
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    response = client.post(
                        "/api/action/agent-skill",
                        json={
                            "goal": "学习 Python 入门",
                            "skill": "search_bilibili_videos",
                            "mode": "manual",
                            "persona": "默认人格",
                        },
                    )
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ok"], True)
        start_thread.assert_called_once_with(
            "学习 Python 入门",
            "search_bilibili_videos",
            "默认人格",
            [{"name": "全局学习风格", "scope": "global", "content": "慢一点讲。"}],
        )

    def test_agent_runner_does_not_enter_terminal_login_without_cookie(self):
        import web_panel

        fake_agent = types.SimpleNamespace(AgentBrain=Mock(side_effect=AssertionError("should not start AgentBrain")))
        missing_cookie = Path(tempfile.mkdtemp(prefix="bili-web-agent-no-cookie-")) / "missing.json"
        try:
            with patch.object(web_panel, "COOKIE_FILE", missing_cookie), \
                    patch.object(web_panel, "log_line") as log_line, \
                    patch.dict(sys.modules, {"new_agent": fake_agent}):
                web_panel._run_agent_skill("学习 Python 入门", "search_bilibili_videos", "默认人格")

            fake_agent.AgentBrain.assert_not_called()
            self.assertTrue(any("请先在 B站登录页完成登录" in str(call.args[0]) for call in log_line.call_args_list))
        finally:
            shutil.rmtree(missing_cookie.parent, ignore_errors=True)

    def test_prompt_skill_api_supports_global_and_persona_uploads(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-prompt-skill-test-"))
        config_file = data_dir / "config.json"
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), patch.object(web_panel, "CONFIG_FILE", config_file):
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    global_response = client.post("/api/prompt-skills", json={
                        "name": "全局学习风格",
                        "scope": "global",
                        "content": "# 全局学习风格\n用苏格拉底式追问。",
                    })
                    persona_response = client.post("/api/prompt-skills", json={
                        "name": "学习搭子补丁",
                        "scope": "persona",
                        "persona": "学习搭子",
                        "content": "# 学习搭子补丁\n口吻更温和。",
                    })
                    list_response = client.get("/api/prompt-skills?persona=学习搭子")

            self.assertEqual(global_response.status_code, 200)
            self.assertEqual(persona_response.status_code, 200)
            payload = list_response.get_json()
            self.assertEqual([item["name"] for item in payload["items"]], ["全局学习风格", "学习搭子补丁"])
            self.assertEqual(payload["items"][0]["scope"], "global")
            self.assertEqual(payload["items"][1]["persona"], "学习搭子")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_main_ui_does_not_use_emoji_as_icons(self):
        html = self.html

        icon_emoji = "📊🎮🔑⚙️🎭💡⚡👥💬👤🧠📖📋🎓🔧💾ℹ️🤖✅❌📷🚪🔍🔄⏳⭐➕🚀🎙️📚📂📦⏱📝📄📱📡🛡️📹📤📥👁🔥▶⏹☑☐✍️🎨⚠️⚠"
        self.assertFalse(set(icon_emoji).intersection(html))

    def test_custom_logo_svg_is_sanitized_server_side(self):
        source = Path("web_panel.py").read_text(encoding="utf-8")

        self.assertIn("def _sanitize_logo_svg", source)
        self.assertIn("site['logo_svg'] = _sanitize_logo_svg", source)
        self.assertIn(r"<\s*script\b", source)
        self.assertIn(r"\son[a-z]+\s*=", source)


if __name__ == "__main__":
    unittest.main()
