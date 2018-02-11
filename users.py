# coding: utf-8

from models import GroupMembers, GroupDB


class User(object):
    def __init__(self, service_name, user_id):
        self.service_name = unicode(service_name)
        self.user_id = unicode(user_id)

    def __unicode__(self):
        return self.serialize()

    def __str__(self):
        return unicode(self).encode('utf-8')

    def serialize(self):
        values = [self.service_name, self.user_id]
        values = [s.replace(u'%', u'%25').replace(u':', u'%3A') for s in values]
        return u':'.join(values)

    @classmethod
    def deserialize(cls, string):
        values = unicode(string).split(u':')
        if len(values) != 2:
            return None
        service_name, user_id = [s.replace(u'%3A', u':').replace(u'%25', u'%') for s in values]
        return cls(service_name, user_id)


def get_group_members(group):
    group_members = GroupMembers.get_by_id(id=group)
    if group_members:
        members = group_members.members
    else:
        members = []
    return [User.deserialize(member) for member in members]


def append_group_member(group, user):
    group = GroupDB(group)
    member = user.serialize()
    group.append_member(member)


def remove_group_member(group, user):
    group = GroupDB(group)
    member = user.serialize()
    group.remove_member(member)


def clear_group(group):
    group = GroupDB(group)
    group.clear()
