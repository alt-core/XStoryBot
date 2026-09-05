from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / 'deploy_aws.sh'


class AwsDeployScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT_PATH.read_text(encoding='utf-8')

    def test_shell構文が正しい(self):
        self.assertTrue(SCRIPT_PATH.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run(
            ['/bin/sh', '-n', str(SCRIPT_PATH)],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual('', result.stderr)
        self.assertEqual(0, result.returncode)

    def test_既存ECRへ同一imageを1回だけbuild_pushする(self):
        self.assertIn('aws ecr describe-repositories', self.source)
        self.assertNotIn('aws ecr create-repository', self.source)
        self.assertEqual(1, self.source.count('docker buildx build'))
        self.assertIn('--platform linux/amd64', self.source)
        self.assertIn('--provenance=false', self.source)
        self.assertIn('--push', self.source)
        self.assertNotIn('\nsam build ', self.source)

    def test_SAMへ必須Parameter名だけを渡す(self):
        for name in (
                'ImageUri',
                'EnvironmentName',
                'SheetId',
                'GoogleSheetsCredentialParameterName',
                'AdminAuthParameterName',
                'RuntimeSecretsParameterName',
                'WebchatEnabled',
                'WebchatImageUri',
                'WebchatSigningKey',
                'WebchatScenarioUri',
                'WebchatCompatibilityEpoch'):
            self.assertIn(f'ParameterKey={name},ParameterValue=', self.source)

        self.assertIn('sam validate', self.source)
        self.assertIn('--lint', self.source)
        self.assertIn('sam deploy', self.source)
        self.assertIn('--image-repository "$repository_uri"', self.source)
        self.assertLess(
            self.source.index('sam validate'),
            self.source.index('aws ecr describe-repositories'),
        )
        self.assertLess(
            self.source.index('sam validate'),
            self.source.index('docker buildx build'),
        )
        self.assertLess(
            self.source.index('docker buildx build'),
            self.source.index('sam deploy'),
        )
        self.assertIn('--capabilities CAPABILITY_IAM', self.source)
        self.assertIn('aws cloudformation describe-stacks', self.source)
        for secret_name in (
                'LINE_ACCESS_TOKEN',
                'LINE_CHANNEL_SECRET',
                'TWILIO_AUTH_TOKEN',
                'PUSHER_APP_SECRET'):
            self.assertNotIn(secret_name, self.source)

        self.assertIn(
            '${XSBOT_WEBCHAT_SIGNING_KEY:?', self.source)
        self.assertNotIn(
            'echo "$XSBOT_WEBCHAT_SIGNING_KEY"', self.source)

    def test_デプロイファイルをimageとGCP_uploadから除外する(self):
        for ignore_name in ('.dockerignore', '.gcloudignore'):
            entries = {
                line.strip()
                for line in (PROJECT_ROOT / ignore_name).read_text(
                    encoding='utf-8').splitlines()
                if line.strip() and not line.lstrip().startswith('#')
            }
            self.assertIn('deploy_aws.sh', entries)
            self.assertIn('template.aws.yaml', entries)

    def _run_deploy(self, extra_env=None, settings_content='default: {}\n'):
        env = {
            'PATH': '/usr/bin:/bin',
            'AWS_REGION': 'ap-northeast-1',
            'XSBOT_AWS_STACK_NAME': 'test-stack',
            'XSBOT_AWS_ECR_REPOSITORY': 'test-repository',
            'XSBOT_AWS_ENVIRONMENT': 'test',
            'XSBOT_AWS_SHEET_ID': 'test-sheet',
            'XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER': '/test/sheets',
            'XSBOT_AWS_ADMIN_AUTH_PARAMETER': '/test/admin',
            'XSBOT_AWS_RUNTIME_SECRETS_PARAMETER': '/test/runtime',
            'XSBOT_AWS_IMAGE_TAG': 'test-image',
            **(extra_env or {}),
        }
        # 実CLIを呼ばず、実scriptが送る引数と呼出し順を記録する。
        shell = r'''
aws() {
    printf 'call:aws:%s:%s\n' "$1" "$2" >&2
    case "$1 $2" in
        'ecr describe-repositories')
            printf '%s\n' 'example.invalid/test-repository' ;;
        'ecr get-login-password')
            printf '%s\n' 'artificial-password' ;;
    esac
}
docker() {
    printf 'call:docker:%s\n' "$1" >&2
    if [ "$1" = login ]; then IFS= read -r mock_password; fi
    return 0
}
sam() {
    printf 'call:sam:%s\n' "$1" >&2
    if [ "$1" = deploy ]; then
        printf '%s\0' "$@"
    fi
    return 0
}
. ./deploy_aws.sh
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'deploy_aws.sh'
            script.write_text(self.source, encoding='utf-8')
            (root / 'template.aws.yaml').write_text(
                'Resources: {}\n', encoding='utf-8')
            if settings_content is not None:
                (root / 'settings.yaml').write_text(
                    settings_content, encoding='utf-8')
            return subprocess.run(
                ['/bin/sh', '-c', shell, str(script)],
                cwd=root, env=env, capture_output=True,
                check=False, text=True,
            )

    def _parameters(self, result):
        self.assertEqual(0, result.returncode, result.stderr)
        parameters = {}
        for argument in result.stdout.split('\0'):
            if argument.startswith('ParameterKey='):
                key, value = argument[len('ParameterKey='):].split(
                    ',ParameterValue=', 1)
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                parameters[key] = value
        return parameters

    def test_未指定optionalは送らずWebchatも同じimageへ更新する(self):
        result = self._run_deploy()
        parameters = self._parameters(result)
        self.assertEqual({
            'ImageUri', 'WebchatImageUri', 'EnvironmentName', 'SheetId',
            'GoogleSheetsCredentialParameterName', 'AdminAuthParameterName',
            'RuntimeSecretsParameterName',
        }, set(parameters))
        self.assertEqual(
            'example.invalid/test-repository:test-image',
            parameters['ImageUri'])
        self.assertEqual(parameters['ImageUri'], parameters['WebchatImageUri'])
        self.assertEqual(1, result.stderr.count('call:docker:buildx'))
        self.assertIn('--confirm-changeset', result.stdout.split('\0'))
        self.assertCountEqual([
            'call:sam:validate',
            'call:aws:ecr:describe-repositories',
            'call:aws:ecr:get-login-password',
            'call:docker:login',
            'call:docker:buildx',
            'call:sam:deploy',
            'call:aws:cloudformation:describe-stacks',
        ], result.stderr.splitlines())
        self.assertLess(
            result.stderr.index('call:docker:buildx'),
            result.stderr.index('call:sam:deploy'))

    def test_false明示は秘密値を要求せず無効化だけを送る(self):
        parameters = self._parameters(self._run_deploy({
            'XSBOT_WEBCHAT_ENABLED': 'false',
        }))
        self.assertEqual('false', parameters['WebchatEnabled'])
        self.assertNotIn('WebchatSigningKey', parameters)
        self.assertNotIn('WebchatScenarioUri', parameters)
        self.assertEqual(parameters['ImageUri'], parameters['WebchatImageUri'])

    def test_true明示で鍵とScenarioを渡しepochは未指定なら送らない(self):
        parameters = self._parameters(self._run_deploy({
            'XSBOT_WEBCHAT_ENABLED': 'true',
            'XSBOT_WEBCHAT_SIGNING_KEY': 'artificial-signing-key',
            'XSBOT_WEBCHAT_SCENARIO_URI': 's3://test/scenario/' + ('a' * 32),
        }))
        self.assertEqual('true', parameters['WebchatEnabled'])
        self.assertEqual('artificial-signing-key', parameters['WebchatSigningKey'])
        self.assertEqual(
            's3://test/scenario/' + ('a' * 32), parameters['WebchatScenarioUri'])
        self.assertNotIn('WebchatCompatibilityEpoch', parameters)

    def test_有効状態未指定でもoptionalの明示値は送る(self):
        settings = {
            'XSBOT_WEBCHAT_SIGNING_KEY': 'artificial-signing-key',
            'XSBOT_WEBCHAT_SCENARIO_URI': 's3://test/scenario/' + ('b' * 32),
            'XSBOT_WEBCHAT_COMPATIBILITY_EPOCH': 'webchat-v3',
            'XSBOT_WEBCHAT_ALLOWED_ORIGINS': 'https://a.example,https://b.example',
            'XSBOT_WEBCHAT_EXTERNAL_HTTP_ORIGINS': 'https://api.example',
            'XSBOT_WEBCHAT_MEDIA_ORIGINS': 'https://media.example',
            'XSBOT_WEBCHAT_THROTTLE_RATE': '2',
            'XSBOT_WEBCHAT_THROTTLE_BURST': '4',
            'XSBOT_AWS_ALARM_EMAIL': 'test@example.com',
        }
        parameters = self._parameters(self._run_deploy(settings))
        self.assertNotIn('WebchatEnabled', parameters)
        for variable, parameter in (
                ('XSBOT_WEBCHAT_SIGNING_KEY', 'WebchatSigningKey'),
                ('XSBOT_WEBCHAT_SCENARIO_URI', 'WebchatScenarioUri'),
                ('XSBOT_WEBCHAT_COMPATIBILITY_EPOCH', 'WebchatCompatibilityEpoch'),
                ('XSBOT_WEBCHAT_ALLOWED_ORIGINS', 'WebchatAllowedOrigins'),
                ('XSBOT_WEBCHAT_EXTERNAL_HTTP_ORIGINS', 'WebchatExternalHttpOrigins'),
                ('XSBOT_WEBCHAT_MEDIA_ORIGINS', 'WebchatMediaOrigins'),
                ('XSBOT_WEBCHAT_THROTTLE_RATE', 'WebchatThrottleRate'),
                ('XSBOT_WEBCHAT_THROTTLE_BURST', 'WebchatThrottleBurst'),
                ('XSBOT_AWS_ALARM_EMAIL', 'AlarmEmail')):
            self.assertEqual(settings[variable], parameters[parameter])

    def test_originと通知先の明示空を削除として送る(self):
        result = self._run_deploy({
            'XSBOT_WEBCHAT_ALLOWED_ORIGINS': '',
            'XSBOT_WEBCHAT_EXTERNAL_HTTP_ORIGINS': '',
            'XSBOT_WEBCHAT_MEDIA_ORIGINS': '',
            'XSBOT_AWS_ALARM_EMAIL': '',
        })
        parameters = self._parameters(result)
        for name in (
                'WebchatAllowedOrigins', 'WebchatExternalHttpOrigins',
                'WebchatMediaOrigins', 'AlarmEmail'):
            self.assertEqual('', parameters[name])
            self.assertIn(
                f'ParameterKey={name},ParameterValue=""',
                result.stdout.split('\0'))

    def test_有効化に必要な値の不足と不正な明示空はCLI呼出し前に停止する(self):
        cases = [
            {'XSBOT_WEBCHAT_ENABLED': value}
            for value in ('', 'invalid', 'true')
        ]
        cases.append({
            'XSBOT_WEBCHAT_ENABLED': 'true',
            'XSBOT_WEBCHAT_SIGNING_KEY': 'artificial-signing-key',
        })
        cases.extend({name: ''} for name in (
            'XSBOT_WEBCHAT_SIGNING_KEY',
            'XSBOT_WEBCHAT_SCENARIO_URI',
            'XSBOT_WEBCHAT_COMPATIBILITY_EPOCH',
            'XSBOT_WEBCHAT_THROTTLE_RATE',
            'XSBOT_WEBCHAT_THROTTLE_BURST',
        ))
        for values in cases:
            with self.subTest(values=values):
                result = self._run_deploy(values)
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn('call:', result.stderr)
                self.assertEqual('', result.stdout)

    def test_settings欠落と空はCLI呼出し前に停止する(self):
        for content in (None, ''):
            with self.subTest(content=content):
                result = self._run_deploy(settings_content=content)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('settings.yaml', result.stderr)
                self.assertNotIn('call:', result.stderr)


if __name__ == '__main__':
    unittest.main()
