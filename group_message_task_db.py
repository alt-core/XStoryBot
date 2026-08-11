import json
import time
import uuid
import logging

from cloud_backend import create_object_store
from cloud_backend.contracts import ObjectNotFoundError, ObjectStoreError
from models import GroupMembersDB, get_state_store

class GroupMessageTaskDB:
    # クラス定数
    MAX_FAILED_IDS_IN_DB = 100
    MAX_ERROR_MESSAGES_IN_DB = 10

    # タスクステータス定義
    STATUS_PENDING = 'pending'     # 作成済み、処理待ち
    STATUS_RUNNING = 'running'     # 実行中
    STATUS_COMPLETED = 'completed' # 完了
    STATUS_FAILED = 'failed'       # 失敗
    STATUS_ABORTED = 'aborted'     # 中断（ユーザーによる）

    # 設定用変数
    _options = None
    _state_store = None
    _object_store = None

    # タスク設定のキャッシュ
    _task_settings = {}
    _batch_size = 2000  # デフォルト値
    _default_max_workers = 150  # デフォルト値
    _default_max_rate = 500  # デフォルト値

    @staticmethod
    def initialize(gcp_settings, options):
        GroupMessageTaskDB._options = options
        GroupMessageTaskDB._state_store = get_state_store()
        GroupMessageTaskDB._object_store = create_object_store()

        # Managerと同じフラットな設定値を使う。
        GroupMessageTaskDB._task_settings = {}
        GroupMessageTaskDB._batch_size = options.get('group_batch_size', 2000)
        GroupMessageTaskDB._default_max_workers = options.get('group_max_workers', 150)
        GroupMessageTaskDB._default_max_rate = options.get('group_max_rate', 500)

    @staticmethod
    def create_task(bot_name, group_id, action, attrs, created_by, scheduled_at=None):
        # タスクIDを生成
        task_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"

        members = GroupMembersDB.get_members(group_id)
        total_members = len(members)

        if total_members == 0:
            raise ValueError(f"Group {group_id} has no members")

        member_list_url = GroupMessageTaskDB._store_member_list(task_id, members)

        task_data = {
            'bot_name': bot_name,
            'group_id': group_id,
            'action': action,
            'attrs': attrs,
            'created_by': created_by,
            'status': GroupMessageTaskDB.STATUS_PENDING,
            'total_members': total_members,
            'processed_members': 0,
            'successful_members': 0,
            'failed_members': 0,
            'member_list_url': member_list_url,
            'current_batch': 0,
            'total_batches': (total_members + GroupMessageTaskDB._batch_size - 1) // GroupMessageTaskDB._batch_size,
            'interval_ms': 1,  # 送信間隔（ミリ秒）
            'failed_member_ids': [],  # 失敗したメンバーIDリスト（最大 MAX_FAILED_IDS_IN_DB 件）
            'error_messages': []  # エラーメッセージリスト（最大 MAX_ERROR_MESSAGES_IN_DB 件）
        }

        # 送信予定日時があれば追加
        if scheduled_at:
            task_data['scheduled_at'] = scheduled_at

        GroupMessageTaskDB._state_store.create_group_message_task(
            task_id, task_data)

        return task_id

    @staticmethod
    def _store_member_list(task_id, members):
        member_ids = [member for member in members]

        try:
            reference = GroupMessageTaskDB._object_store.store_private(
                f"group_tasks/{task_id}/members.json",
                json.dumps(member_ids),
            )
            logging.info(f"Stored member list for task {task_id} to GCS.")
            return reference
        except ObjectStoreError as e:
            logging.error(f"Failed to store member list for task {task_id} to GCS: {type(e).__name__} - {str(e)}")
            # 例外を再発生させてタスク作成を失敗させる
            raise  # 例外を再発生させてタスク作成を失敗させる

    @staticmethod
    def get_members_from_storage(task_id):
        try:
            content = GroupMessageTaskDB._object_store.load_private(
                f"group_tasks/{task_id}/members.json").decode('utf-8')
            return json.loads(content)
        except ObjectNotFoundError as e:
            logging.error(f"Member list file not found in GCS for task {task_id}: {str(e)}")
            raise # ファイルがない場合は致命的なので例外を発生させる
        except ObjectStoreError as e:
            logging.error(f"Failed to get member list for task {task_id} from GCS: {type(e).__name__} - {str(e)}")
            raise # その他のGCSエラーも例外を発生させる

    @staticmethod
    def get_task(task_id):
        return GroupMessageTaskDB._state_store.get_group_message_task(task_id)

    @staticmethod
    def update_task_status(task_id, status, processed=None, successful=None, failed=None, error=None, current_batch=None, interval_ms=None, failed_member_id=None):
        update_data = {
            'status': status,
        }

        if processed is not None:
            update_data['processed_members'] = processed

        if successful is not None:
            update_data['successful_members'] = successful

        if failed is not None:
            update_data['failed_members'] = failed

        if current_batch is not None:
            update_data['current_batch'] = current_batch

        if interval_ms is not None:
            update_data['interval_ms'] = interval_ms

        def build_update(data):
            current_update = dict(update_data)

            if error is not None:
                errors = list(data.get('error_messages', []))
                if len(errors) >= GroupMessageTaskDB.MAX_ERROR_MESSAGES_IN_DB: # ★★★ 定数使用 ★★★
                    # 古いものから削除 (リストの末尾に追加される想定なら pop(0))
                    # 現在は先頭に追加しているので、末尾を削除
                    errors = errors[:GroupMessageTaskDB.MAX_ERROR_MESSAGES_IN_DB - 1]
                errors.insert(0, error) # 新しいエラーを先頭に追加
                current_update['error_messages'] = errors

            if failed_member_id is not None:
                # 従来どおりStateStoreのtransaction callback内で追記する。
                try:
                    GroupMessageTaskDB._append_failed_member_list(task_id, [failed_member_id])
                except Exception as e_gcs:
                    # GCS追記失敗はログに残すが、Firestore更新は続行する
                    logging.error(f"Failed to append failed member {failed_member_id} to GCS for task {task_id}: {str(e_gcs)}")

                # Firestoreには最新の N 件のみを保持（UI表示用）
                failed_ids = list(data.get('failed_member_ids', []))

                if failed_member_id not in failed_ids:
                    if len(failed_ids) >= GroupMessageTaskDB.MAX_FAILED_IDS_IN_DB: # ★★★ 定数使用 ★★★
                        # 古いものから削除 (リストの末尾に追加される想定なら pop(0))
                        # 現在は末尾に追加しているので、先頭を削除
                        failed_ids = failed_ids[1:]
                    failed_ids.append(failed_member_id) # 新しいIDを末尾に追加

                    current_update['failed_member_ids'] = failed_ids

            return current_update

        return GroupMessageTaskDB._state_store.update_group_message_task(
            task_id, build_update)

    @staticmethod
    def _store_failed_member_list(task_id, failed_members):
        try:
            reference = GroupMessageTaskDB._object_store.store_private(
                f"group_tasks/{task_id}/failed_members.json",
                json.dumps(failed_members),
            )
            logging.info(f"Stored failed member list for task {task_id} to GCS.")
            return reference
        except ObjectStoreError as e:
            logging.error(f"Failed to store failed member list for task {task_id} to GCS: {type(e).__name__} - {str(e)}")
            raise # GCSエラーは呼び出し元に伝える

    @staticmethod
    def _append_failed_member_list(task_id, new_failed_members):
        try:
            try:
                content = GroupMessageTaskDB._object_store.load_private(
                    f"group_tasks/{task_id}/failed_members.json").decode('utf-8')
                failed_members = json.loads(content)
            except ObjectNotFoundError:
                # ファイルが存在しない場合は空リストから開始
                failed_members = []
            except Exception as e_read: # GCS以外の読み取り/JSONパースエラー
                 logging.error(f"Failed to read/parse existing failed_members.json for task {task_id}: {type(e_read).__name__} - {str(e_read)}")
                 failed_members = [] # 読み取りエラーの場合も空から開始（データ損失リスクあり）

            added_count = 0
            for member in new_failed_members:
                if member not in failed_members:
                    failed_members.append(member)
                    added_count += 1

            if added_count > 0:
                GroupMessageTaskDB._object_store.store_private(
                    f"group_tasks/{task_id}/failed_members.json",
                    json.dumps(failed_members),
                )
                logging.debug(f"Appended {added_count} members to failed_members.json for task {task_id}. Total: {len(failed_members)}")
            else:
                logging.debug(f"No new members to append to failed_members.json for task {task_id}.")

        except ObjectStoreError as e:
            logging.error(f"Failed to append to failed_members.json for task {task_id} in GCS: {type(e).__name__} - {str(e)}")
            raise # GCSエラーは呼び出し元に伝える

    @staticmethod
    def _get_failed_members_from_storage(task_id):
        try:
            content = GroupMessageTaskDB._object_store.load_private(
                f"group_tasks/{task_id}/failed_members.json").decode('utf-8')
            members = json.loads(content)
            logging.debug(f"Retrieved {len(members)} failed members from Cloud Storage for task {task_id}")
            return members
        except ObjectNotFoundError:
            logging.debug(f"failed_members.json not found in GCS for task {task_id}. Returning empty list.")
            return []
        except ObjectStoreError as e:
            logging.error(f"Failed to retrieve failed members from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return [] # エラー時は空リストを返す（リトライできない可能性がある）
        except Exception as e: # GCS以外の予期せぬエラー
            logging.error(f"Unexpected error retrieving failed members from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return []

    @staticmethod
    def abort_task(task_id):
        task = GroupMessageTaskDB.get_task(task_id)
        if not task:
            return False

        # 既に完了または中止されている場合は何もしない
        if task['status'] in [GroupMessageTaskDB.STATUS_COMPLETED, GroupMessageTaskDB.STATUS_ABORTED]:
            return False

        # ステータスを中止に変更
        return GroupMessageTaskDB.update_task_status(task_id, GroupMessageTaskDB.STATUS_ABORTED)

    @staticmethod
    def create_rate_limiter(max_rate=1000):
        import threading
        import time

        # スレッド間で共有するトークンバケットの状態
        class TokenBucket:
            def __init__(self, rate):
                self.rate = rate  # トークンの補充レート（1秒あたり）
                self.tokens = rate  # 初期トークン数
                self.last_refill = time.time()
                self.lock = threading.Lock()

            def get_token(self):
                with self.lock:
                    now = time.time()
                    # トークンを補充
                    elapsed = now - self.last_refill
                    new_tokens = elapsed * self.rate
                    self.tokens = min(self.rate, self.tokens + new_tokens)
                    self.last_refill = now

                    if self.tokens >= 1:
                        # トークンを消費
                        self.tokens -= 1
                        return 0  # 待機不要
                    else:
                        # トークンが不足している場合、次のトークンが利用可能になるまでの待機時間を計算
                        wait_time = (1 - self.tokens) / self.rate
                        return wait_time

        bucket = TokenBucket(max_rate)

        def rate_limit_decorator(func):
            def wrapper(*args, **kwargs):
                # トークンを取得し、必要に応じて待機
                wait_time = bucket.get_token()
                if wait_time > 0:
                    time.sleep(wait_time)
                return func(*args, **kwargs)
            return wrapper

        return rate_limit_decorator

    @staticmethod
    def process_members_in_parallel(task_id, process_function, max_workers=None,
                                    max_rate=None, member_ids=None):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time

        # デフォルト値を設定ファイルから取得
        if max_workers is None:
            max_workers = GroupMessageTaskDB._default_max_workers
            logging.info(f"ワーカー数にデフォルト値を使用: {max_workers}")

        if max_rate is None:
            max_rate = GroupMessageTaskDB._default_max_rate
            logging.info(f"最大レートにデフォルト値を使用: {max_rate} rps")

        # レート制限を適用した処理関数を作成
        rate_limiter = GroupMessageTaskDB.create_rate_limiter(max_rate)
        rate_limited_process = rate_limiter(process_function)

        # タスク情報とメンバーリストを取得
        task = GroupMessageTaskDB.get_task(task_id)

        # バッチタスクIDの場合は元のタスクIDからメンバーリストを取得
        storage_task_id = task_id
        if "_batch_" in task_id:
            # {original_task_id}_batch_{batch_index} 形式から original_task_id を抽出
            storage_task_id = task_id.split("_batch_")[0]
            logging.info(f"バッチタスクID {task_id} を検出: メンバーリストを元のタスクID {storage_task_id} から取得します")

        if member_ids is None:
            all_members = GroupMessageTaskDB.get_members_from_storage(storage_task_id)
        else:
            all_members = member_ids
        total_members = len(all_members)

        # 処理結果を保持するメモリ内コレクション
        successful_members = []
        error_logs = []  # (member_id, error_message, timestamp) のタプルリスト

        # 進捗報告用の変数
        processed_count = 0
        last_report_time = time.time()
        report_interval = 5  # 5秒ごとに進捗を報告

        # タスクステータスを実行中に更新
        GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_RUNNING
        )

        start_time = time.time()
        logging.info(f"タスク {task_id} の並列処理を開始します（メンバー数: {total_members}, ワーカー数: {max_workers}, 最大レート: {max_rate} rps）")

        # 並列処理でメンバーを処理
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(rate_limited_process, member_id): member_id for member_id in all_members}

            for future in as_completed(futures):
                member_id = futures[future]
                processed_count += 1

                try:
                    success, error_message = future.result()
                    if success:
                        successful_members.append(member_id)
                    else:
                        error_logs.append((member_id, error_message, time.time()))
                except Exception as e:
                    error_logs.append((member_id, str(e), time.time()))

                # 定期的に進捗を報告
                current_time = time.time()
                if current_time - last_report_time > report_interval or processed_count == total_members:
                    progress_percent = (processed_count / total_members) * 100
                    elapsed = current_time - start_time
                    rate = processed_count / elapsed if elapsed > 0 else 0
                    logging.info(f"処理進捗: {processed_count}/{total_members} ({progress_percent:.1f}%)")
                    logging.info(f"成功: {len(successful_members)}, 失敗: {len(error_logs)}, 処理速度: {rate:.1f}件/秒")
                    last_report_time = current_time

        end_time = time.time()
        elapsed = end_time - start_time
        rate = total_members / elapsed if elapsed > 0 else 0
        logging.info(f"タスク {task_id} の処理が完了しました（所要時間: {elapsed:.2f}秒, 処理速度: {rate:.1f}件/秒）")

        # 処理完了後、成功メンバーリストをCloud Storageに保存
        if successful_members:
            GroupMessageTaskDB._store_successful_members(task_id, successful_members)
            logging.info(f"成功メンバーリスト（{len(successful_members)}件）を保存しました")

        # エラーログをCloud Storageに保存
        if error_logs:
            GroupMessageTaskDB._store_error_logs(task_id, error_logs)
            logging.info(f"エラーログ（{len(error_logs)}件）を保存しました")

        # タスクステータスを更新（一度だけ）
        successful_count = len(successful_members)
        failed_count = len(error_logs)

        # Firestoreに保存するエラーメッセージは最新の N 件のみ
        recent_errors = [f"{err[0]}: {err[1]}" for err in sorted(error_logs, key=lambda x: x[2], reverse=True)[:GroupMessageTaskDB.MAX_ERROR_MESSAGES_IN_DB]] # ★★★ 定数使用 ★★★
        error_summary = "\n".join(recent_errors) if recent_errors else None

        GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_COMPLETED if failed_count == 0 else GroupMessageTaskDB.STATUS_FAILED,
            processed=total_members,
            successful=successful_count,
            failed=failed_count,
            error=error_summary
        )

        return successful_count, failed_count, successful_members, error_logs

    @staticmethod
    def _store_successful_members(task_id, successful_members):
        try:
            GroupMessageTaskDB._object_store.store_private(
                f"group_tasks/{task_id}/successful_members.json",
                json.dumps(successful_members),
            )
            logging.info(f"Stored successful members list for task {task_id} to GCS.")
        except ObjectStoreError as e:
            logging.error(f"Failed to store successful members list for task {task_id} to GCS: {type(e).__name__} - {str(e)}")
            # このエラーは処理結果の記録に関するものなので、ログ出力に留める

    @staticmethod
    def _store_error_logs(task_id, error_logs):
        try:
            GroupMessageTaskDB._object_store.store_private(
                f"group_tasks/{task_id}/error_logs.json",
                json.dumps(error_logs),
            )
            logging.info(f"Stored error logs for task {task_id} to GCS.")
        except ObjectStoreError as e:
            logging.error(f"Failed to store error logs for task {task_id} to GCS: {type(e).__name__} - {str(e)}")
            # このエラーは処理結果の記録に関するものなので、ログ出力に留める

    @staticmethod
    def get_successful_members(task_id):
        try:
            content = GroupMessageTaskDB._object_store.load_private(
                f"group_tasks/{task_id}/successful_members.json").decode('utf-8')
            return json.loads(content)
        except ObjectNotFoundError:
            logging.debug(f"successful_members.json not found in GCS for task {task_id}. Returning empty list.")
            return []
        except ObjectStoreError as e:
            logging.error(f"Failed to retrieve successful members from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return []
        except Exception as e: # GCS以外の予期せぬエラー
            logging.error(f"Unexpected error retrieving successful members from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return []

    @staticmethod
    def get_error_logs(task_id):
        try:
            content = GroupMessageTaskDB._object_store.load_private(
                f"group_tasks/{task_id}/error_logs.json").decode('utf-8')
            return json.loads(content)
        except ObjectNotFoundError:
            logging.debug(f"error_logs.json not found in GCS for task {task_id}. Returning empty list.")
            return []
        except ObjectStoreError as e:
            logging.error(f"Failed to retrieve error logs from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return []
        except Exception as e: # GCS以外の予期せぬエラー
            logging.error(f"Unexpected error retrieving error logs from GCS for task {task_id}: {type(e).__name__} - {str(e)}")
            return []

    @staticmethod
    def get_remaining_members(task_id):
        all_members = GroupMessageTaskDB.get_members_from_storage(task_id)
        successful_members = GroupMessageTaskDB.get_successful_members(task_id)

        return list(set(all_members) - set(successful_members))

    @staticmethod
    def retry_failed_members(original_task_id, created_by):
        original_task = GroupMessageTaskDB.get_task(original_task_id)
        if not original_task:
            return None

        remaining_members = GroupMessageTaskDB._get_failed_members_from_storage(
            original_task_id
        )

        if not remaining_members:
            return None

        task_id = f"{int(time.time())}-retry-{uuid.uuid4().hex[:8]}"

        member_list_url = GroupMessageTaskDB._store_member_list(task_id, remaining_members)

        task_data = {
            'bot_name': original_task['bot_name'],
            'group_id': original_task['group_id'],
            'action': original_task['action'],
            'attrs': original_task['attrs'],
            'created_by': created_by,
            'status': GroupMessageTaskDB.STATUS_PENDING,
            'total_members': len(remaining_members),
            'processed_members': 0,
            'successful_members': 0,
            'failed_members': 0,
            'member_list_url': member_list_url,
            'current_batch': 0,
            'total_batches': (len(remaining_members) + GroupMessageTaskDB._batch_size - 1) // GroupMessageTaskDB._batch_size,
            'interval_ms': 1,
            'failed_member_ids': [],
            'error_messages': [],
            'original_task_id': original_task_id,
            'is_retry': True
        }
        GroupMessageTaskDB._state_store.create_group_message_task(
            task_id, task_data)

        return task_id

    @staticmethod
    def get_recent_tasks(bot_name, limit=10):
        try:
            return GroupMessageTaskDB._state_store.get_recent_group_message_tasks(
                bot_name, limit)
        except Exception as e:
            logging.error(f"Error fetching recent tasks: {str(e)}")
            # エラー時でも空のリストを返す
            return []
