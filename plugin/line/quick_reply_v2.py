# coding: utf-8
import logging
import re

import hub
import commands
import utility
from plugin.line import default_commands
from utility import safe_list_get


SET_QUICK_REPLY_GUARD_CMDS = ['@@set_quick_reply_guard']
CLEAR_QUICK_REPLY_GUARD_CMDS = ['@clear_quick_reply_guard', '@clear_quick_reply_state']

QUICK_REPLY_GUARD_VARIABLE = '$$__line.quick_reply'


def append_quick_reply(builder, quick_reply_guard, replies, sender, retry_message):

    # リトライ時に戻ってこれるようにラベルを設定
    retry_label = quick_reply_guard + '0'
    builder.add_command(sender, retry_label, [], None)
    builder.add_new_string_block(retry_label)

    # QuickReplyの設定
    reply_labels = []
    reply_options = []
    reply_children = []

    for i, r in enumerate(replies):
        label = quick_reply_guard + str(i+1)

        # 文字列に'=>'が含まれているか判定し、含まれていたら前と後に分割
        if "=>" in r:
            r1, r2 = r.split("=>", 1)  # 最初の'=>'で分割
            choice = [r1, r2, label]
        else:
            choice = [r, label]
        reply_children.append(choice)
        reply_labels.append(label)

    builder.add_command(sender, default_commands.REPLY_CMDS[0], reply_options, reply_children)
    builder.add_command(sender, SET_QUICK_REPLY_GUARD_CMDS[0], [quick_reply_guard], None)

    # その他の入力が来た時用のラベルを設定

    label = quick_reply_guard + 'R'
    builder.add_new_string_block(label)
    retry_message_sender, retry_message = utility.parse_sender(retry_message)
    if retry_message_sender is None:
        retry_message_sender = sender
    builder.add_command(retry_message_sender, retry_message, [], None)
    builder.add_command(sender, retry_label, [], None)

    # 各選択肢用のラベルを設定

    n_replies = len(replies)

    for i in range(n_replies):
        label = reply_labels[i]
        builder.add_new_string_block(label)
        builder.add_command(sender, CLEAR_QUICK_REPLY_GUARD_CMDS[0], [], None)
        if n_replies > 1:
            next_label = '##' + str(i+1)
            builder.add_command(sender, next_label, [], None)
        else:
            # 1つしか選択肢がなければ、そのままフォールスルーすればよい
            pass

    if n_replies > 1:
        # 省略されている '##' を足す
        builder.add_new_anonymous_block()


class LineQuickReplyPlugin_Builder(object):
    def __init__(self, params):
        self.command = params['command']
        self.default_reply = params['default_reply']
        self.retry_message = params['retry_message']

    def filter_plain_text(self, builder, msg, options, sender):

        if msg in self.command:
            if len(options) > 0:
                replies = options
            else:
                replies = [self.default_reply]

            quick_reply_guard = '##QREP__{}_'.format(builder.scene.get_relative_position_desc(builder.node))

            append_quick_reply(builder, quick_reply_guard, replies, sender, self.retry_message)

            # 解釈はここで終了
            return None

        # 解釈は継続
        return msg


class LineQuickReplyPlugin_Runtime(object):
    def __init__(self, params):
        self.default_reply = params['default_reply']
        self.retry_message = params['retry_message']
        self.ignore_pattern_re = None
        if 'ignore_pattern' in params and params['ignore_pattern']:
            self.ignore_pattern_re = re.compile(params['ignore_pattern'])

    def run_command(self, context, sender, msg, options):
        if msg in SET_QUICK_REPLY_GUARD_CMDS:
            context.status[QUICK_REPLY_GUARD_VARIABLE] = options[0]

            # 解釈はここで終了
            return True

        elif msg in CLEAR_QUICK_REPLY_GUARD_CMDS:
            if QUICK_REPLY_GUARD_VARIABLE in context.status:
                del context.status[QUICK_REPLY_GUARD_VARIABLE]

            # 解釈はここで終了
            return True

        # 解釈は継続
        return False

    def modify_incoming_action(self, context, action):
        quick_reply_guard = context.status.get(QUICK_REPLY_GUARD_VARIABLE, None)
        if quick_reply_guard is None:
            # quick_reply_guard が設定されていなかったらスルー
            return action
        else:
            if action in ['##line.follow', '##line.join']:
                # 変な状態でハマった時の復旧用に ##follow と ##join で状態リセット
                logging.info('LineQuickReplyPlugin_Runtime: reset next_label: {} {}'.format(quick_reply_guard, action))
                del context.status[QUICK_REPLY_GUARD_VARIABLE]
                return action
            elif action.startswith(quick_reply_guard):
                # 次に進む入力が来た
                # ここでquick_reply_guardを消すと、statusの保存のレースコンディションで進行状態との不整合が発生する可能性があるので、シナリオ内の clear コマンドに任せる
                # del context.status[QUICK_REPLY_GUARD_VARIABLE]
                return action
            elif self.ignore_pattern_re and self.ignore_pattern_re.search(action):
                # そのまま通すパターン
                return action
            else:
                # 知らない入力が来たので、もう一度選択肢を出す
                return quick_reply_guard + 'R'


def load_plugin(params):
    builder = LineQuickReplyPlugin_Builder(params)
    runtime = LineQuickReplyPlugin_Runtime(params)
    hub.register_handler(
        service='line',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=SET_QUICK_REPLY_GUARD_CMDS,
            options='label',
            runtime=runtime,
            service='line'),
        commands.CommandEntry(
            names=CLEAR_QUICK_REPLY_GUARD_CMDS,
            builder=commands.Default_Builder(),
            runtime=runtime,
            service='line'),
    ])
