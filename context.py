# coding: utf-8

from models import PlayerStatusDB
from utility import safe_list_get


class ActionContext(object):
    class RuntimeEnvironment(dict):
        def __init__(self, context):
            self.context = context
            self.matches = []

        def __getitem__(self, key):
            for d in self.context.env_dicts:
                if key in d:
                    return d[key]
            if key in self.context.status:
                return self.context.status[key]
            return 0 # default

        def __contains__(self, key):
            for d in self.context.env_dicts:
                if key in d:
                    return True
            return key in self.context.status

        def set_matches(self, matches):
            self.matches = matches

        def clear_matches(self):
            self.matches = []

        def get_match(self, index, default=None):
            return safe_list_get(self.matches, index, default)

        def get(self, key, default=None):
            if key in self:
                return self[key]
            else:
                return default


    def __init__(self, bot_name, service_name, interface, user, action, attrs):
        self.bot_name = bot_name
        self.service_name = service_name
        self.user = user
        self.action = action
        self.current_action = action
        self.attrs = attrs
        self.interfaces = {}
        self.status = None
        self.env_dicts = []
        self.reactions = None # interface に中立なフォーマット
        self.response = None # interface 毎に異なるフォーマット
        if service_name is not None and interface is not None:
            self.add_interface(service_name, interface)
        self.env = ActionContext.RuntimeEnvironment(self)
        self.version = None

    def add_interface(self, service_name, interface):
        self.interfaces[service_name] = interface

    def get_interface(self, service_name):
        return self.interfaces.get(service_name, None)

    def load_status(self):
        user_id = self.user.serialize()
        self.status = PlayerStatusDB(user_id)

    def save_status(self):
        self.status.save()

    def add_reaction(self, sender, msg, options=None, children=None):
        row = [sender, msg]
        if options:
            row.extend(options)
        self.reactions.append((row, children))

    def add_env(self, env):
        if env not in self.env_dicts:
            self.env_dicts.append(env)
