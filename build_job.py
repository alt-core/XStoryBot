"""単発コンテナからシナリオビルドを実行するCLI。"""

import argparse
import logging

import build_service


def create_parser():
    parser = argparse.ArgumentParser(
        description='指定したBotのシナリオをビルドします。')
    parser.add_argument('--bot-name', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--skip-image', action='store_true')
    parser.add_argument('--force', action='store_true')
    return parser


def main(argv=None):
    args = create_parser().parse_args(argv)
    try:
        ok, _ = build_service.build_bot(
            bot_name=args.bot_name,
            task_id=args.task_id,
            skip_image=args.skip_image,
            force=args.force,
        )
    except build_service.BotNotFoundError:
        logging.error('ビルド対象のBotが見つかりません')
        return 2
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
