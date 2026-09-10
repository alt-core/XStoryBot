"""信頼するローカルデータをSQLiteへ保存する。"""

from contextlib import contextmanager
import datetime
import pickle
import sqlite3
import time
import uuid

from cloud_backend.contracts import (
    StateConflictError, StateStore, StateStoreError, StateVersion, VersionedState,
)
from cloud_backend.local.storage import checked_path, prepare_root


class LocalStateStore(StateStore):
    def __init__(self, storage_root):
        self.storage_root = prepare_root(storage_root)
        self.database_path = checked_path(self.storage_root, 'state.sqlite3')
        with self._connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, key TEXT NOT NULL, payload BLOB NOT NULL,
                version TEXT, expires_at REAL, PRIMARY KEY (kind, key))''')

    @contextmanager
    def _connection(self, write=False):
        connection = None
        try:
            checked_path(self.storage_root, 'state.sqlite3')
            connection = sqlite3.connect(self.database_path, timeout=5)
            if write:
                connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            if connection is not None:
                connection.rollback()
            raise StateStoreError(str(error)) from error
        except Exception:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _read(connection, kind, key):
        row = connection.execute(
            'SELECT payload, version, expires_at FROM records WHERE kind=? AND key=?',
            (kind, key)).fetchone()
        if row is None or (row[2] is not None and row[2] <= time.time()):
            return None
        return pickle.loads(row[0]), row[1]

    @staticmethod
    def _write(connection, kind, key, data, version=None, expires_at=None):
        connection.execute('''INSERT INTO records VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(kind, key) DO UPDATE SET payload=excluded.payload,
                version=excluded.version, expires_at=excluded.expires_at''',
            (kind, key, pickle.dumps(data), version, expires_at))

    def _get(self, kind, key):
        with self._connection() as connection:
            row = self._read(connection, kind, key)
            return row[0] if row is not None else None

    def _put(self, kind, key, data, expires_at=None):
        with self._connection(write=True) as connection:
            self._write(connection, kind, key, data, expires_at=expires_at)

    def _delete(self, kind, key):
        with self._connection(write=True) as connection:
            connection.execute('DELETE FROM records WHERE kind=? AND key=?', (kind, key))

    def get_global_bot_variables(self, bot_name):
        return self._get('global', bot_name)

    def save_global_bot_variables(self, bot_name, scenario_uri):
        self._put('global', bot_name, {'scenario_uri': scenario_uri})

    def load_player_status(self, status_id):
        with self._connection() as connection:
            row = self._read(connection, 'player', status_id)
            return VersionedState(row[0], StateVersion(row[1])) if row else None

    def create_player_status(self, status_id, data):
        version = uuid.uuid4().hex
        with self._connection(write=True) as connection:
            if self._read(connection, 'player', status_id) is not None:
                raise StateConflictError('Player状態は既に作成されています')
            self._write(connection, 'player', status_id, dict(data), version)
        return StateVersion(version)

    def update_player_status(self, status_id, data, version):
        if not isinstance(version, StateVersion):
            raise TypeError('versionにはStateVersionを指定してください')
        next_version = uuid.uuid4().hex
        with self._connection(write=True) as connection:
            updated = connection.execute('''UPDATE records SET payload=?, version=?
                WHERE kind='player' AND key=? AND version=?''',
                (pickle.dumps(dict(data)), next_version, status_id, version.value))
            if updated.rowcount != 1:
                raise StateConflictError('Player状態が別の書込みで変更されています')
        return StateVersion(next_version)

    def force_put_player_status(self, status_id, data):
        version = uuid.uuid4().hex
        with self._connection(write=True) as connection:
            self._write(connection, 'player', status_id, dict(data), version)
        return StateVersion(version)

    def delete_player_status(self, status_id):
        self._delete('player', status_id)

    def get_group_members(self, group_id):
        shards = self._get('group', group_id) or {}
        return [member for members in shards.values() for member in members]

    def append_group_member(self, group_id, shard_id, member):
        with self._connection(write=True) as connection:
            row = self._read(connection, 'group', group_id)
            shards = row[0] if row else {}
            members = shards.setdefault(shard_id, [])
            if member not in members:
                members.append(member)
                self._write(connection, 'group', group_id, shards)

    def remove_group_member(self, group_id, shard_id, member):
        with self._connection(write=True) as connection:
            row = self._read(connection, 'group', group_id)
            if row is not None:
                members = row[0].get(shard_id, [])
                if member in members:
                    members.remove(member)
                    self._write(connection, 'group', group_id, row[0])

    def clear_group_members(self, group_id):
        self._delete('group', group_id)

    def get_all_groups(self):
        with self._connection() as connection:
            return [{'id': row[0]} for row in connection.execute(
                "SELECT key FROM records WHERE kind='group' ORDER BY key")]

    def get_image_file_stat(self, key):
        return self._get('image_file', key)

    def put_image_file_stat(self, key, data):
        self._put('image_file', key, dict(data))

    def get_media_file_stat(self, key):
        return self._get('media_file', key)

    def put_media_file_stat(self, key, data):
        self._put('media_file', key, dict(data))

    def get_image_text_stat(self, key):
        return self._get('image_text', key)

    def put_image_text_stat(self, key, data):
        self._put('image_text', key, dict(data))

    def get_next_label(self, status_id):
        return self._get('next_label', status_id) or (None, None)

    def set_next_label(self, status_id, label, trigger_message):
        with self._connection(write=True) as connection:
            row = self._read(connection, 'next_label', status_id)
            previous = row[0] if row and row[0][0] else (None, None)
            self._write(connection, 'next_label', status_id, (label, trigger_message))
            return previous

    def compare_and_clear_next_label(self, status_id, next_label):
        with self._connection(write=True) as connection:
            row = self._read(connection, 'next_label', status_id)
            if row and row[0][0] == next_label:
                self._write(connection, 'next_label', status_id, (None, None))
                return row[0]
            return None, None

    def clear_next_label(self, status_id):
        self._put('next_label', status_id, (None, None))

    def get_build_cache(self, key):
        return self._get('cache', key)

    def set_build_cache(self, key, value, expire_at=None):
        expires_at = expire_at.timestamp() if expire_at is not None else None
        self._put('cache', key, value, expires_at=expires_at)

    def delete_build_cache(self, key):
        self._delete('cache', key)

    def clear_build_cache(self):
        with self._connection(write=True) as connection:
            connection.execute("DELETE FROM records WHERE kind='cache'")

    @staticmethod
    def _task_datetimes(data):
        result = dict(data)
        for key in ('created_at', 'updated_at', 'scheduled_at'):
            value = result.get(key)
            if isinstance(value, datetime.datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=datetime.timezone.utc)
                result[key] = value.astimezone(datetime.timezone.utc)
        return result

    def create_group_message_task(self, task_id, data):
        data = self._task_datetimes(data)
        now = datetime.datetime.now(datetime.timezone.utc)
        data.setdefault('created_at', now)
        data.setdefault('updated_at', now)
        self._put('task', task_id, data)

    def get_group_message_task(self, task_id):
        return self._get('task', task_id)

    def update_group_message_task(self, task_id, update_builder):
        with self._connection(write=True) as connection:
            row = self._read(connection, 'task', task_id)
            if row is None:
                return False
            data = row[0]
            update = self._task_datetimes(update_builder(dict(data)))
            update.setdefault('updated_at', datetime.datetime.now(datetime.timezone.utc))
            data.update(update)
            self._write(connection, 'task', task_id, data)
            return True

    def get_recent_group_message_tasks(self, bot_name, limit):
        with self._connection() as connection:
            tasks = []
            for key, payload in connection.execute(
                    "SELECT key, payload FROM records WHERE kind='task'"):
                data = pickle.loads(payload)
                if data.get('bot_name') == bot_name:
                    tasks.append(dict(data, id=key))
        tasks.sort(key=lambda data: data['created_at'], reverse=True)
        return tasks[:max(0, limit)]
