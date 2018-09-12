# coding: utf-8

import commands
import utility
import datetime
import pytz
import logging

TIMESTAMP_OBJECT = (u'TimeStamp',)


class TimestampPlugin_RuntimeObject(object):
    def __init__(self, timezone, time_format):
        self.timezone = timezone
        self.time_format = time_format

    @property
    def now(self):
        now = datetime.datetime.now(tz=pytz.utc).astimezone(self.timezone)
        return now.strftime(self.time_format)

    @property
    def datetime(self):
        now = datetime.datetime.now(tz=pytz.utc).astimezone(self.timezone)
        return now.strftime('%Y/%m/%d %H:%M:%S')

    @property
    def date(self):
        now = datetime.datetime.now(tz=pytz.utc).astimezone(self.timezone)
        return now.strftime('%Y/%m/%d')

    @property
    def time(self):
        now = datetime.datetime.now(tz=pytz.utc).astimezone(self.timezone)
        return now.strftime('%H:%M:%S')


class TimestampPlugin_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.time_format = params.get('now_format', '%Y/%m/%d %H:%M:%S')
        tzname = params.get('timezone', 'UTC')
        try:
            self.timezone = pytz.timezone(tzname)
        except pytz.exceptions.UnknownTimeZoneError:
            logging.error(u'unknown timezone: {}'.format(tzname))
            self.timezone = pytz.utc
        self.runtime_object = TimestampPlugin_RuntimeObject(self.timezone, self.time_format)

    def get_runtime_object(self, _name, _context):
        return self.runtime_object


def load_plugin(params):
    runtime = TimestampPlugin_Runtime(params)
    commands.register_object(commands.ObjectEntry(
        names=TIMESTAMP_OBJECT,
        runtime=runtime,
        service='*'))
