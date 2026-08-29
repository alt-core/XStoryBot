import json
import unittest

from cloud_backend.contracts import CredentialData, CredentialSourceError
from cloud_backend.gcp.credential_source import GcpCredentialSource


class GcpCredentialSourceTest(unittest.TestCase):
    def test_管理者認証JSONを指定環境変数から取得する(self):
        source = GcpCredentialSource(
            auth_settings={'admin_auth_json_env': 'TEST_ADMIN_AUTH'},
            environ={'TEST_ADMIN_AUTH': '{"users":{}}'},
        )

        self.assertEqual('{"users":{}}', source.get_admin_auth_json())

    def test_管理者認証JSONの未設定を拒否する(self):
        source = GcpCredentialSource(
            auth_settings={'admin_auth_json_env': 'TEST_ADMIN_AUTH'},
            environ={},
        )

        with self.assertRaises(CredentialSourceError):
            source.get_admin_auth_json()

    def test_既定の管理者認証環境変数名を使う(self):
        source = GcpCredentialSource(
            auth_settings={},
            environ={'XSBOT_ADMIN_AUTH_JSON': '{"users":{"admin":"hash"}}'},
        )

        self.assertEqual(
            '{"users":{"admin":"hash"}}', source.get_admin_auth_json())

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

    def test_不正なGoogle資格情報JSONを拒否する(self):
        source = GcpCredentialSource()

        for invalid in ('{"private_key":"secret-value"', '["secret-value"]'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CredentialSourceError) as raised:
                    source.get_google_service_account(invalid)
                self.assertNotIn('secret-value', str(raised.exception))

    def test_不明な参照型を拒否する(self):
        with self.assertRaises(CredentialSourceError):
            GcpCredentialSource().get_google_service_account(123)


if __name__ == '__main__':
    unittest.main()
