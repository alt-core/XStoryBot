"""通し確認用の台本を、同梱の設定例のまま実際のビルドとLINE検証へ通す。"""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORY = PROJECT_ROOT / 'examples' / 'walkthrough'
CLI = PROJECT_ROOT / 'tools' / 'local_scenario.py'


class WalkthroughStoryTest(unittest.TestCase):
    def test_同梱の設定例と台本で全ケースが期待どおりに応答する(self):
        with tempfile.TemporaryDirectory() as temporary:
            # 設定例の相対pathをそのまま使うため、同じ階層構成で複製する。
            story = Path(temporary).resolve() / 'examples' / 'walkthrough'
            shutil.copytree(STORY, story, ignore=shutil.ignore_patterns('settings.yaml'))
            shutil.copyfile(story / 'settings.yaml.template', story / 'settings.yaml')
            completed = subprocess.run(
                [sys.executable, str(CLI), 'verify', '--settings', 'settings.yaml',
                 '--bot', 'bot', '--suite', 'suite.json'],
                cwd=story, capture_output=True, text=True, timeout=180)
        try:
            result = json.loads(completed.stdout)
        except ValueError:
            self.fail(f'stdoutがJSON一件ではありません:\n{completed.stdout}\n{completed.stderr[-2000:]}')
        # ビルドや起動の失敗は、ケースの比較より先に原因（シート名と行）を示す。
        self.assertNotIn('error', result, f"{result.get('phase')}: {result.get('error')}")
        self.assertIn('cases', result, json.dumps(result, ensure_ascii=False)[:2000])

        failures = []
        for entry in result['cases']:
            case = entry['case']
            for step in case['steps']:
                if not step.get('passed'):
                    failures.append({
                        'case': case['name'], 'step': step['step'], 'input': step['input'],
                        'error': step.get('error'), 'differences': step.get('differences'),
                    })
        expected = json.loads((STORY / 'suite.json').read_text(encoding='utf-8'))['cases']
        self.assertEqual([], failures, json.dumps(failures, ensure_ascii=False, indent=1))
        self.assertEqual([case['name'] for case in expected], [entry['case']['name'] for entry in result['cases']])
        self.assertEqual(0, completed.returncode, completed.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
