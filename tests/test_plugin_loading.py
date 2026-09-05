# coding: utf-8
"""plugin.load_plugins の依存欠落時の振る舞い。"""

import unittest
from unittest.mock import patch

import plugin


class PluginLoadingTest(unittest.TestCase):
    def test_任意pluginの依存packageが無ければ何を入れるか示して停止する(self):
        error = ModuleNotFoundError("No module named 'twilio'", name='twilio')
        with patch.object(plugin.importlib, 'import_module', side_effect=error):
            with self.assertRaises(RuntimeError) as captured:
                plugin.load_plugins({}, {'twilio': {}})
        message = str(captured.exception)
        self.assertIn('plugin.twilio', message)
        self.assertIn('"twilio"', message)
        self.assertIn('requirements-optional.txt', message)

    def test_plugin自体が無い場合は元の例外のまま(self):
        error = ModuleNotFoundError("No module named 'plugin.nothing'", name='plugin.nothing')
        with patch.object(plugin.importlib, 'import_module', side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                plugin.load_plugins({}, {'nothing': {}})


if __name__ == '__main__':
    unittest.main()
