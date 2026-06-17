import importlib.util
import unittest


class BilibiliLoginV2DependencyTest(unittest.TestCase):
    def test_bilibili_api_login_v2_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec("bilibili_api.login_v2"))
        from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

        self.assertEqual(QrCodeLogin.__name__, "QrCodeLogin")
        self.assertEqual(QrCodeLoginEvents.__name__, "QrCodeLoginEvents")


if __name__ == "__main__":
    unittest.main()
