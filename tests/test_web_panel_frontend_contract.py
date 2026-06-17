import re
import unittest
import ast
from pathlib import Path


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
        self.assertIn('id="siteLogoPreview"', html)
        self.assertIn("safeLogoSvg", html)
        self.assertIn("logoDataUri", html)
        self.assertIn("applySiteLogo", html)
        self.assertIn('id="siteLogoMark">{{SITE_LOGO_MARK}}</div>', html)
        self.assertIn("syncVisualConfigFromJson", html)
        self.assertIn("syncJsonFromVisualConfig", html)
        self.assertIn('<link rel="apple-touch-icon" href="{{SITE_ICON_DATA}}">', html)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', html)

    def test_persona_and_agent_management_are_merged(self):
        html = self.html

        self.assertNotIn("人格管理</button>", html)
        self.assertIn("Agent 管理</button>", html)
        self.assertIn('id="agentGoal"', html)
        self.assertIn('id="agentMode"', html)
        self.assertIn('id="agentPersonaSelect"', html)
        self.assertIn('id="agentSettingsGrid"', html)
        self.assertIn("renderAgentSettings", html)

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
