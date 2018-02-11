# coding: utf-8

from models import PlayerStatusDB


class ActionContext(object):
    def __init__(self, bot_name, service_name, interface, user, action):
        self.bot_name = bot_name
        self.service_name = service_name
        self.user = user
        self.action = action
        self.current_action = action
        self.interfaces = {}
        self.status = None
        self.reactions = None # interface に中立なフォーマット
        self.response = None # interface 毎に異なるフォーマット
        if service_name is not None and interface is not None:
            self.add_interface(service_name, interface)

    def add_interface(self, service_name, interface):
        self.interfaces[service_name] = interface

    def get_interface(self, service_name):
        return self.interfaces.get(service_name, None)

    def load_status(self):
        user_id = self.user.serialize()
        self.status = PlayerStatusDB(user_id)

    def save_status(self):
        self.status.save()

    def add_reaction(self, msg, options=None, children=None):
        row = [msg]
        if options:
            row.extend(options)
        self.reactions.append((row, children))
