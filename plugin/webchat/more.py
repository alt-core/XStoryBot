import logging
import re

import commands
import hub
from utility import safe_list_get


SET_NEXT_LABEL_CMD = '@@set_next_label'
CLEAR_NEXT_LABEL_CMDS = ('@clear_next_label', '@reset_next_label')


class WebchatMoreRuntime:
    """More継続情報を署名state内だけで処理する。"""

    def __init__(self, params):
        self.action_pattern_re = None
        if params.get('action_pattern'):
            self.action_pattern_re = re.compile(params['action_pattern'])
        self.ignore_pattern_re = None
        if params.get('ignore_pattern'):
            self.ignore_pattern_re = re.compile(params['ignore_pattern'])
        self.please_push_more_button_label = (
            params['please_push_more_button_label'])

    @staticmethod
    def _store(context):
        return context.next_label_store

    def run_command(self, context, sender, msg, options):
        store = self._store(context)
        if msg == SET_NEXT_LABEL_CMD:
            overwrite_label, _overwrite_trigger = store.set_next_label(
                context.status,
                options[0],
                safe_list_get(options, 1, None),
            )
            if overwrite_label:
                logging.warning(
                    'exec set next label command: %s overwrites %s',
                    options[0], overwrite_label)
            else:
                logging.debug(
                    'exec set next label command: %s', options[0])
            return True
        if msg in CLEAR_NEXT_LABEL_CMDS:
            store.clear_next_label(context.status)
            logging.debug('exec reset next label command')
            return True
        return False

    def modify_incoming_action(self, context, action):
        store = self._store(context)
        for _retry in range(10):
            next_label, trigger_message = store.get_next_label(context.status)
            if next_label is None:
                return action
            if action in ('##line.follow', '##line.join'):
                logging.warning(
                    'WebchatMoreRuntime: reset next_label: %s %s',
                    next_label, action)
                store.clear_next_label(context.status)
                return action
            if (
                    action == trigger_message
                    or (
                        self.action_pattern_re
                        and self.action_pattern_re.search(action)
                    )):
                if store.compare_and_clear_next_label(
                        context.status, next_label)[0]:
                    return next_label
                continue
            if self.ignore_pattern_re and self.ignore_pattern_re.search(action):
                return action
            return self.please_push_more_button_label

        logging.error(
            'WebchatMoreRuntime: retry limit exceeds: %s', action)
        return action


def load_plugin(params):
    runtime = WebchatMoreRuntime(params)
    hub.register_handler(service='webchat', runtime=runtime)
    commands.register_commands([
        commands.CommandEntry(
            names=[SET_NEXT_LABEL_CMD],
            options='label [text]',
            runtime=runtime,
            service='webchat'),
        commands.CommandEntry(
            names=CLEAR_NEXT_LABEL_CMDS,
            runtime=runtime,
            service='webchat'),
    ])
