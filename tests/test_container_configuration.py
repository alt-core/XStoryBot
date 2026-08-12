from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ContainerConfigurationTest(unittest.TestCase):
    def test_dockerfile_uses_approved_runtime_and_entrypoints(self):
        dockerfile = (PROJECT_ROOT / 'Dockerfile').read_text(encoding='utf-8')

        self.assertIn('FROM python:3.11-slim', dockerfile)
        adapter_lines = [
            line.strip()
            for line in dockerfile.splitlines()
            if '/lambda-adapter /opt/extensions/lambda-adapter' in line
        ]
        self.assertEqual(adapter_lines, [
            'COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 '
            '/lambda-adapter /opt/extensions/lambda-adapter',
        ])
        self.assertIn('USER xstorybot', dockerfile)
        self.assertIn('settings.yaml.template settings.yaml', dockerfile)
        self.assertIn('${XSBOT_APP_MODULE:-app:app}', dockerfile)
        self.assertIn('/healthz', dockerfile)
        self.assertIn('gunicorn', dockerfile)

        public_app = (PROJECT_ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertNotIn("'/events'", public_app)
        self.assertNotIn('"/events"', public_app)

    def test_legacy_gae_configuration_is_absent(self):
        paths = (
            'app_dev1.yaml',
            'app_prod.yaml',
            'builder_dev1.yaml',
            'builder_prod.yaml',
            'appengine_config.py',
            'plugin/liff/app.yaml',
            'plugin/line/app.yaml',
            'plugin/render_text/app.yaml',
            'plugin/twilio/app.yaml',
        )

        for path in paths:
            with self.subTest(path=path):
                self.assertFalse((PROJECT_ROOT / path).exists())


if __name__ == '__main__':
    unittest.main()
