# coding: utf-8

from models import GroupMembersDB


class User(object):
    def __init__(self, service_name, user_id):
        self.service_name = str(service_name)
        self.user_id = str(user_id)

    def __str__(self):
        return self.serialize()

    def serialize(self):
        values = [self.service_name, self.user_id]
        values = [s.replace('%', '%25').replace(':', '%3A') for s in values]
        return ':'.join(values)

    @classmethod
    def deserialize(cls, string):
        values = str(string).split(':')
        if len(values) != 2:
            return None
        service_name, user_id = [s.replace('%3A', ':').replace('%25', '%') for s in values]
        return cls(service_name, user_id)


def get_group_members(group):
    members = GroupMembersDB.get_members(group)
    return [User.deserialize(member) for member in members]


def append_group_member(group, user):
    member = user.serialize()
    GroupMembersDB.append_member(group, member)


def remove_group_member(group, user):
    member = user.serialize()
    GroupMembersDB.remove_member(group, member)


def clear_group(group):
    GroupMembersDB.clear(group)


def get_all_groups():
    return GroupMembersDB.get_all_groups()
