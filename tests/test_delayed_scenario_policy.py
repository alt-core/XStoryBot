"""AWSの遅延指定を、台本の位置を示してビルド時に拒否する。"""

import unittest
from unittest.mock import Mock, patch

import cloud_backend
import commands
import common_commands
import hub
from tests.test_scenario_formatting import _load_module


class DelayedScenarioPolicyTest(unittest.TestCase):
    def setUp(self):
        for patcher in (
            patch.multiple(hub, builder_list=[], runtime_list=[], method_cache={}),
            patch.multiple(commands, catalog=[], catalog_map={}, object_catalog=[], object_catalog_map={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        common_commands.setup({'reset_keyword': '!reset', 'timezone': 'Asia/Tokyo'})
        with patch.object(cloud_backend, 'create_object_store', return_value=Mock()):
            self.scenario = _load_module('tests._delay_policy_scenario', 'scenario.py')

    def _build(self, seconds, allowed=False):
        with patch.object(common_commands.task_client, 'allows_delayed_scenarios', return_value=allowed):
            return self.scenario.ScenarioBuilder.build_from_tables(
                [('物語', [['開始', '本文'], ['続き', '@delay', seconds, '#next']])], version=3)

    def test_遅延と負数は行位置付きで拒否する(self):
        for seconds in ('5', '0.1', '-1', '-0.1'):
            with self.subTest(seconds=seconds):
                with self.assertRaises(self.scenario.ScenarioSyntaxError) as error:
                    self._build(seconds)
                self.assertIn('@delay 0', str(error.exception))
                self.assertIn('＠物語!2行目', str(error.exception))

    def test_数値ゼロの表記を実行可能な0へ揃える(self):
        for seconds in ('0', '0.0', '-0.0', '０'):
            with self.subTest(seconds=seconds):
                built = self._build(seconds)
                lines = [line for scene in built.scenes.values() for region in scene.regions
                         for _condition, block in region.blocks for line in block]
                delay = next(line for line in lines if line.msg == '@delay')
                self.assertEqual('0', delay.options[0])

    def test_遅延を許す環境の秒数は変更しない(self):
        self._build('5', allowed=True)


if __name__ == '__main__':
    unittest.main()
