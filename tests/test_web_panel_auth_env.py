import os
import shutil
import tempfile
import unittest


TEST_DATA_DIR = tempfile.mkdtemp(prefix="bili-panel-auth-")
os.environ["BILI_ACCOUNT_DATA_DIR"] = TEST_DATA_DIR

import web_panel  # noqa: E402


class WebPanelAuthEnvTest(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def setUp(self):
        web_panel.app.config.update(TESTING=True, SECRET_KEY="test")
        web_panel.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if web_panel.CONFIG_FILE.exists():
            web_panel.CONFIG_FILE.unlink()
        os.environ.pop("BILI_LEARNING_PANEL_PASSWORD", None)

    def tearDown(self):
        os.environ.pop("BILI_LEARNING_PANEL_PASSWORD", None)

    def _agree_disclaimer(self, client):
        with client.session_transaction() as session:
            session["disclaimer_agreed"] = True

    def _write_web_config(self, web_config):
        self.assertTrue(web_panel.write_json(web_panel.CONFIG_FILE, {"web": web_config}))

    def test_env_panel_password_overrides_config_password(self):
        self._write_web_config({"username": "alice", "password": "stored-pass"})
        os.environ["BILI_LEARNING_PANEL_PASSWORD"] = "env-pass"

        with web_panel.app.test_client() as client:
            self._agree_disclaimer(client)
            response = client.post(
                "/api/auth/login",
                json={"username": "alice", "password": "env-pass"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ok"], True)

    def test_env_panel_password_counts_as_configured_with_config_username(self):
        self._write_web_config({"username": "alice", "password": ""})
        os.environ["BILI_LEARNING_PANEL_PASSWORD"] = "env-pass"

        with web_panel.app.test_client() as client:
            self._agree_disclaimer(client)
            response = client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")


if __name__ == "__main__":
    unittest.main()
