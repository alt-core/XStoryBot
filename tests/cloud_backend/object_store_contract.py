"""各ObjectStore実装へ同じ外部契約を適用するMixin。"""

import hashlib
from urllib.parse import urlsplit

from cloud_backend.contracts import (
    InvalidObjectReferenceError,
    ObjectNotFoundError,
)


class ObjectStoreContractMixin:
    """SDK形式を見ずにObjectStoreの共通意味論だけを検査する。"""

    def create_contract_store(self):
        raise NotImplementedError

    def foreign_scenario_reference(self, key):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.contract_store = self.create_contract_store()

    def test_scenarioをopaque参照でbytes_round_tripする(self):
        data = b'pickle-data'
        key = f'scenario/{hashlib.md5(data).hexdigest()}'

        reference = self.contract_store.store_scenario(key, data)

        self.assertIsInstance(reference, str)
        self.assertTrue(reference)
        self.assertEqual(data, self.contract_store.load_scenario(reference))

    def test_scenarioは異なるbackend参照を共通例外で拒否する(self):
        key = 'scenario/00000000000000000000000000000000'

        with self.assertRaises(InvalidObjectReferenceError):
            self.contract_store.load_scenario(
                self.foreign_scenario_reference(key))

    def test_privateは文字列とbytesを上書き保存できる(self):
        key = 'group_tasks/task/members.json'

        reference = self.contract_store.store_private(key, '["ユーザー"]')
        self.assertIsInstance(reference, str)
        self.assertTrue(reference)
        self.assertEqual(
            '["ユーザー"]'.encode('utf-8'),
            self.contract_store.load_private(key),
        )

        self.contract_store.store_private(key, b'["updated"]')
        self.assertEqual(b'["updated"]', self.contract_store.load_private(key))

    def test_private_missingは共通NotFoundを返す(self):
        with self.assertRaises(ObjectNotFoundError):
            self.contract_store.load_private('group_tasks/missing/members.json')

    def test_public保存の返却値はpublic_urlと同じHTTPS_URLになる(self):
        key = 'image/digest image.png'

        stored_url = self.contract_store.store_public(
            key, b'image', 'image/png')

        self.assertEqual(stored_url, self.contract_store.public_url(key))
        self.assertEqual('https', urlsplit(stored_url).scheme)
