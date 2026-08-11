import unittest

import auth


class AuthTest(unittest.TestCase):
    def tearDown(self):
        auth.setup({'api_token': ''})

    def test_configured_token_is_accepted(self):
        auth.setup({'api_token': 'expected-token'})

        self.assertTrue(auth.check_token('expected-token'))
        self.assertFalse(auth.check_token('different-token'))

    def test_empty_token_is_always_rejected(self):
        auth.setup({'api_token': ''})

        self.assertFalse(auth.check_token(''))
        self.assertFalse(auth.check_token(None))

    def test_get_api_token_returns_configured_value(self):
        auth.setup({'api_token': 'expected-token'})

        self.assertEqual(auth.get_api_token(), 'expected-token')


if __name__ == '__main__':
    unittest.main()
