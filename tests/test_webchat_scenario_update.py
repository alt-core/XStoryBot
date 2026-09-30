from pathlib import Path
import json
import os
import stat
import subprocess
import tempfile
import unittest

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / 'update_webchat_scenario.sh'
TEMPLATE_PATH = PROJECT_ROOT / 'template.aws.yaml'
MOCK_AWS_PATH = PROJECT_ROOT / 'tests/fixtures/webchat_update/aws'
BASE_PARAMETERS = ('ImageUri', 'WebchatEnabled', 'WebchatImageUri', 'WebchatSigningKey',
                   'WebchatScenarioUri', 'WebchatCompatibilityEpoch', 'AlarmEmail')


class WebchatScenarioUpdateScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT_PATH.read_text(encoding='utf-8')

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

    def test_更新スクリプトを配布imageへ含めない(self):
        for ignore_name in ('.dockerignore', '.gcloudignore'):
            entries = (PROJECT_ROOT / ignore_name).read_text(
                encoding='utf-8').splitlines()
            self.assertIn('update_webchat_scenario.sh', entries)

    def _run_with_mock(self, answer, unexpected_change=False, parameter_names=BASE_PARAMETERS,
                       template_format='yaml', template_body=None, template_failure=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        call_log = root / 'aws-calls.log'
        call_log.touch()
        script = root / SCRIPT_PATH.name
        script.write_text(self.source)
        script.chmod(0o700)
        template = {
            'AWSTemplateFormatVersion': '2010-09-09',
            'Transform': ['AWS::LanguageExtensions', 'AWS::Serverless-2016-10-31'],
            'Parameters': {name: {'Type': 'String'} for name in parameter_names},
            'Resources': {'WebchatFunction': {'Type': 'AWS::Serverless::Function',
                                            'Metadata': {'SamResourceId': 'WebchatFunction'}}},
        }
        deployed = root / 'deployed.template'
        if template_body is None:
            template_body = json.dumps(template) if template_format == 'json' else yaml.safe_dump(template)
        deployed.write_text(template_body)
        # 手元のtemplateに未適用の変更があっても、Scenario更新へ混ぜない。
        (root / 'template.aws.yaml').write_text('Resources: {LocalOnly: {Type: AWS::SNS::Topic}}\n')
        temporary = root / 'temporary files'
        temporary.mkdir()
        submitted = root / 'submitted.template'
        submitted_path = root / 'submitted-path'
        env = {
            **os.environ,
            'PATH': f'{MOCK_AWS_PATH.parent}:{os.environ["PATH"]}',
            'MOCK_AWS_CALL_LOG': str(call_log),
            'MOCK_DEPLOYED_TEMPLATE': str(deployed),
            'MOCK_PARAMETER_KEYS': '\t'.join(parameter_names),
            'MOCK_SUBMITTED_TEMPLATE': str(submitted),
            'MOCK_SUBMITTED_TEMPLATE_PATH': str(submitted_path),
            'MOCK_TEMPLATE_FAILURE': str(template_failure).lower(),
            'TMPDIR': str(temporary),
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_STACK_NAME': 'xstorybot-test',
            'XSBOT_WEBCHAT_SCENARIO_URI': (
                's3://private/scenario/' + ('a' * 32)),
            'XSBOT_WEBCHAT_CHANGE_SET_NAME': 'scenario-update-test',
            'MOCK_UNEXPECTED_CHANGE': (
                'true' if unexpected_change else 'false'),
        }
        result = subprocess.run(
            [str(script)], cwd=root, env=env,
            input=answer, capture_output=True, check=False, text=True)
        calls = call_log.read_text(encoding='utf-8')
        if submitted.exists():
            self.assertEqual(template_body, submitted.read_text())
            self.assertNotEqual(str(root / 'template.aws.yaml'), submitted_path.read_text())
            self.assertFalse(Path(submitted_path.read_text()).exists())
        self.assertEqual([], list(temporary.iterdir()))
        return result, calls

    def test_確認後にchange_set実行とstack完了待ちまで行う(self):
        result, calls = self._run_with_mock('y\n')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('更新が完了しました', result.stdout)
        self.assertIn('s3api head-object', calls)
        self.assertIn('cloudformation get-template', calls)
        self.assertIn('--template-stage Original', calls)
        self.assertIn('--query to_string(TemplateBody)', calls)
        self.assertNotIn('--use-previous-template', calls)
        self.assertIn('cloudformation create-change-set', calls)
        for name in BASE_PARAMETERS:
            if name == 'WebchatScenarioUri':
                self.assertIn(f'ParameterKey={name},ParameterValue=s3://private/scenario/', calls)
            else:
                self.assertIn(f'ParameterKey={name},UsePreviousValue=true', calls)
        self.assertNotIn('ParameterKey=ActionWorkerEnabled,', calls)
        self.assertNotIn(str(TEMPLATE_PATH), calls)
        self.assertIn('cloudformation wait change-set-create-complete', calls)
        self.assertIn('cloudformation describe-change-set', calls)
        self.assertIn('cloudformation execute-change-set', calls)
        self.assertIn('cloudformation wait stack-update-complete', calls)
        self.assertIn('cloudformation describe-stacks', calls)

    def test_JSONのOriginalと追加parameterも動的に維持する(self):
        extras = ('ActionWorkerEnabled', 'GroupWorkerEnabled', 'AlarmsEnabled', 'CustomDeployedParameter')
        result, calls = self._run_with_mock('n\n', parameter_names=BASE_PARAMETERS + extras, template_format='json')
        self.assertEqual(0, result.returncode, result.stderr)
        for name in extras:
            self.assertIn(f'ParameterKey={name},UsePreviousValue=true', calls)
        self.assertNotIn('cloudformation execute-change-set', calls)

    def test_本文取得失敗と空とサイズ超過ではchange_setを作らない(self):
        for options in ({'template_failure': True}, {'template_body': ''},
                        {'template_body': '#' + 'あ' * 18000}):
            with self.subTest(options=list(options)):
                result, calls = self._run_with_mock('y\n', **options)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('cloudformation create-change-set', calls)
                self.assertNotIn('cloudformation execute-change-set', calls)
                if options.get('template_body'):
                    self.assertIn('51,200', result.stderr)

    def test_対象外stackと不正parameter名は変更しない(self):
        for parameters in (('OtherParameter',), BASE_PARAMETERS + ('Bad/Name',)):
            with self.subTest(parameters=parameters):
                result, calls = self._run_with_mock('y\n', parameter_names=parameters)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('cloudformation create-change-set', calls)

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
