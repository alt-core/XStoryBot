"""HTTPとバッチ処理で共有するシナリオビルド処理。"""

import logging


class BotNotFoundError(ValueError):
    """指定されたBotが存在しない。"""


def build_runtime(bot, task_id='', skip_image=False, force=False, version=1):
    """解決済みのBotをビルドし、従来と同じ結果を返す。"""
    options = {
        'skip_image': bool(skip_image),
        'force': bool(force),
    }
    logging.info(
        'start building...: options: %s, version: %s', options, version)
    return bot.build_scenario(
        task_id=task_id,
        options=options,
        version=version,
    )


def build_bot(
        bot_name, task_id='', skip_image=False, force=False,
        main_module=None):
    """指定Botを解決してビルドする。"""
    if main_module is None:
        # CLIの引数検証前にクラウドclientを初期化しないよう遅延importする。
        import main as main_module

    bot = main_module.get_bot(bot_name)
    if bot is None:
        raise BotNotFoundError(f'Botが見つかりません: {bot_name}')
    version = main_module.get_options().get('scenario_version', 1)
    return build_runtime(
        bot,
        task_id=task_id,
        skip_image=skip_image,
        force=force,
        version=version,
    )
