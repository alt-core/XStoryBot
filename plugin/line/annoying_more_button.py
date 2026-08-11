# coding: utf-8
import re

import commands
from plugin.line import default_commands


MORE_BUTTON_CMDS = ('@morebutton', '@続きを読む')


class LineAnnoyingMoreButtonPlugin_Builder(object):
    def build_from_command(self, builder, sender, msg, options):
        i = 0
        first_flag = True
        while i < len(builder.node.children):
            child = builder.node.children[i]
            i += 1
            button_title = None
            flag_dialog_mode = False
            if len(child.term) == 1 and re.search('[:：]$', child.term[0]) and i < len(builder.node.children):
                # タイトル設定
                button_title = child.term[0][0:-1]
                child = builder.node.children[i]
                i += 1
                flag_dialog_mode = True

            if first_flag:
                first_flag = False
            else:
                line_label = '##MORE__{}'.format(child.line_no)

                builder.add_new_string_block(line_label)

            if flag_dialog_mode:
                # 台詞モードでは次の話者指定までメッセージを連結する
                msg = child.term[0]
                while i < len(builder.node.children):
                    child = builder.node.children[i]
                    if len(child.term) == 1 and re.search('[:：]$', child.term[0]):
                        break
                    msg += "\n" + child.term[0]
                    i += 1

                next_label = None
                if i < len(builder.node.children):
                    next_label = '##0'
                else:
                    if len(options) > 0:
                        next_label = options[0]
                    else:
                        # 最後に飛ぶ先の引数指定がない場合は、最終行は通常メッセージとして表示される
                        pass
                if next_label is None:
                    builder.assert_strlen(msg, 300)
                    builder.add_command(sender, msg, None, None)
                else:
                    if button_title is not None:
                        builder.assert_strlen(msg, 60)
                        builder.add_command(sender, default_commands.BUTTON_CMDS[0], [msg, button_title], [['▽', next_label]])
                    else:
                        builder.assert_strlen(msg, 160)
                        builder.add_command(sender, default_commands.BUTTON_CMDS[0], [msg,], [['▽', next_label]])
            else:
                for j, msg in enumerate(child.term):
                    builder.msg_count += 1
                    if builder.msg_count > 5:
                        builder.raise_error('6つ以上のメッセージを同時に送ろうとしました')
                    next_label = None
                    if j == len(child.term)-1:
                        # 各行の最後のメッセージは「続きを読む」のボタン
                        if i < len(builder.node.children):
                            next_label = '##0'
                        else:
                            if len(options) > 0:
                                next_label = options[0]
                            else:
                                # 最後に飛ぶ先の引数指定がない場合は、最終行は通常メッセージとして表示される
                                pass
                    if next_label is None:
                        builder.assert_strlen(msg, 300)
                        builder.add_command(sender, msg, None, None)
                    else:
                        builder.assert_strlen(msg, 160)
                        builder.add_command(sender, default_commands.BUTTON_CMDS[0], [msg,], [['▽', next_label]])

        # 解釈はここで終了
        return True


def load_plugin(param):
    commands.register_command(commands.CommandEntry(
        names=MORE_BUTTON_CMDS,
        options='[label]',
        builder=LineAnnoyingMoreButtonPlugin_Builder(),
        service='line',
        specs={'children_min': 0}))
