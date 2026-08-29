"""各StateStore実装へ同じ外部契約を適用するMixin。"""

import datetime

from cloud_backend.contracts import (
    StateConflictError,
    StateVersion,
    VersionedState,
)


class StateStoreContractMixin:
    """保存先SDKの形式を見ずにStateStoreの共通意味論を検査する。"""

    def create_contract_store(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.contract_store = self.create_contract_store()

    def test_Bot共通変数はmissingと上書きを区別する(self):
        self.assertIsNone(
            self.contract_store.get_global_bot_variables('bot-a'))

        result = self.contract_store.save_global_bot_variables(
            'bot-a', 'opaque://scenario/1')
        self.assertIsNone(result)
        self.assertEqual(
            {'scenario_uri': 'opaque://scenario/1'},
            self.contract_store.get_global_bot_variables('bot-a'),
        )

        self.contract_store.save_global_bot_variables(
            'bot-a', 'opaque://scenario/2')
        self.assertEqual(
            {'scenario_uri': 'opaque://scenario/2'},
            self.contract_store.get_global_bot_variables('bot-a'),
        )

    def test_Player状態は楽観ロックと強制更新を共通化する(self):
        status_id = 'bot-a:line:user-1'
        self.assertIsNone(self.contract_store.load_player_status(status_id))

        created_version = self.contract_store.create_player_status(
            status_id, {'scene': 'scene-1', 'value': '{}'})
        self.assertIsInstance(created_version, StateVersion)
        with self.assertRaises(StateConflictError):
            self.contract_store.create_player_status(
                status_id, {'scene': 'duplicate'})

        loaded = self.contract_store.load_player_status(status_id)
        self.assertIsInstance(loaded, VersionedState)
        self.assertEqual(
            {'scene': 'scene-1', 'value': '{}'}, dict(loaded.data))
        self.assertEqual(created_version, loaded.version)

        updated_version = self.contract_store.update_player_status(
            status_id,
            {'scene': 'scene-2', 'value': '{"count": 1}'},
            loaded.version,
        )
        self.assertIsInstance(updated_version, StateVersion)
        self.assertNotEqual(loaded.version, updated_version)
        with self.assertRaises(StateConflictError):
            self.contract_store.update_player_status(
                status_id, {'scene': 'stale'}, loaded.version)

        forced_version = self.contract_store.force_put_player_status(
            status_id, {'scene': 'forced', 'value': '{}'})
        self.assertIsInstance(forced_version, StateVersion)
        self.assertEqual(
            {'scene': 'forced', 'value': '{}'},
            dict(self.contract_store.load_player_status(status_id).data),
        )

        self.assertIsNone(
            self.contract_store.delete_player_status(status_id))
        self.assertIsNone(self.contract_store.load_player_status(status_id))
        self.assertIsNone(
            self.contract_store.delete_player_status(status_id))

    def test_Group構成は重複せず一覧と削除を共通化する(self):
        self.assertEqual([], self.contract_store.get_group_members('group-a'))

        self.assertIsNone(self.contract_store.append_group_member(
            'group-a', 'shard-1', 'line:user-1'))
        self.contract_store.append_group_member(
            'group-a', 'shard-1', 'line:user-1')
        self.contract_store.append_group_member(
            'group-a', 'shard-2', 'line:user-2')
        self.contract_store.append_group_member(
            'group-b', 'shard-1', 'line:user-3')

        self.assertCountEqual(
            ['line:user-1', 'line:user-2'],
            self.contract_store.get_group_members('group-a'),
        )
        self.assertCountEqual(
            [{'id': 'group-a'}, {'id': 'group-b'}],
            self.contract_store.get_all_groups(),
        )

        self.assertIsNone(self.contract_store.remove_group_member(
            'group-a', 'shard-1', 'line:user-1'))
        self.assertIsNone(self.contract_store.remove_group_member(
            'group-a', 'shard-1', 'line:user-1'))
        self.assertEqual(
            ['line:user-2'],
            self.contract_store.get_group_members('group-a'),
        )

        self.assertIsNone(
            self.contract_store.clear_group_members('group-a'))
        self.assertEqual([], self.contract_store.get_group_members('group-a'))
        self.assertEqual(
            [{'id': 'group-b'}],
            self.contract_store.get_all_groups(),
        )

        # clearと並行した古い書き込みが後から可視化されないよう、AWSでは
        # generationを進める。共通契約としては再作成後に新世代だけを返す。
        self.contract_store.append_group_member(
            'group-a', 'shard-3', 'line:user-4')
        self.assertEqual(
            ['line:user-4'],
            self.contract_store.get_group_members('group-a'),
        )

    def test_三種類の統計を独立して保存する(self):
        operations = (
            (
                self.contract_store.get_image_file_stat,
                self.contract_store.put_image_file_stat,
            ),
            (
                self.contract_store.get_media_file_stat,
                self.contract_store.put_media_file_stat,
            ),
            (
                self.contract_store.get_image_text_stat,
                self.contract_store.put_image_text_stat,
            ),
        )
        for index, (getter, putter) in enumerate(operations):
            key = f'key-{index}'
            data = {'url': f'https://example.invalid/{index}', 'size': index}
            with self.subTest(index=index):
                self.assertIsNone(getter(key))
                self.assertIsNone(putter(key, data))
                self.assertEqual(data, getter(key))

    def test_次ラベルは上書き値とcompare_and_clearを返す(self):
        status_id = 'bot-a:line:user-1'
        self.assertEqual(
            (None, None), self.contract_store.get_next_label(status_id))

        self.assertEqual(
            (None, None),
            self.contract_store.set_next_label(
                status_id, '##FIRST', '最初の入力'),
        )
        self.assertEqual(
            ('##FIRST', '最初の入力'),
            self.contract_store.set_next_label(
                status_id, '##SECOND', '次の入力'),
        )
        self.assertEqual(
            (None, None),
            self.contract_store.compare_and_clear_next_label(
                status_id, '##FIRST'),
        )
        self.assertEqual(
            ('##SECOND', '次の入力'),
            self.contract_store.get_next_label(status_id),
        )
        self.assertEqual(
            ('##SECOND', '次の入力'),
            self.contract_store.compare_and_clear_next_label(
                status_id, '##SECOND'),
        )
        self.assertEqual(
            (None, None), self.contract_store.get_next_label(status_id))

        self.contract_store.set_next_label(status_id, '##THIRD', None)
        self.assertIsNone(self.contract_store.clear_next_label(status_id))
        self.assertEqual(
            (None, None), self.contract_store.get_next_label(status_id))

    def test_build_cacheはbytesと期限を保存して個別全体削除できる(self):
        expire_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=30)
        )
        self.assertIsNone(self.contract_store.get_build_cache('cache-a'))

        self.assertIsNone(self.contract_store.set_build_cache(
            'cache-a', b'\x00binary-a', expire_at=expire_at))
        self.assertEqual(
            b'\x00binary-a',
            self.contract_store.get_build_cache('cache-a'),
        )
        self.contract_store.set_build_cache('cache-b', b'binary-b')

        self.assertIsNone(
            self.contract_store.delete_build_cache('cache-a'))
        self.assertIsNone(self.contract_store.get_build_cache('cache-a'))
        self.assertEqual(
            b'binary-b', self.contract_store.get_build_cache('cache-b'))

        self.assertIsNone(self.contract_store.clear_build_cache())
        self.assertIsNone(self.contract_store.get_build_cache('cache-b'))

    def test_配信Taskは日時と更新と最近順を共通化する(self):
        old_time = datetime.datetime(
            2026, 8, 12, 1, 0, tzinfo=datetime.timezone.utc)
        new_time = datetime.datetime(
            2026, 8, 12, 2, 0, tzinfo=datetime.timezone.utc)
        other_time = datetime.datetime(
            2026, 8, 12, 3, 0, tzinfo=datetime.timezone.utc)

        self.assertIsNone(
            self.contract_store.get_group_message_task('missing'))
        self.assertFalse(self.contract_store.update_group_message_task(
            'missing', lambda current: {'status': 'unexpected'}))

        self.assertIsNone(self.contract_store.create_group_message_task(
            'task-old', {
                'bot_name': 'bot-a',
                'status': 'pending',
                'created_at': old_time,
                'updated_at': old_time,
            }))
        self.contract_store.create_group_message_task('task-new', {
            'bot_name': 'bot-a',
            'status': 'pending',
            'created_at': new_time,
            'updated_at': new_time,
        })
        self.contract_store.create_group_message_task('task-other', {
            'bot_name': 'bot-b',
            'status': 'pending',
            'created_at': other_time,
            'updated_at': other_time,
        })

        current = self.contract_store.get_group_message_task('task-old')
        self.assertIs(type(current['created_at']), datetime.datetime)
        self.assertEqual(datetime.timezone.utc, current['created_at'].tzinfo)

        received = {}

        def update_builder(task):
            received.update(task)
            return {'status': 'running', 'scheduled_at': new_time}

        self.assertTrue(self.contract_store.update_group_message_task(
            'task-old', update_builder))
        self.assertIs(type(received['created_at']), datetime.datetime)
        self.assertEqual('running', self.contract_store.get_group_message_task(
            'task-old')['status'])

        recent = self.contract_store.get_recent_group_message_tasks(
            'bot-a', 2)
        self.assertCountEqual(
            ['task-new', 'task-old'], [task['id'] for task in recent])
        self.assertTrue(all(
            type(task['created_at']) is datetime.datetime
            and task['created_at'].tzinfo == datetime.timezone.utc
            for task in recent
        ))
        self.assertEqual(
            [], self.contract_store.get_recent_group_message_tasks(
                'missing-bot', 2))
