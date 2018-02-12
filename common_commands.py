# coding: utf-8
import logging
import requests

from google.appengine.api import taskqueue

import main
import auth
import hub
import commands
import users


OR_CMDS = (u'@or', u'@または')
RESET_CMDS = (u'@reset', u'@リセット')
SET_CMDS = (u'@set', u'@セット')
FORWARD_CMDS = (u'@forward', u'@転送')
DELAY_CMDS = (u'@delay', u'@遅延')

IF_CMDS = (u'@if', u'@条件')

SEQ_CMDS = (u'@seq', u'@順々')
RESET_NODES_CMDS = (u'@reset_nodes', u'@ノードリセット')

GROUP_ADD_CMDS = (u'@group_add', u'@グループ追加')
GROUP_DEL_CMDS = (u'@group_del', u'@グループ削除')
GROUP_CLEAR_CMDS = (u'@group_clear', u'@グループ初期化')
WEBHOOK_CMDS = (u'@webhook', u'@WebHook')

ALL_COMMON_CMDS = OR_CMDS + RESET_CMDS + SET_CMDS + FORWARD_CMDS + DELAY_CMDS + IF_CMDS + SEQ_CMDS + RESET_NODES_CMDS + GROUP_ADD_CMDS + GROUP_DEL_CMDS + GROUP_CLEAR_CMDS + WEBHOOK_CMDS


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

    def build_from_command(self, builder, msg, options, children=[], grandchildren=[]):
        if msg not in ALL_COMMON_CMDS:
            builder.raise_error(u'内部エラー：未知のコマンドです')

        builder.add_command(msg, options, children)

        # 解釈はここで終了
        return True


class CommonCommands_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.reset_keyword = params['reset_keyword']

    def modify_incoming_action(self, context, action):
        if action == self.reset_keyword:
            # 強制リセットキーワードがアクションとして入ってきた場合は
            # プレイヤーの状態を初期化して処理を終了
            context.status.reset()
            context.add_reaction(u'リセットしました')
            return None
        return action

    def run_command(self, context, msg, options, _children=[]):
        if msg in (OR_CMDS + IF_CMDS + SEQ_CMDS):
            # 制御系のコマンドは scenario.py 内で直接対応
            return False
        elif msg in RESET_CMDS:
            context.status.reset()
        elif msg in SET_CMDS:
            context.status[options[0]] = options[1]
        elif msg in FORWARD_CMDS:
            bot_name = options[0]
            action = options[1]
            to_bot = main.get_bot(bot_name)
            if to_bot is None or to_bot.get_interface(context.service_name) is None:
                logging.error("invalid bot name: @forward :"+ bot_name)
                context.add_reaction(u"<<@forwardを解釈できませんでした>>")
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
                context.add_reaction(u"<<@delayを解釈できませんでした>>")
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
        else:
            logging.error(u'内部エラー：未知のコマンドです:' + msg)
            context.add_reaction(u"<<内部エラー：未知のコマンドです>>")
        return True


def setup(params):
    builder = CommonCommands_Builder(params)
    runtime = CommonCommands_Runtime(params)
    hub.register_handler(
        service='*',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            command=OR_CMDS,
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=RESET_CMDS,
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=SET_CMDS,
            options='variable expr',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=FORWARD_CMDS,
            options='hankaku text|label',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=DELAY_CMDS,
            options='number text|label',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=IF_CMDS,
            options='expr label label',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=SEQ_CMDS,
            # TODO: 可変長表現の追加
            options='label [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=RESET_NODES_CMDS,
            options='[hankaku]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=GROUP_ADD_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=GROUP_DEL_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=GROUP_CLEAR_CMDS,
            options='hankaku',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            command=WEBHOOK_CMDS,
            options='raw',
            builder=builder,
            runtime=runtime,
            service='*'),
    ])


