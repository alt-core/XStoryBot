"""プロセス共通のログ初期化。

GCP の本番系は Cloud Logging へ送り、それ以外（AWS、ローカル、GCP の test/local）は
標準エラーへ INFO 以上を出す。deploy 環境名 test は AWS の test stack など実環境でも
使われるため、ここで特別扱いはしない（unit test での出力はテスト側で扱う）。
"""

import logging
import sys
import traceback


def configure(provider, deploy_env):
    if provider == 'gcp' and deploy_env not in ('test', 'local'):
        _configure_cloud_logging()
        return
    # basicConfig は root に handler が既にあると何もしないので、level は必ず別に設定する
    logging.basicConfig(format='%(levelname)s %(name)s: %(message)s')
    logging.getLogger().setLevel(logging.INFO)


def _configure_cloud_logging():
    # ログを Cloud Logging に送信するための初期化
    import google.cloud.logging
    client = google.cloud.logging.Client()
    client.setup_logging()

    # 例外も Logging ライブラリで送信する（これを設定しないと、エラー出力が1行1ログとして記録される）
    def exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        tb_list = traceback.extract_tb(exc_traceback)
        if tb_list:
            last_entry = tb_list[-1]
            location_info = f"{last_entry.filename}:{last_entry.lineno} ({last_entry.name})"
        else:
            location_info = "情報なし"

        summary = f"例外が発生しました: {exc_type.__name__} at {location_info}"
        logging.error(summary, exc_info=(exc_type, exc_value, exc_traceback))

    sys.excepthook = exception_handler
