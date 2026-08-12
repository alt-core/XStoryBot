from pathlib import Path
import stat
import subprocess
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
                'RuntimeSecretsParameterName'):
            self.assertIn(f'ParameterKey={name},ParameterValue=', self.source)

        self.assertIn('sam validate', self.source)
        self.assertIn('--lint', self.source)
        self.assertIn('sam deploy', self.source)
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


if __name__ == '__main__':
    unittest.main()
