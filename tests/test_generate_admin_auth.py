from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from tools.generate_admin_auth import PASSWORD_HASHER, generate, main


class GenerateAdminAuthTest(unittest.TestCase):
    def test_8文字のパスワードでArgon2idハッシュと署名鍵を含むJSONを生成する(self):
        result = json.loads(generate(
            ' admin ',
            'testpass',
            'testpass',
            secret_factory=lambda: 's' * 64,
        ))

        password_hash = result['users']['admin']
        self.assertTrue(password_hash.startswith('$argon2id$'))
        self.assertTrue(PASSWORD_HASHER.verify(
            password_hash, 'testpass'))
        self.assertEqual('s' * 64, result['session_secret'])

    def test_空入力と確認不一致を拒否する(self):
        invalid = (
            ('', 'long-password-value', 'long-password-value'),
            ('admin', '', ''),
            ('admin', 'short-7', 'short-7'),
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

    def test_標準出力を保存してもJSONだけが入る(self):
        output, prompts = io.StringIO(), io.StringIO()
        with patch('sys.stdin', io.StringIO('admin\n')), \
                patch('tools.generate_admin_auth.getpass.getpass', return_value='testpass'), \
                redirect_stdout(output), redirect_stderr(prompts):
            main([])

        result = json.loads(output.getvalue())
        self.assertTrue(PASSWORD_HASHER.verify(result['users']['admin'], 'testpass'))
        self.assertIn('管理者ユーザー名:', prompts.getvalue())


if __name__ == '__main__':
    unittest.main()
