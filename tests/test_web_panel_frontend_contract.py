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

    def test_sidebar_hover_is_sand_and_active_is_black_without_accent_edge(self):
        html = self.html

        self.assertIn(".sb-nav{flex:1;overflow-y:auto;padding:10px 10px 12px;display:grid;gap:4px", html)
        self.assertIn(".ni:hover:not(.ac){background-color:var(--sand);color:var(--fg)", html)
        self.assertIn(".ni.ac{background-color:var(--fg);color:var(--surface)", html)
        self.assertIn(".ni:hover:not(.ac) .nav-ico{background:var(--surface);color:var(--fg);border-color:var(--ring-color)}", html)
        self.assertIn(".ni.ac .nav-ico{background:rgba(255,255,255,.12);color:var(--surface);border-color:rgba(255,255,255,.18)}", html)
        self.assertNotIn("box-shadow:inset 3px 0 0 var(--accent)", html)

    def test_dashboard_stat_cards_are_compact_and_live_updated(self):
        html = self.html

        self.assertIn(".sr{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:12px", html)
        self.assertIn(".sc{background:rgba(250,249,245,.78);border:1px solid var(--line);border-radius:14px;padding:14px", html)
        self.assertIn(".sv{font-size:17px;font-weight:650", html)
        self.assertIn('class="sv" id="dashUptime"', html)
        self.assertIn('class="sv" id="dashCost"', html)
        self.assertIn("updateLiveUptime", html)
        self.assertIn("setInterval(updateLiveUptime,1000)", html)

    def test_config_page_has_visual_editor_and_logo_controls(self):
        html = self.html

        self.assertIn('id="confVisual"', html)
        self.assertIn('id="siteLogoText"', html)
        self.assertIn('id="siteLogoImage"', html)
        self.assertIn('id="siteLogoFile"', html)
        self.assertIn('id="siteLogoPreview"', html)
        self.assertIn("loadLogoImageFile", html)
        self.assertIn("safeLogoImage", html)
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

    def test_auto_refresh_skips_form_editing_pages(self):
        html = self.html

        self.assertIn("var autoRefreshSkipPages={conf:1,psna:1,mood:1,behavior:1,tools:1,tutor:1,sys:1}", html)
        self.assertIn("if(!autoRefreshSkipPages[id]&&window['rf_'+id])window['rf_'+id]()", html)

    def test_api_helper_reports_json_errors_instead_of_parsing_html(self):
        html = self.html

        self.assertIn("var ct=r.headers.get('content-type')||''", html)
        self.assertIn("登录状态已失效，请重新登录或确认免责声明", html)
        self.assertIn("throw new Error(data.message||('请求失败: '+r.status))", html)

    def test_model_config_explains_single_provider_multiple_model_roles(self):
        html = self.html

        self.assertIn("一个 API Key / Base URL 可以同时服务多个用途", html)
        self.assertIn('data-cfg-path="models.chat"', html)
        self.assertIn('data-cfg-path="models.vision"', html)
        self.assertIn('data-cfg-path="models.image"', html)
        self.assertIn('data-cfg-path="models.embedding"', html)
        self.assertIn('data-cfg-path="fallback_models.image"', html)
        self.assertIn('data-cfg-path="fallback_models.embedding"', html)

    def test_config_page_exposes_per_role_provider_settings(self):
        html = self.html

        self.assertIn("按用途拆分供应商", html)
        for role in ("chat", "vision", "image", "embedding", "fast"):
            self.assertIn(f'data-cfg-path="providers.{role}.api_key"', html)
            self.assertIn(f'data-cfg-path="providers.{role}.base_url"', html)
            self.assertIn(f'data-cfg-path="providers.{role}.model"', html)

    def test_focus_ring_uses_terracotta_not_blue(self):
        html = self.html

        self.assertIn("--focus:#c96442", html)
        self.assertIn("rgba(201,100,66,.18)", html)
        self.assertNotIn("#3898ec", html)
        self.assertNotIn("rgba(56,152,236", html)

    def test_system_buttons_have_spacing_from_descriptive_text(self):
        html = self.html

        self.assertIn(".sys-actions{margin-top:14px", html)
        self.assertIn('<div class="sys-actions"><button class="btn btn-pr" onclick="exportConfig()">导出全部配置</button></div>', html)
        self.assertIn('<div class="sys-actions"><button class="btn btn-out" onclick="listBackups()">刷新备份列表</button></div>', html)
        self.assertIn('var r=await api("GET","/api/import")', html)

    def test_backup_list_api_accepts_frontend_get_request(self):
        source = Path("web_panel.py").read_text(encoding="utf-8")

        self.assertIn("@app.route('/api/import', methods=['GET', 'POST'])", source)

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

    def test_start_bot_process_enters_main_run_menu_choice(self):
        import web_panel

        class FakePipe:
            def readline(self):
                return ""

            def close(self):
                pass

        fake_stdin = Mock()
        fake_stdin.closed = False
        fake_process = types.SimpleNamespace(stdout=FakePipe(), stdin=fake_stdin)
        old_process, old_running, old_start = web_panel.bot_process, web_panel.bot_running, web_panel.bot_start_time
        web_panel.bot_process = None
        web_panel.bot_running = False
        web_panel.bot_start_time = None
        try:
            with patch.object(web_panel.subprocess, "Popen", return_value=fake_process):
                ok, msg = web_panel.start_bot_process()
        finally:
            web_panel.bot_process = old_process
            web_panel.bot_running = old_running
            web_panel.bot_start_time = old_start

        self.assertTrue(ok)
        self.assertEqual(msg, "机器人已启动")
        fake_stdin.write.assert_any_call("1\n")
        fake_stdin.flush.assert_called()

    def test_new_agent_respects_terminal_disclaimer_skip_env(self):
        source = Path("new_agent.py").read_text(encoding="utf-8")

        self.assertIn("BILI_DISCLAIMER_SKIP", source)
        self.assertRegex(source, r"if not os\.getenv\('BILI_DISCLAIMER_SKIP'\):\s+_disclaimer_confirm\(\)")

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

    def test_custom_logo_image_is_sanitized_server_side(self):
        source = Path("web_panel.py").read_text(encoding="utf-8")

        self.assertIn("def _sanitize_logo_image", source)
        self.assertIn("site['logo_image'] = _sanitize_logo_image", source)
        self.assertIn("image/png", source)
        self.assertIn("image/jpeg", source)
        self.assertIn("image/webp", source)

    def test_config_api_returns_json_when_authentication_is_missing(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-config-auth-test-"))
        config_file = data_dir / "config.json"
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), patch.object(web_panel, "CONFIG_FILE", config_file):
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                with web_panel.app.test_client() as client:
                    response = client.post("/api/config", json={"site": {"logo_text": "BL"}})

            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.content_type.split(";")[0], "application/json")
            self.assertEqual(response.get_json()["ok"], False)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_config_api_preserves_regular_image_logo_data_url(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-logo-image-test-"))
        config_file = data_dir / "config.json"
        logo = "data:image/png;base64,iVBORw0KGgo="
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), patch.object(web_panel, "CONFIG_FILE", config_file):
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    response = client.post("/api/config", json={
                        "web": {"username": "alice", "password": "secret"},
                        "site": {"brand_name": "Bili", "logo_text": "BL", "logo_image": logo},
                    })
                    saved = web_panel.read_json(config_file, {})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["ok"], True)
            self.assertEqual(saved["site"]["logo_image"], logo)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_monitor_apis_normalize_runtime_data_shapes(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-monitor-shape-test-"))
        config_file = data_dir / "config.json"
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), patch.object(web_panel, "CONFIG_FILE", config_file):
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                web_panel.write_json(data_dir / "comment_log.json", {
                    "history": [
                        {"timestamp": "2026-06-18T07:01:02", "action": "reply", "content": "测试评论", "target_user": "小明", "comment_id": "c1"}
                    ]
                })
                web_panel.write_json(data_dir / "user_profiles.json", {
                    "up::1": {"name": "测试UP", "affinity": 12, "impression": "讲解清楚", "last_seen": "2026-06-18T07:02:03"}
                })
                web_panel.write_json(data_dir / "bot_diary.json", {
                    "diaries": [{"time": "2026-06-18T07:03:04", "title": "日记", "content": "今天学了很多", "energy": ""}]
                })
                web_panel.write_json(data_dir / "self_evolution.json", {
                    "items": [{"time": "2026-06-18T07:04:05", "category": "style", "suggestion": "更克制"}]
                })
                web_panel.write_json(data_dir / "agent_skill_log.json", [
                    {"created_at": "2026-06-18T07:05:06", "skill": "full_plan", "goal": "学习测试", "ok": True}
                ])
                web_panel.write_json(data_dir / "web_costs.json", {
                    "total": 0,
                    "calls": [{"model": "gpt-4.1-mini", "price": 0.0012, "purpose": "chat", "created_at": "2026-06-18T07:06:07"}]
                })

                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    comments = client.get("/api/comments?limit=5").get_json()
                    users = client.get("/api/users").get_json()
                    diary = client.get("/api/diary").get_json()
                    actions = client.get("/api/actions?limit=5").get_json()
                    charts = client.get("/api/charts").get_json()
                    info = client.get("/api/info").get_json()

            self.assertEqual(comments["items"][0]["content"], "测试评论")
            self.assertIn("up::1", users["users"])
            self.assertEqual(diary["diary"]["entries"][0]["content"], "今天学了很多")
            self.assertEqual(diary["evolution"]["events"][0]["detail"], "更克制")
            self.assertEqual(actions["items"][0]["action"], "full_plan")
            self.assertEqual(len(charts["comments"]), 1)
            self.assertEqual(len(charts["moods"]), 1)
            self.assertEqual(len(charts["actions"]), 1)
            self.assertAlmostEqual(info["cost_total"], 0.0012)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_config_api_merges_partial_updates_and_sanitizes_logo(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-config-merge-test-"))
        config_file = data_dir / "config.json"
        logo = "data:image/png;base64,iVBORw0KGgo="
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), patch.object(web_panel, "CONFIG_FILE", config_file):
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {
                    "web": {"username": "alice", "password": "secret"},
                    "api": {"model_brain": "keep-me"},
                    "behavior": {"comment_mode": "real"},
                })
                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    response = client.post("/api/config", json={"site": {"logo_image": logo}})
                    saved = web_panel.read_json(config_file, {})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["ok"], True)
            self.assertEqual(saved["site"]["logo_image"], logo)
            self.assertEqual(saved["api"]["model_brain"], "keep-me")
            self.assertEqual(saved["behavior"]["comment_mode"], "real")
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    def test_info_api_detects_external_new_agent_process(self):
        import web_panel

        data_dir = Path(tempfile.mkdtemp(prefix="bili-runtime-status-test-"))
        config_file = data_dir / "config.json"
        old_process, old_running, old_start = web_panel.bot_process, web_panel.bot_running, web_panel.bot_start_time
        try:
            with patch.object(web_panel, "DATA_DIR", data_dir), \
                    patch.object(web_panel, "CONFIG_FILE", config_file), \
                    patch.object(web_panel, "_find_new_agent_process", return_value=True):
                web_panel.bot_process = None
                web_panel.bot_running = False
                web_panel.bot_start_time = None
                web_panel.app.config.update(TESTING=True)
                web_panel.write_json(config_file, {"web": {"username": "alice", "password": "secret"}})
                web_panel.write_json(data_dir / "bot_runtime_state.json", {
                    "current_start_at": "2026-06-18T07:00:00",
                    "current_heartbeat_at": "2026-06-18T07:01:00",
                })
                with web_panel.app.test_client() as client:
                    with client.session_transaction() as session:
                        session["disclaimer_agreed"] = True
                        session["panel_authenticated"] = True
                    response = client.get("/api/info")

            payload = response.get_json()
            self.assertTrue(payload["bot_running"])
            self.assertEqual(payload["bot_start_time"], "2026-06-18 07:00:00")
            self.assertIn("bot_uptime_seconds", payload)
        finally:
            web_panel.bot_process = old_process
            web_panel.bot_running = old_running
            web_panel.bot_start_time = old_start
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
