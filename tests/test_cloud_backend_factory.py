import sys
import types
import unittest
from unittest.mock import patch

from cloud_backend import factory


class CloudBackendFactoryTest(unittest.TestCase):
    def setUp(self):
        factory._reset_for_test()

    def tearDown(self):
        factory._reset_for_test()

    def test_未指定ならgcpを選ぶ(self):
        self.assertEqual('gcp', factory.get_provider())

    def test_起動後にproviderを変更しない(self):
        factory.configure({'provider': 'gcp'})
        with self.assertRaises(RuntimeError):
            factory.configure({'provider': 'aws'})

    def test_未知のproviderは拒否する(self):
        with self.assertRaises(ValueError):
            factory.configure({'provider': 'unknown'})

    def test_選択中providerのfactoryだけを呼ぶ(self):
        module_name = 'cloud_backend.gcp'
        original = sys.modules.get(module_name)
        fake_module = types.ModuleType(module_name)
        fake_module.create_state_store = lambda: 'state'
        fake_module.create_object_store = lambda: 'object'
        fake_module.create_task_queue = lambda: 'task'
        fake_module.create_credential_source = lambda: 'credential'
        sys.modules[module_name] = fake_module
        try:
            factory.configure({'provider': 'gcp'})
            self.assertEqual('state', factory.create_state_store())
            self.assertEqual('object', factory.create_object_store())
            self.assertEqual('task', factory.create_task_queue())
            self.assertEqual('credential', factory.create_credential_source())
        finally:
            if original is None:
                del sys.modules[module_name]
            else:
                sys.modules[module_name] = original

    def test_AWS選択時はGCPmoduleをimportしない(self):
        fake_module = types.SimpleNamespace(
            create_state_store=lambda: 'aws-state')
        factory.configure({'provider': 'aws'})

        with patch.object(
                factory.importlib, 'import_module',
                return_value=fake_module) as importer:
            self.assertEqual('aws-state', factory.create_state_store())

        importer.assert_called_once_with('cloud_backend.aws')


if __name__ == '__main__':
    unittest.main()
