# coding: utf-8
import json
import random
import string

from google.appengine.ext import ndb


class GlobalBotVariables(ndb.Model):
    scenario_uri = ndb.StringProperty()


class GroupMembers(ndb.Model):
    members = ndb.StringProperty(repeated=True)


class PlayerStatus(ndb.Model):
    scene = ndb.StringProperty()
    scene_history = ndb.StringProperty(repeated=True)
    visit_id = ndb.StringProperty()
    value = ndb.TextProperty()


class PlayerStatusDB(object):
    MAX_HISTORY = 5 # ヒストリーは最大5つまで

    def __init__(self, user_id):
        self.id = user_id
        self.entry = PlayerStatus.get_by_id(user_id)
        if self.entry:
            self.db = json.loads(self.entry.value) or {}
        else:
            self.entry = PlayerStatus(id=user_id, scene="*start", value="{}")
            self.db = {}
        self.is_dirty = False
        self.is_values_dirty = False
        if self.visit_id is None:
            self.create_visit_id()

    def __getitem__(self, item):
        value = self.db[item]
        return value

    def __setitem__(self, item, value):
        if isinstance(value, list) or isinstance(value, dict):
            is_ref = True
        else:
            is_ref = False
        if item not in self.db or (self.db[item] != value or is_ref):
            # 参照型は直接中身を書き換えられてしまうと更新チェックができないので、保守的に倒す
            self.db[item] = value
            self.is_dirty = True
            self.is_values_dirty = True

    def __delitem__(self, item):
        del self.db[item]
        self.is_dirty = True
        self.is_values_dirty = True

    def __contains__(self, item):
        return item in self.db

    def keys(self):
        return self.db.keys()

    def get(self, item, default):
        if item in self:
            return self[item]
        else:
            return default

    def reset(self):
        self.db = {}
        self.entry.scene = None
        self.entry.scene_history = []
        self.is_dirty = True
        self.is_values_dirty = True
        self.create_visit_id()

    @property
    def scene(self):
        return self.entry.scene

    @scene.setter
    def scene(self, value):
        self.entry.scene = value
        self.is_dirty = True

    @property
    def scene_history(self):
        return self.entry.scene_history

    @scene_history.setter
    def scene_history(self, value):
        self.entry.scene_history = value
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
    def visit_id(self):
        return self.entry.visit_id

    @visit_id.setter
    def visit_id(self, value):
        self.entry.visit_id = value
        self.is_dirty = True
        
    def create_visit_id(self):
        self.visit_id = \
            u''.join([random.choice(string.ascii_letters) for _ in range(8)])

    def __str__(self):
        return str(self.db)

    def save(self):
        if self.is_dirty:
            if self.is_values_dirty:
                self.entry.value = json.dumps(self.db)
            self.entry.put()


class GroupDB(object):
    def __init__(self, group_id):
        self.entry = GroupMembers.get_by_id(id=group_id)
        if self.entry is None:
            self.entry = GroupMembers(id=group_id, members=[])

    def append_member(self, member):
        if member not in self.entry.members:
            self.entry.members.append(member)
            self.entry.put()

    def remove_member(self, member):
        if member in self.entry.members:
            self.entry.members.remove(member)
            self.entry.put()

    def clear(self):
        if self.entry.members:
            del self.entry.members[:]
            self.entry.put()


class ImageFileStatDB(ndb.Model):
    file_digest = ndb.StringProperty()
    width = ndb.IntegerProperty()
    height = ndb.IntegerProperty()

    @classmethod
    def get_cached_image_file_stat(cls, kind, image_url):
        key = u'{}|{}'.format(kind, image_url)
        stat = cls.get_by_id(id=key)
        if stat is None:
            return None
        size = (stat.width, stat.height)
        return stat.file_digest, size

    @classmethod
    def put_cached_image_file_stat(cls, kind, image_url, file_digest, size):
        key = u'{}|{}'.format(kind, image_url)
        entry = cls.get_by_id(id=key)
        if entry is None:
            entry = cls(id=key, file_digest=file_digest, width=size[0], height=size[1])
        else:
            if entry.file_digest == file_digest:
                # 更新しない
                return
            entry.file_digest = file_digest
            entry.width, entry.height = size
        entry.put()


