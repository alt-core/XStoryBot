# coding: utf-8
import json

from google.appengine.ext import ndb


class GlobalBotVariables(ndb.Model):
    scenario_counter = ndb.IntegerProperty()


class GroupMembers(ndb.Model):
    members = ndb.StringProperty(repeated=True)


class PlayerState(ndb.Model):
    scene = ndb.StringProperty()
    scene_history = ndb.StringProperty(repeated=True)
    visit_id = ndb.StringProperty()
    value = ndb.StringProperty()


class PlayerStateDB(object):
    def __init__(self, user_id):
        self.entry = PlayerState.get_by_id(user_id)
        if self.entry:
            self.db = json.loads(self.entry.value) or {}
        else:
            self.entry = PlayerState(id=user_id, value="{}")
            self.db = {}
        self.is_dirty = False
        self.is_values_dirty = False

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

    @property
    def visit_id(self):
        return self.entry.visit_id

    @visit_id.setter
    def visit_id(self, value):
        self.entry.visit_id = value
        self.is_dirty = True

    def __str__(self):
        return str(self.db)

    def save(self):
        if self.is_dirty:
            if self.is_values_dirty:
                self.entry.value = json.dumps(self.db)
            self.entry.put()
