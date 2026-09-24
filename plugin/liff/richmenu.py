"""LIFFの応答形式を保ったまま、実行中Botのメニューを切り替える。"""

import commands
from plugin.line.command_names import RICHMENU_CMDS
from richmenu_spec import resolve_line_id


def _line_target(context):
    import main
    bot = main.get_bot(context.bot_name)
    interface = bot.get_interface('line') if bot else None
    kind, separator, user_id = context.user.user_id.partition(',')
    if (interface is None or context.user.service_name != 'line'
            or kind != 'user' or not separator or not user_id):
        raise ValueError('LIFFの@richmenuには同じBotのLINE interfaceとLINE利用者が必要です')
    return interface, user_id


class LiffRichmenuRuntime:
    def run_command(self, context, sender, msg, options):
        if context.get_interface('webchat') is not None:
            from plugin.webchat.presenter import WebchatRichmenuRuntime
            return WebchatRichmenuRuntime().run_command(context, sender, msg, options)
        resolve_line_id(options[0], getattr(context, 'richmenu_ids', {}))
        _line_target(context)
        return False

    def construct_response(self, context, sender, msg, options):
        interface, user_id = _line_target(context)
        menu_id = resolve_line_id(options[0], getattr(context, 'richmenu_ids', {}))
        interface.api.link_rich_menu(user_id, menu_id)
        return True


def register_runtime(params):
    from plugin.line.default_commands import LineDefaultCommandsPlugin_Builder
    commands.register_command(commands.CommandEntry(
        names=RICHMENU_CMDS, options='text',
        builder=LineDefaultCommandsPlugin_Builder(params),
        runtime=LiffRichmenuRuntime(), service='liff'))
