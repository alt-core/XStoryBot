import json
import unittest

from cloud_backend.contracts import CredentialData, CredentialSourceError
from cloud_backend.gcp.credential_source import GcpCredentialSource


class GcpCredentialSourceTest(unittest.TestCase):
    def test_Firebase資格情報は専用設定から取得する(self):
        source = GcpCredentialSource({
            'firebase_credentials_path': '/keys/firebase.json',
        })

        credential = source.get_admin_auth_credential()

        self.assertEqual('/keys/firebase.json', credential.file_path)
        self.assertIsNone(credential.inline_json)
        self.assertFalse(credential.use_default)

    def test_Firebase_client設定を既存と同じ形で返す(self):
        source = GcpCredentialSource(gcp_settings={
            'project_id': 'test-project',
            'firebase': {
                'api_key': 'test-api-key',
                'auth_domain': 'test-project.firebaseapp.com',
                'storage_bucket': 'test-project.firebasestorage.app',
                'messaging_sender_id': '1234567890',
                'app_id': 'test-app-id',
            },
        })

        self.assertEqual(source.get_admin_auth_client_config(), {
            'apiKey': 'test-api-key',
            'authDomain': 'test-project.firebaseapp.com',
            'projectId': 'test-project',
            'storageBucket': 'test-project.firebasestorage.app',
            'messagingSenderId': '1234567890',
            'appId': 'test-app-id',
        })

    def test_GCPとSheetsの異なるpathをそのまま保持する(self):
        source = GcpCredentialSource()

        gcp = source.get_google_service_account('/keys/gcp.json')
        sheets = source.get_google_service_account('/keys/sheets.json')

        self.assertEqual('/keys/gcp.json', gcp.file_path)
        self.assertEqual('/keys/sheets.json', sheets.file_path)

    def test_空pathのADC条件を呼出し側が選べる(self):
        source = GcpCredentialSource()

        required = source.get_google_service_account('', allow_default=False)
        optional = source.get_google_service_account('', allow_default=True)

        self.assertEqual('', required.file_path)
        self.assertFalse(required.use_default)
        self.assertIsNone(optional.file_path)
        self.assertTrue(optional.use_default)

    def test_inline_JSONとCredentialDataを受け入れる(self):
        source = GcpCredentialSource()
        original = CredentialData(inline_json='{"project_id":"test"}')

        self.assertIs(
            original, source.get_google_service_account(original))
        mapped = source.get_google_service_account({'project_id': 'test'})
        self.assertEqual(
            {'project_id': 'test'}, json.loads(mapped.inline_json))

        inline = source.get_google_service_account(
            '  {"project_id":"inline-test"}')
        self.assertEqual(
            {'project_id': 'inline-test'}, json.loads(inline.inline_json))
        self.assertIsNone(inline.file_path)

    def test_壊れたinline_JSONを秘密値なしの共通例外で拒否する(self):
        invalid = '{"private_key":"secret-value"'

        with self.assertRaises(CredentialSourceError) as raised:
            GcpCredentialSource().get_google_service_account(invalid)

        self.assertNotIn('secret-value', str(raised.exception))

    def test_不明な参照型を拒否する(self):
        with self.assertRaises(CredentialSourceError):
            GcpCredentialSource().get_google_service_account(123)


if __name__ == '__main__':
    unittest.main()
