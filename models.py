import json
import random
import string
import hashlib
import copy

import logging

from cloud_backend import create_state_store
from utility import deep_dump

DEBUG = False

_state_store = create_state_store()


def get_state_store():
    """同じmoduleが所有するStateStoreを返す。"""
    return _state_store


class GlobalBotVariablesDB:
    @staticmethod
    def get_by_bot_name(bot_name):
        return _state_store.get_global_bot_variables(bot_name)

    @staticmethod
    def save(bot_name, scenario_uri):
        _state_store.save_global_bot_variables(bot_name, scenario_uri)


class PlayerStatusDB:
    MAX_HISTORY = 5 # ヒストリーは最大5つまで

    def __init__(self, bot_name, user_id):
        self.bot_name = bot_name
        self.user_id = user_id
        self.id = bot_name + ':' + user_id
        state = _state_store.load_player_status(self.id)
        if state is not None:
            data = state.data
            if DEBUG:
                from pprint import pprint
                print('==PlayerStatusDB==')
                pprint(data)
            self.entry = {
                'scene': data.get('scene'),
                'scene_history': data.get('scene_history', []),
                'action_token': data.get('action_token'),
                'value': data.get('value', '{}')
            }
            self.rollback_entry = copy.deepcopy(self.entry)
            self.db = self._str_to_db(self.entry['value'])
            self.last_update_time = state.version
        else:
            self.entry = {
                'scene': '*start',
                'scene_history': [],
                'action_token': None,
                'value': None
            }
            self.rollback_entry = None
            self.db = {}
            self.last_update_time = None
        self.is_dirty = False # is_dirty は self.db 以外の値の更新確認
        if self.action_token is None:
            self.renew_action_token()

    def __getitem__(self, item):
        return self.db[item]

    def __setitem__(self, item, value):
        if DEBUG:
            print(f'PlayerStatusDB: {item} = {value}')
        self.db[item] = value
        # db の更新は保存時に dump して比較する

    def __delitem__(self, item):
        del self.db[item]

    def __contains__(self, item):
        return item in self.db

    def keys(self):
        return list(self.db.keys())

    def get(self, item, default=None):
        return self.db.get(item, default)

    def reset(self):
        self.db = {}
        self.entry['scene'] = None
        self.entry['scene_history'] = []
        self.entry['action_token'] = None
        self.is_dirty = True
        self.renew_action_token()

    @property
    def scene(self):
        return self.entry['scene']

    @scene.setter
    def scene(self, value):
        self.entry['scene'] = value
        self.is_dirty = True

    @property
    def scene_history(self):
        return self.entry['scene_history']

    @scene_history.setter
    def scene_history(self, value):
        self.entry['scene_history'] = value
        self.is_dirty = True

    def push_scene_history(self, scene_title):
        if scene_title is not None:
            scene_history = self.scene_history
            scene_history.append(scene_title)
            self.scene_history = scene_history[-PlayerStatusDB.MAX_HISTORY:]

    def pop_scene_history(self):
        if len(self.scene_history) > 0:
            return self.scene_history.pop()
        return None

    @property
    def action_token(self):
        return self.entry['action_token']

    @action_token.setter
    def action_token(self, value):
        self.entry['action_token'] = value
        self.is_dirty = True

    def renew_action_token(self):
        self.action_token = ''.join([random.choice(string.ascii_letters) for _ in range(8)])

    def __str__(self):
        return str(self.db)

    def _db_to_str(self, db):
        # $_ で始まるローカル変数は保存しない
        return json.dumps({k: v for k, v in db.items() if not k.startswith('$_')})

    def _str_to_db(self, s):
        return json.loads(s)

    def save(self, force=False):
        if DEBUG:
            from pprint import pprint
            print('==DB @ save==')
            deep_dump(self.db)
        new_value = self._db_to_str(self.db)
        if force or self.is_dirty or self.entry['value'] != new_value:
            if DEBUG:
                print('== SAVED ==')
            self.entry['value'] = new_value
            if force:
                self.last_update_time = _state_store.force_put_player_status(
                    self.id, self.entry)
            elif self.last_update_time is not None:
                # 更新の場合は前回の更新時間を指定することで並行実行を排除する
                # 失敗したときは例外が上がるはず
                self.last_update_time = _state_store.update_player_status(
                    self.id, self.entry, self.last_update_time)
            else:
                self.last_update_time = _state_store.create_player_status(
                    self.id, self.entry)
            self.is_dirty = False
        else:
            if DEBUG:
                print('== not saved ==')

    def rollback(self):
        logging.info(f'Rollback: {self.id}')
        if self.rollback_entry is not None:
            self.entry = copy.deepcopy(self.rollback_entry)
            self.db = self._str_to_db(self.entry['value'])
            self.save(force=True)
            self.is_dirty = False
        else:
            _state_store.delete_player_status(self.id)
            self.reset()


class GroupMembersDB:
    @staticmethod
    def _get_shard_id(member):
        # シャードIDとして、user_id の SHA256 ハッシュの 16進数表現先頭2文字 (256個) を使う
        # firestore は 1 エントリが 1MB までなので、1つのシャードには 10000 人程度を想定
        h = hashlib.sha256(member.encode('utf-8')).hexdigest()
        return h[:2]

    @staticmethod
    def get_members(group_id):
        return _state_store.get_group_members(group_id)

    @staticmethod
    def append_member(group_id, member):
        shard_id = GroupMembersDB._get_shard_id(member)
        _state_store.append_group_member(group_id, shard_id, member)

    @staticmethod
    def remove_member(group_id, member):
        shard_id = GroupMembersDB._get_shard_id(member)
        _state_store.remove_group_member(group_id, shard_id, member)

    @staticmethod
    def clear(group_id):
        _state_store.clear_group_members(group_id)

    @staticmethod
    def get_all_groups():
        return _state_store.get_all_groups()


import urllib.parse

class ImageFileStatDB:
    @staticmethod
    def get_cached_image_file_stat(kind, image_url):
        key = f'{kind}|{urllib.parse.quote_plus(image_url)}'
        data = _state_store.get_image_file_stat(key)
        if data is not None:
            return data.get('file_digest'), (data.get('width'), data.get('height'))
        return None

    @staticmethod
    def put_cached_image_file_stat(kind, image_url, file_digest, size):
        key = f'{kind}|{urllib.parse.quote_plus(image_url)}'
        _state_store.put_image_file_stat(key, {
            'file_digest': file_digest,
            'width': size[0],
            'height': size[1]
        })

class MediaFileStatDB:
    @staticmethod
    def get_cached_media_file_stat(kind, media_url):
        key = f'{kind}|{urllib.parse.quote_plus(media_url)}'
        data = _state_store.get_media_file_stat(key)
        if data is not None:
            return data.get('file_type'), data.get('file_size'), data.get('file_digest'), data.get('attributes', {})
        return None

    @staticmethod
    def put_cached_media_file_stat(kind, media_url, file_type, file_size, file_digest, attributes=None):
        key = f'{kind}|{urllib.parse.quote_plus(media_url)}'
        _state_store.put_media_file_stat(key, {
            'file_type': file_type,
            'file_size': file_size,
            'file_digest': file_digest,
            'attributes': attributes or {}
        })
