# coding: utf-8
import logging
import re
import json

import hub
import commands
import utility
from plugin.line.command_names import REPLY_CMDS
from utility import safe_list_get


SET_QUICK_REPLY_GUARD_CMDS = ['@@set_quick_reply_guard']
CLEAR_QUICK_REPLY_GUARD_CMDS = ['@clear_quick_reply_guard']
SHOW_QUICK_REPLY_CHOICES_CMDS = ['@show_quick_reply_choices']

QUICK_REPLY_GUARD_VARIABLE = '$$line.quick_reply'


def append_quick_reply(builder, quick_reply_base_label, choices, sender, please_select_quick_retry_label, guard_flag=True):

    # リトライ時に戻ってこれるようにラベルを設定
    retry_label = quick_reply_base_label + '0'
    builder.add_command(sender, retry_label, [], None)
    builder.add_new_string_block(retry_label)

    # QuickReplyの設定
    reply_labels = []
    reply_options = []
    reply_children = []

    for i, r in enumerate(choices):
        label = quick_reply_base_label + str(i+1)

        # 文字列に'=>'が含まれているか判定し、含まれていたら前と後に分割
        if "=>" in r:
            r1, r2 = r.split("=>", 1)  # 最初の'=>'で分割
            choice = [r1, r2, label]
        else:
            choice = [r, label]
        reply_children.append(choice)
        reply_labels.append(label)

    builder.add_command(sender, REPLY_CMDS[0], reply_options, reply_children)

    builder.add_command(sender, SET_QUICK_REPLY_GUARD_CMDS[0], [quick_reply_base_label, please_select_quick_retry_label, json.dumps(reply_children), str(guard_flag) ], None)

    # 各選択肢用のラベルを設定

    n_choices = len(choices)

    if n_choices > 1:
        builder.start_control_flow('quick_reply')

    for i in range(n_choices):
        label = reply_labels[i]
        builder.add_new_string_block(label)
        builder.add_command(sender, CLEAR_QUICK_REPLY_GUARD_CMDS[0], [quick_reply_base_label], None)
        if n_choices > 1:
            next_label = builder.make_control_flow_refernce_label(i)
            builder.add_command(sender, next_label, [], None)
        else:
            # 1つしか選択肢がなければ、そのままフォールスルーすればよい
            pass

    if n_choices > 1:
        builder.add_new_control_flow_block()


class LineQuickReplyPlugin_Builder(object):
    def __init__(self, params):
        self.command = params['command']
        self.command_without_guard = params.get('command_without_guard', [])
        self.default_reply = params['default_reply']
        self.please_select_quick_reply_label = params['please_select_quick_reply_label']

    def filter_plain_text(self, builder, msg, options, sender):

        if msg in self.command:
            if len(options) > 0:
                choices = options
            else:
                choices = [self.default_reply]

            quick_reply_base_label = '##QREP__{}_'.format(builder.scene.get_relative_position_desc(builder.node))

            append_quick_reply(builder, quick_reply_base_label, choices, sender, self.please_select_quick_reply_label, guard_flag=True)

            # 解釈はここで終了
            return None

        elif msg in self.command_without_guard:
            if len(options) > 0:
                choices = options
            else:
                choices = [self.default_reply]

            quick_reply_base_label = '##QREP__{}_'.format(builder.scene.get_relative_position_desc(builder.node))

            append_quick_reply(builder, quick_reply_base_label, choices, sender, self.please_select_quick_reply_label, guard_flag=False)

            # 解釈はここで終了
            return None

        # 解釈は継続
        return msg


class LineQuickReplyPlugin_Runtime(object):
    def __init__(self, params):
        self.default_reply = params['default_reply']
        self.ignore_pattern_re = None
        if 'ignore_pattern' in params and params['ignore_pattern']:
            self.ignore_pattern_re = re.compile(params['ignore_pattern'])
        self.please_select_quick_reply_label = params['please_select_quick_reply_label']

    def run_command(self, context, sender, msg, options):
        if msg in SET_QUICK_REPLY_GUARD_CMDS:
            quick_reply_guard = context.status.get(QUICK_REPLY_GUARD_VARIABLE, None)
            if quick_reply_guard is None or not isinstance(quick_reply_guard, dict) or quick_reply_guard["label"] != options[0]:
                retry_count = 0
            else:
                # 同じ base_label で複数回来たら、カウンタを引き継ぐ
                retry_count = quick_reply_guard["retry_count"]
            guard = options[3] == "True" if len(options) > 3 else True

            context.status[QUICK_REPLY_GUARD_VARIABLE] = {
                "label": options[0],
                "please_label": options[1],
                "choices": options[2],
                "guard": guard,
                "retry_count": retry_count,
                "action": "",
            }

            # 解釈はここで終了
            return True

        elif msg in SHOW_QUICK_REPLY_CHOICES_CMDS:
            quick_reply_guard = context.status.get(QUICK_REPLY_GUARD_VARIABLE, None)
            if quick_reply_guard is None or not isinstance(quick_reply_guard, dict):
                # quick_reply_guard が設定されていなかったらスルー
                return True
            else:
                # 選択肢表示のラベルへ戻る
                return quick_reply_guard["label"] + '0'

        elif msg in CLEAR_QUICK_REPLY_GUARD_CMDS:
            quick_reply_guard = context.status.get(QUICK_REPLY_GUARD_VARIABLE, None)
            if quick_reply_guard is None or not isinstance(quick_reply_guard, dict):
                # quick_reply_guard が設定されていなかったらスルー
                return True
            else:
                label = quick_reply_guard["label"]
                if len(options) == 0 or label == options[0]:
                    del context.status[QUICK_REPLY_GUARD_VARIABLE]
                else:
                    logging.warning("quick_reply_guard label mismatch: {} != {}".format(label, options[0]))

            # 解釈はここで終了
            return True

        # 解釈は継続
        return False

    def modify_incoming_action(self, context, action):
        quick_reply_guard = context.status.get(QUICK_REPLY_GUARD_VARIABLE, None)
        if quick_reply_guard is None or not isinstance(quick_reply_guard, dict):
            # quick_reply_guard が設定されていなかったらスルー
            return action
        else:
            please_label = quick_reply_guard["please_label"]
            guard_flag = quick_reply_guard.get("guard", True)
            choices = json.loads(quick_reply_guard["choices"])
            if action in ['##line.follow', '##line.join']:
                # 変な状態でハマった時の復旧用に ##follow と ##join で状態リセット
                logging.info('LineQuickReplyPlugin_Runtime: reset next_label: {} {}'.format(quick_reply_guard, action))
                del context.status[QUICK_REPLY_GUARD_VARIABLE]
                return action
            elif action.startswith(quick_reply_guard["label"]):
                # 次に進む入力が来た
                # ここでquick_reply_guardを消すと、statusの保存のレースコンディションで進行状態との不整合が発生する可能性があるので、シナリオ内の clear コマンドに任せる
                # del context.status[QUICK_REPLY_GUARD_VARIABLE]
                return action
            elif self.ignore_pattern_re and self.ignore_pattern_re.search(action):
                # そのまま通すパターン
                return action
            else:
                # 選択肢の入力が自由文で来たかを完全一致で確認
                #print(f'QREP> action: {action}')
                for choice in choices:
                    if action == choice[0]:
                        #print(f'QREP> matched: {action} -> {choice[-1]}')
                        return choice[-1]
                # 知らない入力が来たので、再入力用のラベルにジャンプ
                # 飛んだ先で @show_quick_reply_choices が実行される前提

                if guard_flag:
                    # カウンタの更新
                    context.status[QUICK_REPLY_GUARD_VARIABLE] = {
                        "label": quick_reply_guard["label"],
                        "please_label": quick_reply_guard["please_label"],
                        "choices": quick_reply_guard["choices"],
                        "guard": quick_reply_guard["guard"],
                        "retry_count": quick_reply_guard["retry_count"] + 1,
                        "action": action,
                    }
                    #print(f'QREP> -> {please_label}, count: {quick_reply_guard["retry_count"]}')

                    return please_label
                else:
                    # ガードしない設定
                    del context.status[QUICK_REPLY_GUARD_VARIABLE]
                    return action


def load_plugin(params):
    builder = LineQuickReplyPlugin_Builder(params)
    runtime = LineQuickReplyPlugin_Runtime(params)
    hub.register_handler(
        service='line',
        builder=builder,
        runtime=runtime)
    hub.register_handler(service='webchat', runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=SET_QUICK_REPLY_GUARD_CMDS,
            options='label label text text',
            runtime=runtime,
            service='line'),
        commands.CommandEntry(
            names=SHOW_QUICK_REPLY_CHOICES_CMDS,
            builder=commands.Default_Builder(),
            runtime=runtime,
            service='line'),
        commands.CommandEntry(
            names=CLEAR_QUICK_REPLY_GUARD_CMDS,
            options='[label]',
            builder=commands.Default_Builder(),
            runtime=runtime,
            service='line'),
    ])
    commands.register_commands([
        commands.CommandEntry(
            names=SET_QUICK_REPLY_GUARD_CMDS,
            options='label label text text',
            runtime=runtime,
            service='webchat'),
        commands.CommandEntry(
            names=SHOW_QUICK_REPLY_CHOICES_CMDS,
            runtime=runtime,
            service='webchat'),
        commands.CommandEntry(
            names=CLEAR_QUICK_REPLY_GUARD_CMDS,
            options='[label]',
            runtime=runtime,
            service='webchat'),
    ])
