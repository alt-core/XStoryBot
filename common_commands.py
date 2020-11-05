# coding: utf-8
import logging
import requests
import datetime
import pytz
import json

from google.appengine.api import taskqueue

import main
import auth
import hub
import commands
import users
from expression import Expression


IMAGE_CMDS = (u'@image', u'@画像')

OR_CMDS = (u'@or', u'@または')
RESET_CMDS = (u'@reset', u'@リセット')
SET_CMDS = (u'@set', u'@セット')
FORWARD_CMDS = (u'@forward', u'@転送')
DELAY_CMDS = (u'@delay', u'@遅延')

IF_CMDS = (u'@if', u'@条件')

SEQ_CMDS = (u'@seq', u'@順々')
RESET_NODES_CMDS = (u'@reset_nodes', u'@ノードリセット')
NEW_CHAPTER_CMDS = (u'@new_chapter', u'@新章')

GROUP_ADD_CMDS = (u'@group_add', u'@グループ追加')
GROUP_DEL_CMDS = (u'@group_del', u'@グループ削除')
GROUP_CLEAR_CMDS = (u'@group_clear', u'@グループ初期化')
WEBHOOK_CMDS = (u'@webhook', u'@WebHook')
LOG_CMDS = (u'@log', u'@Log')
ERROR_CMDS = (u'@error', u'@Error')

ALL_COMMON_CMDS = IMAGE_CMDS + OR_CMDS + RESET_CMDS + SET_CMDS + FORWARD_CMDS + DELAY_CMDS + IF_CMDS + SEQ_CMDS + RESET_NODES_CMDS + NEW_CHAPTER_CMDS + GROUP_ADD_CMDS + GROUP_DEL_CMDS + GROUP_CLEAR_CMDS + WEBHOOK_CMDS + LOG_CMDS + ERROR_CMDS

COMMON_OBJECT = (u'Core',)


def send_request(bot_name, user, action, delay_secs=None):
    params = {
        u'user': user.serialize(),
        u'action': action,
        u'token': auth.api_token
    }

    if delay_secs is None:
        task = taskqueue.add(url='/api/v1/bots/{}/action'.format(bot_name),
                             params=params)
    else:
        task = taskqueue.add(url='/api/v1/bots/{}/action'.format(bot_name),
                             params=params,
                             countdown=delay_secs)
    logging.info("enqueue a task: {}, ETA {}".format(task.name, task.eta))


class CommonCommands_Builder(object):
    def __init__(self, params):
        self.params = params

    def build_from_command(self, builder, sender, msg, options, children=[], grandchildren=[]):
        if msg not in ALL_COMMON_CMDS:
            builder.raise_error(u'内部エラー：未知のコマンドです')

        if msg in IMAGE_CMDS:
            # IMAGE_CMDS は scenario.py で直接対応する
            return False

        builder.add_command(sender, msg, options, children)

        # 解釈はここで終了
        return True


class CommonCommands_RuntimeObject(object):
    def __init__(self):
        self.context = None
        pass

    @property
    def uid(self):
        if self.context != None:
            return unicode(self.context.user)
        return u'None'

    @property
    def scene(self):
        if self.context != None:
            return unicode(self.context.status.scene)
        return u'None'


class CommonCommands_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.runtime_object = CommonCommands_RuntimeObject()
        self.lastContext = None
        self.reset_keyword = params['reset_keyword']
        self.timezone = pytz.timezone(params.get('timezone', 'utc'))

    def modify_incoming_action(self, context, action):
        if action == self.reset_keyword:
            # 強制リセットキーワードがアクションとして入ってきた場合は
            # プレイヤーの状態を初期化して処理を終了
            context.status.reset()
            context.add_reaction(None, u'リセットしました')
            return None
        return action

    def run_command(self, context, sender, msg, options, _children=[]):
        if msg in (IMAGE_CMDS + OR_CMDS + IF_CMDS + SEQ_CMDS):
            # 画像と制御系のコマンドは scenario.py 内で直接対応
            return False
        elif msg in RESET_CMDS:
            context.status.reset()
        elif msg in SET_CMDS:
            value = options[1]
            if isinstance(value, Expression):
                value = value.eval(context.env, context.env.matches)
            context.status[options[0]] = value
        elif msg in FORWARD_CMDS:
            bot_name = options[0]
            action = options[1]
            to_bot = main.get_bot(bot_name)
            if to_bot is None or to_bot.get_interface(context.service_name) is None:
                logging.error("invalid bot name: @forward :"+ bot_name)
                context.add_reaction(None, u"<<@forwardを解釈できませんでした>>")
                return True
            send_request(bot_name, context.user, action)
        elif msg in DELAY_CMDS:
            delay_secs = int(options[0])
            if len(options) > 2:
                bot_name = options[1]
                action = options[2]
            else:
                bot_name = context.bot_name
                action = options[1]
            to_bot = main.get_bot(bot_name)
            if to_bot is None or to_bot.get_interface(context.service_name) is None:
                logging.error("invalid bot name: @delay: " + bot_name)
                context.add_reaction(None, u"<<@delayを解釈できませんでした>>")
                return True
            send_request(bot_name, context.user, action, delay_secs)
        elif msg in RESET_NODES_CMDS:
            if len(options) > 0:
                target_name = options[0]
                del context.status[u'node.seq.' + target_name]
            else:
                for key in context.status.keys():
                    if key.startswith(u'node.seq.'):
                        del context.status[key]
        elif msg in NEW_CHAPTER_CMDS:
            for key in context.status.keys():
                if key.startswith(u'$$') or key.startswith(u'node.seq.'):
                    del context.status[key]
        elif msg in GROUP_ADD_CMDS:
            group_name = options[0]
            users.append_group_member(group_name, context.user)
        elif msg in GROUP_DEL_CMDS:
            group_name = options[0]
            users.remove_group_member(group_name, context.user)
        elif msg in GROUP_CLEAR_CMDS:
            group_name = options[0]
            users.clear_group(group_name)
        elif msg in WEBHOOK_CMDS:
            url = options[0]
            if len(options) > 1:
                # 残りのオプションを key:value の組と見なす
                data = dict(zip(options[1:-1:2], options[2::2]))
            else:
                data = None
            requests.post(url, data=data)
        elif msg in LOG_CMDS:
            category = options[0]
            if len(options) == 2:
                message = options[1]
            else:
                message = options[1:]
            timestamp = datetime.datetime.now(tz=self.timezone).strftime('%Y/%m/%d %H:%M:%S')
            scene_str = context.status.scene
            uid_str = unicode(context.user)
            action_str = context.action
            log = {
                "type": "XSBLog",
                "cat": category,
                "date": timestamp,
                "uid": uid_str,
                "log": message,
                "scene": scene_str,
                "action": action_str,
            }
            logging.info(json.dumps(log))
        elif msg in ERROR_CMDS:
            message = options[0]
            logging.error(message)
        else:
            logging.error(u'内部エラー：未知のコマンドです:' + msg)
            context.add_reaction(None, u"<<内部エラー：未知のコマンドです>>")
        return True

    def get_runtime_object(self, _name, context):
        self.runtime_object.context = context
        return self.runtime_object


def setup(params):
    builder = CommonCommands_Builder(params)
    runtime = CommonCommands_Runtime(params)
    hub.register_handler(
        service='*',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=IMAGE_CMDS,
            options='image',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=OR_CMDS,
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=RESET_CMDS,
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=FORWARD_CMDS,
            options='hankaku text|label',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=DELAY_CMDS,
            options='number text|label',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=SEQ_CMDS,
            # TODO: 可変長表現の追加
            options='label [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=RESET_NODES_CMDS,
            options='[hankaku]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=GROUP_ADD_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=GROUP_DEL_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=GROUP_CLEAR_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=WEBHOOK_CMDS,
            options='raw',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=LOG_CMDS,
            options='text text',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=ERROR_CMDS,
            options='text',
            builder=builder,
            runtime=runtime,
            service='*'),
    ])

    # version別
    # min_version の高いものから順番に
    commands.register_commands([
        commands.CommandEntry(
            names=IF_CMDS,
            options='expr label label',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=2),
        commands.CommandEntry(
            names=IF_CMDS,
            options='hankaku label label',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=1),
        commands.CommandEntry(
            names=SET_CMDS,
            options='variable expr',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=2),
        commands.CommandEntry(
            names=SET_CMDS,
            options='variable hankaku',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=1),
        commands.CommandEntry(
            names=NEW_CHAPTER_CMDS,
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=2),
    ])

    commands.register_object(commands.ObjectEntry(
        names=COMMON_OBJECT,
        runtime=runtime,
        service='*'))


