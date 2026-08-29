import json
import unittest

from tools.generate_admin_auth import PASSWORD_HASHER, generate


class GenerateAdminAuthTest(unittest.TestCase):
    def test_Argon2idハッシュと署名鍵を含むJSONを生成する(self):
        result = json.loads(generate(
            ' admin ',
            'long-password-value',
            'long-password-value',
            secret_factory=lambda: 's' * 64,
        ))

        password_hash = result['users']['admin']
        self.assertTrue(password_hash.startswith('$argon2id$'))
        self.assertTrue(PASSWORD_HASHER.verify(
            password_hash, 'long-password-value'))
        self.assertEqual('s' * 64, result['session_secret'])

    def test_空入力と確認不一致を拒否する(self):
        invalid = (
            ('', 'long-password-value', 'long-password-value'),
            ('admin', '', ''),
            ('admin', 'short-password', 'short-password'),
            ('admin', 'long-password-value', 'different-password'),
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    generate(*arguments)

    def test_短い署名鍵を拒否する(self):
        with self.assertRaises(ValueError):
            generate(
                'admin', 'long-password-value', 'long-password-value',
                secret_factory=lambda: 'short',
            )


if __name__ == '__main__':
    unittest.main()
