import copy
import secrets
import string


class TokenPlayerStatus:
    """署名tokenから復元するDB非依存のPlayer状態。"""

    MAX_HISTORY = 5

    def __init__(self, bot_name, conversation_id, player=None):
        self.bot_name = bot_name
        self.user_id = conversation_id
        self.id = f'{bot_name}:webchat:{conversation_id}'
        source = player or {}
        self.db = copy.deepcopy(source.get('flags', {}))
        self._scene = source.get('scene', '*start')
        self._scene_history = copy.deepcopy(source.get('scene_history', []))
        self._action_token = source.get('action_generation')
        self.web_next_label = source.get('web_next_label')
        self.web_next_trigger = source.get('web_next_trigger')
        if not self._action_token:
            self.renew_action_token()
        self._rollback = self.export()

    def __getitem__(self, item):
        return self.db[item]

    def __setitem__(self, item, value):
        self.db[item] = value

    def __delitem__(self, item):
        del self.db[item]

    def __contains__(self, item):
        return item in self.db

    def keys(self):
        return list(self.db.keys())

    def get(self, item, default=None):
        return self.db.get(item, default)

    @property
    def scene(self):
        return self._scene

    @scene.setter
    def scene(self, value):
        self._scene = value

    @property
    def scene_history(self):
        return self._scene_history

    @scene_history.setter
    def scene_history(self, value):
        self._scene_history = value

    def push_scene_history(self, scene_title):
        if scene_title is not None:
            self._scene_history.append(scene_title)
            self._scene_history = self._scene_history[-self.MAX_HISTORY:]

    def pop_scene_history(self):
        if self._scene_history:
            return self._scene_history.pop()
        return None

    @property
    def action_token(self):
        return self._action_token

    @action_token.setter
    def action_token(self, value):
        self._action_token = value

    def renew_action_token(self):
        self._action_token = ''.join(
            secrets.choice(string.ascii_letters) for _ in range(8))

    def reset(self):
        self.db = {}
        self._scene = None
        self._scene_history = []
        self.web_next_label = None
        self.web_next_trigger = None
        self.renew_action_token()

    def export(self):
        return {
            'scene': self._scene,
            'scene_history': copy.deepcopy(self._scene_history),
            'action_generation': self._action_token,
            'flags': {
                key: copy.deepcopy(value)
                for key, value in self.db.items()
                if not key.startswith('$_')
            },
            'web_next_label': self.web_next_label,
            'web_next_trigger': self.web_next_trigger,
        }

    def mark_saved(self):
        self._rollback = self.export()

    def rollback(self):
        restored = TokenPlayerStatus(
            self.bot_name, self.user_id, self._rollback)
        self.db = restored.db
        self._scene = restored._scene
        self._scene_history = restored._scene_history
        self._action_token = restored._action_token
        self.web_next_label = restored.web_next_label
        self.web_next_trigger = restored.web_next_trigger


class TokenNextLabelStore:
    """More継続情報をTokenPlayerStatus内で管理する。"""

    def set_next_label(self, status, label, trigger):
        previous = status.web_next_label
        status.web_next_label = label
        status.web_next_trigger = trigger
        return previous, None

    def get_next_label(self, status):
        return status.web_next_label, status.web_next_trigger

    def compare_and_clear_next_label(self, status, expected):
        if status.web_next_label != expected:
            return False, status.web_next_label
        status.web_next_label = None
        status.web_next_trigger = None
        return True, expected

    def clear_next_label(self, status):
        previous = status.web_next_label
        status.web_next_label = None
        status.web_next_trigger = None
        return previous
