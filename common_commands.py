# coding: utf-8
import logging
import requests
import datetime
import pytz
import json

import task_client

import main
import hub
import commands
import users
from expression import Expression


IMAGE_CMDS = ('@image', '@画像')
VIDEO_CMDS = ('@video', '@動画')

RAWIMAGE_CMDS = ('@rawimage', '@生画像')

OR_CMDS = ('@or', '@または')
RESET_CMDS = ('@reset', '@リセット')
SET_CMDS = ('@set', '@セット')
FORWARD_CMDS = ('@forward', '@転送')
DELAY_CMDS = ('@delay', '@遅延')

IF_CMDS = ('@if', '@条件')
ELSE_CMDS = ('@else', '@さもなくば')
ELIF_CMDS = ('@elif', '@あるいは')
END_CMDS = ('@end', '@終わり')

SEQ_CMDS = ('@seq', '@順々')
LOOP_CMDS = ('@loop', '@ループ')
RESET_NODES_CMDS = ('@reset_nodes', '@ノードリセット')
NEW_CHAPTER_CMDS = ('@new_chapter', '@新章')

RANDOM_CMDS = ('@random', '@ランダム')

CALL_CMDS = ('@call', '@サブルーチン')
RETURN_CMDS = ('@return', '@リターン')

DEFER_CMDS = ('@defer', '@遅延実行')

GROUP_ADD_CMDS = ('@group_add', '@グループ追加')
GROUP_DEL_CMDS = ('@group_del', '@グループ削除')
GROUP_CLEAR_CMDS = ('@group_clear', '@グループ初期化')
WEBHOOK_CMDS = ('@webhook', '@WebHook')
POSTJSON_CMDS = ('@postjson', '@PostJSON')
GETJSON_CMDS = ('@getjson', '@GetJSON')
LOG_CMDS = ('@log', '@Log')
ERROR_CMDS = ('@error', '@Error')

RAISE_CMDS = ('@raise', '@例外')

ALL_COMMON_CMDS = IMAGE_CMDS + VIDEO_CMDS + RAWIMAGE_CMDS + OR_CMDS + RESET_CMDS + SET_CMDS + FORWARD_CMDS + DELAY_CMDS + IF_CMDS + SEQ_CMDS + LOOP_CMDS + RANDOM_CMDS + CALL_CMDS + RETURN_CMDS + RESET_NODES_CMDS + NEW_CHAPTER_CMDS + GROUP_ADD_CMDS + GROUP_DEL_CMDS + GROUP_CLEAR_CMDS + WEBHOOK_CMDS + LOG_CMDS + ERROR_CMDS + RAISE_CMDS + ELSE_CMDS + ELIF_CMDS + END_CMDS + DEFER_CMDS + POSTJSON_CMDS + GETJSON_CMDS

COMMON_OBJECT = ('core',)

POSTJSON_RESULT_VARIABLE = '$_result'
POSTJSON_RESPONSE_VARIABLE = '$_response'


def send_request(bot_name, user, action, delay_secs=None):
    params = {
        'user': user.serialize(),
        'action': action,
    }

    task = task_client.create_task(
        queue_name='action-queue',
        url=f'/api/v1/bots/{bot_name}/action',
        params=params,
        delay_seconds=delay_secs
    )
    logging.info(f"enqueue a task: {task}")


class CommonCommands_Builder(object):
    def __init__(self, params):
        self.params = params

    def build_from_command(self, builder, sender, msg, options, children=[], grandchildren=[]):
        if msg not in ALL_COMMON_CMDS:
            builder.raise_error('内部エラー：未知のコマンドです')

        if msg in IMAGE_CMDS:
            # 画像
            if len(options) < 1:
                builder.raise_error('@imageには引数が1つ必要です')
            s = options[0]
            orig_url = builder.parse_imageurl(s)
            if orig_url is None:
                orig_url = s
            if orig_url is None or not orig_url.startswith('http'):
                builder.raise_error('@imageの第一引数は画像のURLである必要があります')
            image_url, _ = builder.build_image_for_image_command(orig_url)
            builder.add_command(sender, IMAGE_CMDS[0], [image_url,], None)
            return True

        if msg in VIDEO_CMDS:
            # 動画
            if len(options) < 2:
                builder.raise_error('@videoには引数が2つ以上必要です')
            s = options[0]
            orig_thumb_url = builder.parse_imageurl(s)
            if orig_thumb_url is None:
                orig_thumb_url = s
            if orig_thumb_url is None or not orig_thumb_url.startswith('http'):
                builder.raise_error('@videoの第一引数はサムネイル画像のURLである必要があります')
            orig_video_url = options[1]
            if orig_video_url is None or not orig_video_url.startswith('http'):
                builder.raise_error('@videoの第二引数は動画のURLである必要があります')
            thumb_url, _ = builder.build_image_for_image_command(orig_thumb_url)
            video_url = builder.build_video(orig_video_url)
            command_options = [thumb_url, video_url]
            if len(options) > 2:
                # 動画視聴完了後のアクション
                video_action = options[2]
                command_options.append(video_action)
            builder.add_command(sender, VIDEO_CMDS[0], command_options, None)
            return True

        if msg in IF_CMDS:
            if builder.version >= 3 and len(options) < 3:
                if len(options) == 2:
                    builder.raise_error('条件分岐の分岐先は0個か2個指定してください')
                # v3 スタイルの if 文は、コントロールフローを利用する
                builder.start_control_flow('if')
                options.append(builder.make_control_flow_refernce_label(0))
                options.append(builder.make_control_flow_refernce_label(1))
                builder.add_command(sender, msg, options, children)
                builder.add_new_control_flow_block()
                return True

        elif msg in (SEQ_CMDS + LOOP_CMDS + RANDOM_CMDS):
            if builder.version >= 3 and len(options) == 0:
                # v3 スタイルの seq, loop, random コマンドは、コントロールフローを利用する
                kind = 'seq' if msg in SEQ_CMDS else ('loop' if msg in LOOP_CMDS else 'random')
                builder.start_control_flow(kind)
                options.append(builder.make_control_flow_refernce_label())
                builder.add_command(sender, msg, options, children)
                builder.add_new_control_flow_block()
                return True

        elif msg in ELSE_CMDS:
            # 現在のコントロールフローから離脱する
            builder.add_command(sender, builder.make_control_flow_refernce_label(-1), [], [])
            builder.add_new_control_flow_block()
            return True

        elif msg in ELIF_CMDS:
            if builder.get_current_control_flow_kind() != 'if':
                builder.raise_error('@elif は @if の中でのみ使用できます')
            # 現在のコントロールフローから離脱する
            builder.add_command(sender, builder.make_control_flow_refernce_label(-1), [], [])
            builder.add_new_control_flow_block()

            # elif は if と同じ処理をするが、現在のコントロールフローを継続する
            flow_index = builder.get_current_control_flow_index()
            options.append(builder.make_control_flow_refernce_label(flow_index))
            options.append(builder.make_control_flow_refernce_label(flow_index + 1))
            builder.add_command(sender, IF_CMDS[0], options, children)
            builder.add_new_control_flow_block()
            return True

        elif msg in END_CMDS:
            builder.add_command(sender, builder.make_control_flow_refernce_label(-1), [], [])
            builder.add_new_control_flow_block()
            # コントロールフローを終了する
            builder.end_control_flow()
            return True

        elif msg in CALL_CMDS:
            return_label = builder.make_internal_label('return')
            return_full_label = f'*{builder.scene.get_fullpath()}{return_label}'
            new_options = options.copy()
            new_options.insert(1, return_full_label)
            builder.add_command(sender, msg, new_options, children)
            builder.add_new_string_block(return_label)
            return True

        builder.add_command(sender, msg, options, children)
        return True


class CommonCommands_RuntimeObject(object):
    def __init__(self):
        self.context = None
        pass

    @property
    def uid(self):
        if self.context != None:
            return str(self.context.user)
        return 'None'

    @property
    def scene(self):
        if self.context != None:
            return str(self.context.status.scene)
        return 'None'


class CommonCommands_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.lastContext = None
        self.reset_keyword = params['reset_keyword']
        self.timezone = pytz.timezone(params.get('timezone', 'utc'))
        self.cmds_not_handle_here = (IMAGE_CMDS + VIDEO_CMDS + RAWIMAGE_CMDS + OR_CMDS + IF_CMDS + ELSE_CMDS + ELIF_CMDS + END_CMDS + SEQ_CMDS + LOOP_CMDS + RANDOM_CMDS + CALL_CMDS + RETURN_CMDS + DEFER_CMDS)

    def modify_incoming_action(self, context, action):
        if action == self.reset_keyword:
            # 強制リセットキーワードがアクションとして入ってきた場合は
            # プレイヤーの状態を初期化して処理を終了
            context.status.reset()
            context.add_reaction(None, 'リセットしました')
            return None
        return action

    def run_command(self, context, sender, msg, options, _children=[]):
        def set_var(key, value):
            # None を代入したら、変数を削除するという特殊な処理
            context.set_or_del_status_value(key, value)
        def set_list(l, key, value): l[key] = value
        def set_dict(d, key, value):
            # None を代入したら、キーを削除するという特殊な処理
            if value is None:
                del d[key]
            else:
                d[key] = value

        if msg in self.cmds_not_handle_here:
            # 画像と制御系のコマンドは scenario.py 内で直接対応
            return False
        elif msg in RESET_CMDS:
            context.status.reset()
        elif msg in SET_CMDS:
            if context.version >= 3:
                lhs = options[0]
                rhs = options[1]
                new_value = rhs.eval(context.env, context.env.matches)
                lhs.eval_assignment(new_value, context.env, context.env.matches, set_var, set_list, set_dict)
            else:
                value = options[1]
                if isinstance(value, Expression):
                    value = value.eval(context.env, context.env.matches, set_var, set_list, set_dict)
                context.status[options[0]] = value
        elif msg in FORWARD_CMDS:
            bot_name = options[0]
            action = options[1]
            to_bot = main.get_bot(bot_name)
            if to_bot is None or to_bot.get_interface(context.service_name) is None:
                logging.error("invalid bot name: @forward :"+ bot_name)
                context.add_reaction(None, "<<@forwardを解釈できませんでした>>")
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
                context.add_reaction(None, "<<@delayを解釈できませんでした>>")
                return True
            send_request(bot_name, context.user, action, delay_secs)
        elif msg in RESET_NODES_CMDS:
            if len(options) > 0:
                target_name = options[0]
                del context.status['$$node.seq.' + target_name]
            else:
                for key in list(context.status.keys()):
                    if key.startswith('$$node.seq.'):
                        del context.status[key]
        elif msg in NEW_CHAPTER_CMDS:
            if context.version >= 3:
                for key in list(context.status.keys()):
                    if key.startswith('$c_') or key.startswith('$$node.seq.'):
                        del context.status[key]
            else:
                for key in list(context.status.keys()):
                    if key.startswith('$$'):
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
        elif msg in POSTJSON_CMDS:
            url = options[0]
            data_str = options[1]
            if len(options) > 2 and options[2]:
                keys = [key.strip() for key in options[2].split(',')]
            else:
                keys = []
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                logging.error(f'Invalid JSON format for @postjson: {data_str}')
                context.status[POSTJSON_RESULT_VARIABLE] = False
                return True
            try:
                response = requests.post(
                    url,
                    headers={'Content-Type': 'application/json; charset=UTF-8'},
                    data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                    timeout=120
                )
                if response.status_code == 200:
                    response_data = response.json()
                else:
                    logging.error(f'Failed to request @postjson: {response.status_code} {response.text}')
                    context.status[POSTJSON_RESULT_VARIABLE] = False
                    return True
            except requests.RequestException as e:
                logging.error(f'Failed to request @postjson: {e}')
                context.status[POSTJSON_RESULT_VARIABLE] = False
                return True
            context.status[POSTJSON_RESPONSE_VARIABLE] = response_data
            result_flag = True
            for key in keys:
                value = response_data.get(key, None)
                context.set_or_del_status_value('$_' + key.lower(), value)
                if key not in response_data:
                    result_flag = False
            context.status[POSTJSON_RESULT_VARIABLE] = result_flag
        elif msg in GETJSON_CMDS:
            url = options[0]
            data_str = options[1]
            if len(options) > 2 and options[2]:
                keys = [key.strip() for key in options[2].split(',')]
            else:
                keys = []
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                logging.error(f'Invalid JSON format for @postjson: {data_str}')
                context.status[POSTJSON_RESULT_VARIABLE] = False
                return True
            try:
                response = requests.get(
                    url,
                    headers={'Content-Type': 'application/json; charset=UTF-8'},
                    params=data,
                    timeout=120
                )
                if response.status_code == 200:
                    response_data = response.json()
                else:
                    logging.error(f'Failed to request @getjson: {response.status_code} {response.text}')
                    context.status[POSTJSON_RESULT_VARIABLE] = False
                    return True
            except requests.RequestException as e:
                logging.error(f'Failed to request @getjson: {e}')
                context.status[POSTJSON_RESULT_VARIABLE] = False
                return True
            context.status[POSTJSON_RESPONSE_VARIABLE] = response_data
            result_flag = True
            for key in keys:
                value = response_data.get(key, None)
                context.set_or_del_status_value('$_' + key.lower(), value)
                if key not in response_data:
                    result_flag = False
            context.status[POSTJSON_RESULT_VARIABLE] = result_flag
        elif msg in LOG_CMDS:
            category = options[0]
            if len(options) == 2:
                message = options[1]
            else:
                message = options[1:]
            timestamp = datetime.datetime.now(tz=self.timezone).strftime('%Y/%m/%d %H:%M:%S')
            scene_str = context.status.scene
            uid_str = str(context.user)
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
        elif msg in RAISE_CMDS:
            message = options[0]
            raise Exception(message)
        else:
            logging.error('内部エラー：未知のコマンドです:' + msg)
            context.add_reaction(None, "<<内部エラー：未知のコマンドです>>")
        return True

    def get_runtime_object(self, _name, context):
        runtime_object = CommonCommands_RuntimeObject()
        runtime_object.context = context
        return runtime_object


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
            names=VIDEO_CMDS,
            options='image text [text|label]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=RAWIMAGE_CMDS,
            options='text text',
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
            names=POSTJSON_CMDS,
            options='raw raw [raw]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=GETJSON_CMDS,
            options='raw raw [raw]',
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
        commands.CommandEntry(
            names=RAISE_CMDS,
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
            options='expr [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
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
            names=SEQ_CMDS,
            # TODO: 可変長表現の追加
            options='[label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=SEQ_CMDS,
            # TODO: 可変長表現の追加
            options='label [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=1),
        commands.CommandEntry(
            names=LOOP_CMDS,
            # TODO: 可変長表現の追加
            options='[label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=LOOP_CMDS,
            # TODO: 可変長表現の追加
            options='label [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=1),
        commands.CommandEntry(
            names=RANDOM_CMDS,
            # TODO: 可変長表現の追加
            options='[label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=RANDOM_CMDS,
            # TODO: 可変長表現の追加
            options='label [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label] [label]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=1),
        commands.CommandEntry(
            names=ELSE_CMDS,
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=ELIF_CMDS,
            options='expr',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=END_CMDS,
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=SET_CMDS,
            options='expr expr',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
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
        commands.CommandEntry(
            names=CALL_CMDS,
            options='label [text] [text] [text] [text] [text] [text] [text] [text] [text] [text] [text] [text] [text] [text] [text]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=RETURN_CMDS,
            options='[expr]',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
        commands.CommandEntry(
            names=DEFER_CMDS,
            options='label',
            builder=builder,
            runtime=runtime,
            service='*',
            min_version=3),
    ])

    commands.register_object(commands.ObjectEntry(
        names=COMMON_OBJECT,
        runtime=runtime,
        service='*'))
