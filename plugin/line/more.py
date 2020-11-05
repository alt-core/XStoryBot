# coding: utf-8
import logging
import re

from google.appengine.ext import ndb

import hub
import commands
from plugin.line import default_commands
from utility import safe_list_get


SET_NEXT_LABEL_CMD = u'@@set_next_label'
CLEAR_NEXT_LABEL_CMDS = (u'@clear_next_label', u'@reset_next_label')


class PlayerNextLabel(ndb.Model):
    next_label = ndb.StringProperty()
    trigger_message = ndb.StringProperty()


class PlayerNextLabelDB(object):
    # next_label は競合すると先に進めなくなるため、独立させ、トランザクションで囲う
    @ndb.transactional
    def set_next_label(self, label, trigger_message, status):
        entry_next_label = PlayerNextLabel.get_by_id(status.id)
        overwrite = (None, None)
        if not entry_next_label:
            entry_next_label = PlayerNextLabel(id=status.id, next_label=label, trigger_message=trigger_message)
        else:
            if entry_next_label.next_label:
                overwrite = (entry_next_label.next_label, entry_next_label.trigger_message)
            entry_next_label.next_label = label
            entry_next_label.trigger_message = trigger_message
        entry_next_label.put()
        return overwrite

    @ndb.transactional
    def get_next_label(self, status):
        entry_next_label = PlayerNextLabel.get_by_id(status.id)
        if entry_next_label is None:
            return (None, None)
        return (entry_next_label.next_label, entry_next_label.trigger_message)

    @ndb.transactional
    def compare_and_clear_next_label(self, status, next_label):
        entry_next_label = PlayerNextLabel.get_by_id(status.id)
        if entry_next_label is None:
            return (None, None)
        ret = (entry_next_label.next_label, entry_next_label.trigger_message)
        if ret[0] == next_label:
            entry_next_label.next_label = None
            entry_next_label.trigger_message = None
            entry_next_label.put()
            return ret
        return (None, None)

    @ndb.transactional
    def clear_next_label(self, status):
        entry_next_label = PlayerNextLabel.get_by_id(status.id)
        if entry_next_label:
            entry_next_label.next_label = None
            entry_next_label.trigger_message = None
            entry_next_label.put()


class LineMorePlugin_Builder(object):
    def __init__(self, params):
        self.command = params['command']
        self.message = params['message']
        self.image_url = params['image_url']

    def filter_plain_text(self, builder, msg, _options, sender):

        if msg in self.command:
            if builder.i_node == len(builder.parent_node.children) - 1:
                builder.raise_error(u'入力待ちの後に表示するメッセージがありません')
            # 読み進めるボタンを表示するための特殊なメッセージ
            filepath, size = builder.build_image_for_imagemap_command(self.image_url)
            builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [unicode(filepath), unicode(size[0]), unicode(size[1])], [[u'0,0,{},{}'.format(size[0],size[1]), self.message]])

            next_label = u'##MORE__{}'.format(builder.scene.get_relative_position_desc(builder.node))
            builder.add_command(sender, SET_NEXT_LABEL_CMD, [next_label, self.message], None)
            #logging.info(u'insert set next label cmd: {}'.format(next_label))

            builder.add_new_string_index(next_label)

            # 解釈はここで終了
            return None

        # 解釈は継続
        return msg


class LineMorePlugin_Runtime(object):
    def __init__(self, params):
        self.message = params['message']
        self.action_pattern_re = None
        if 'action_pattern' in params and params['action_pattern']:
            self.action_pattern_re = re.compile(params['action_pattern'])
        self.ignore_pattern_re = None
        if 'ignore_pattern' in params and params['ignore_pattern']:
            self.ignore_pattern_re = re.compile(params['ignore_pattern'])
        self.please_push_more_button_label = params['please_push_more_button_label']

    def run_command(self, context, sender, msg, options):
        if msg == SET_NEXT_LABEL_CMD:
            overwrite_label, overwrite_trigger = PlayerNextLabelDB().set_next_label(options[0], safe_list_get(options, 1, None), context.status)
            if overwrite_label:
                logging.warning(u'exec set next label command: {} overwrites {}'.format(options[0], overwrite_label))
            else:
                logging.debug(u'exec set next label command: {}'.format(options[0]))

            # 解釈はここで終了
            return True

        elif msg in CLEAR_NEXT_LABEL_CMDS:
            PlayerNextLabelDB().clear_next_label(context.status)
            logging.debug(u'exec reset next label command')

            # 解釈はここで終了
            return True

        # 解釈は継続
        return False

    def modify_incoming_action(self, context, action):
        db = PlayerNextLabelDB()
        for retry in range(10):
            next_label, trigger_message = db.get_next_label(context.status)
            if next_label is None:
                # next_label が設定されていなかったらスルー
                return action
            else:
                if action in [u'##line.follow', u'##line.join']:
                    # 変な状態でハマった時の復旧用に ##follow と ##join で状態リセット
                    logging.warning(u'LineMorePlugin_Runtime: reset next_label: {} {}'.format(next_label, action))
                    db.clear_next_label(context.status)
                    return action
                elif action == trigger_message or (self.action_pattern_re and self.action_pattern_re.search(action)):
                    # 「続きを読む」ボタンが押されるなど、次に進む入力が来た
                    if db.compare_and_clear_next_label(context.status, next_label)[0]:
                        # クリアに成功したので next_label に入力を差し替える
                        return next_label
                    else:
                        # 何か競合状態で悪いことが起こったので最初から
                        continue
                elif self.ignore_pattern_re and self.ignore_pattern_re.search(action):
                    # そのまま通すパターン
                    return action
                else:
                    # next_label 設定時に他の入力がやってきたときは、more button を押すように促す
                    return self.please_push_more_button_label

        logging.error(u'LineMorePlugin_Runtime: retry limit exceeds: {}'.format(action))
        return action


def load_plugin(params):
    builder = LineMorePlugin_Builder(params)
    runtime = LineMorePlugin_Runtime(params)
    hub.register_handler(
        service='line',
        builder=builder,
        runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=[SET_NEXT_LABEL_CMD],
            options='label [text]',
            runtime=runtime,
            service='line'),
        commands.CommandEntry(
            names=CLEAR_NEXT_LABEL_CMDS,
            builder=commands.Default_Builder(),
            runtime=runtime,
            service='line'),
    ])
