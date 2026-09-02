from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest

import yaml

from tests.test_aws_template import CloudFormationLoader


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / 'update_webchat_scenario.sh'
TEMPLATE_PATH = PROJECT_ROOT / 'template.aws.yaml'
MOCK_AWS_PATH = PROJECT_ROOT / 'tests/fixtures/webchat_update/aws'


class WebchatScenarioUpdateScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT_PATH.read_text(encoding='utf-8')
        cls.template = yaml.load(
            TEMPLATE_PATH.read_text(encoding='utf-8'),
            Loader=CloudFormationLoader)

    def test_shell構文と更新境界(self):
        self.assertTrue(SCRIPT_PATH.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run(
            ['/bin/sh', '-n', str(SCRIPT_PATH)],
            capture_output=True, check=False, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for forbidden in (
                'aws cloudformation update-stack',
                'sam deploy', 'docker ', 'aws ecr'):
            self.assertNotIn(forbidden, self.source)
        self.assertIn('aws cloudformation execute-change-set', self.source)
        self.assertIn('aws cloudformation wait stack-update-complete',
                      self.source)
        self.assertIn('この変更を実行しますか', self.source)

    def test_templateの全parameterを明示的に維持または更新する(self):
        for name in self.template['Parameters']:
            self.assertIn(f'ParameterKey={name},', self.source)
        self.assertIn(
            'ParameterKey=WebchatScenarioUri,ParameterValue=', self.source)
        self.assertIn(
            'ParameterKey=WebchatCompatibilityEpoch,UsePreviousValue=true',
            self.source)
        for ignore_name in ('.dockerignore', '.gcloudignore'):
            entries = (PROJECT_ROOT / ignore_name).read_text(
                encoding='utf-8').splitlines()
            self.assertIn('update_webchat_scenario.sh', entries)

    def _run_with_mock(self, answer, unexpected_change=False):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        call_log = root / 'aws-calls.log'
        env = {
            **os.environ,
            'PATH': f'{MOCK_AWS_PATH.parent}:{os.environ["PATH"]}',
            'MOCK_AWS_CALL_LOG': str(call_log),
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_STACK_NAME': 'xstorybot-test',
            'XSBOT_WEBCHAT_SCENARIO_URI': (
                's3://private/scenario/' + ('a' * 32)),
            'XSBOT_WEBCHAT_CHANGE_SET_NAME': 'scenario-update-test',
            'MOCK_UNEXPECTED_CHANGE': (
                'true' if unexpected_change else 'false'),
        }
        result = subprocess.run(
            [str(SCRIPT_PATH)], cwd=PROJECT_ROOT, env=env,
            input=answer, capture_output=True, check=False, text=True)
        calls = call_log.read_text(encoding='utf-8')
        directory.cleanup()
        return result, calls

    def test_確認後にchange_set実行とstack完了待ちまで行う(self):
        result, calls = self._run_with_mock('y\n')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('更新が完了しました', result.stdout)
        self.assertIn('s3api head-object', calls)
        self.assertNotIn('cloudformation get-template', calls)
        self.assertIn('cloudformation create-change-set', calls)
        self.assertIn(str(TEMPLATE_PATH), calls)
        self.assertIn('cloudformation wait change-set-create-complete', calls)
        self.assertIn('cloudformation describe-change-set', calls)
        self.assertIn('cloudformation execute-change-set', calls)
        self.assertIn('cloudformation wait stack-update-complete', calls)
        self.assertIn('cloudformation describe-stacks', calls)

    def test_確認を拒否した場合は更新しない(self):
        result, calls = self._run_with_mock('n\n')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('更新を中止しました', result.stdout)
        self.assertNotIn('cloudformation execute-change-set', calls)
        self.assertNotIn('cloudformation wait stack-update-complete', calls)

    def test_Webchat以外の変更を削除して実行しない(self):
        result, calls = self._run_with_mock(
            'y\n', unexpected_change=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('Scenario以外の変更', result.stderr)
        self.assertIn('cloudformation delete-change-set', calls)
        self.assertNotIn('cloudformation execute-change-set', calls)

    def test_不正なScenario_URIをAWS呼出し前に拒否する(self):
        env = {
            **os.environ,
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_STACK_NAME': 'xstorybot-test',
            'XSBOT_WEBCHAT_SCENARIO_URI': 's3://private/not-scenario',
        }
        result = subprocess.run(
            [str(SCRIPT_PATH)], cwd=PROJECT_ROOT, env=env,
            capture_output=True, check=False, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('形式が不正です', result.stderr)


if __name__ == '__main__':
    unittest.main()
