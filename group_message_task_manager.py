# coding: utf-8

import logging
from group_message_task_db import GroupMessageTaskDB
import users
import task_client
import settings

class GroupMessageTaskManager:

    def __init__(self, bot_name, bot_instance=None):
        self.bot_name = bot_name
        self.bot = bot_instance

    @classmethod
    def get_task(cls, task_id):
        return GroupMessageTaskDB.get_task(task_id)

    @staticmethod
    def _is_finished(task):
        return task['status'] in (
            GroupMessageTaskDB.STATUS_COMPLETED,
            GroupMessageTaskDB.STATUS_ABORTED,
        )

    @staticmethod
    def _skip_task(task_id, task):
        return {
            'message': f"タスク {task_id} のバッチは処理済み、または中止されています",
            'task_id': task_id,
            'status': task['status'],
        }, 200

    def _update_batch_status(self, task_id, batch_index, status, error=None):
        def build_update(data):
            if (self._is_finished(data) or
                    data.get('current_batch', 0) not in (batch_index, batch_index + 1)):
                return {}
            update = {'status': status}
            if error is not None:
                update['error_messages'] = GroupMessageTaskDB.build_error_messages(
                    data, error)
            return update

        return GroupMessageTaskDB.update_task(task_id, build_update)

    def handle_batch_process_request(self, task_id, batch_index=0, batch_size=None,
                                     max_workers=None, max_rate=None):
        batch_size = batch_size or settings.OPTIONS.get('group_batch_size', 2000)
        max_workers = max_workers or settings.OPTIONS.get('group_max_workers', 150)
        max_rate = max_rate or settings.OPTIONS.get('group_max_rate', 500)

        logging.info(f"バッチ処理リクエスト処理: task_id={task_id}, batch_index={batch_index}")
        try:
            task = self.get_task(task_id)
            if not task:
                return {'error': 'タスクが見つかりません'}, 404
            if self._is_finished(task) or task.get('current_batch', 0) > batch_index + 1:
                return self._skip_task(task_id, task)
            if task.get('current_batch', 0) < batch_index:
                raise ValueError('前のバッチの結果がまだ保存されていません')

            reservation = self._reschedule_if_early(task_id, task, batch_index)
            if reservation is not None:
                return reservation

            self._update_batch_status(
                task_id, batch_index, GroupMessageTaskDB.STATUS_RUNNING)
            task = self.get_task(task_id)
            if not task:
                return {'error': 'タスクが見つかりません'}, 404
            if self._is_finished(task) or task.get('current_batch', 0) > batch_index + 1:
                return self._skip_task(task_id, task)
            if task.get('current_batch', 0) == batch_index + 1:
                return self._register_next_batch(task_id, task, batch_index)
            return self.process_batch(
                task_id, batch_index, batch_size, max_workers, max_rate)
        except Exception as error:
            logging.exception(
                'グループ配信バッチを処理できませんでした: task_id=%s, batch_index=%s',
                task_id, batch_index)
            try:
                self._update_batch_status(
                    task_id, batch_index, GroupMessageTaskDB.STATUS_FAILED,
                    error=str(error))
            except Exception:
                logging.exception('グループ配信の失敗状態を保存できませんでした')
            return {'error': f'バッチ処理に失敗しました: {str(error)}'}, 500

    def _reschedule_if_early(self, task_id, task, batch_index):
        import datetime

        scheduled_time = task.get('scheduled_at')
        if not scheduled_time or not hasattr(scheduled_time, 'timestamp'):
            return None
        scheduled_time_dt = datetime.datetime.fromtimestamp(
            scheduled_time.timestamp(), tz=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        time_diff_seconds = (scheduled_time_dt - now).total_seconds()
        if time_diff_seconds <= 60:
            logging.info(f"タスク {task_id} の予約時間は1分以内（{time_diff_seconds:.1f}秒）なので実行します")
            return None

        logging.info(f"タスク {task_id} は予約実行です（あと約{time_diff_seconds/60:.1f}分）。再キューイングします。")
        task_client.create_task(
            queue_name='group-message-queue',
            url=f'/api/v1/bots/{self.bot_name}/process_group_batch',
            params={'message_task_id': task_id, 'batch_index': batch_index},
            delay_seconds=time_diff_seconds,
        )
        if task['status'] == GroupMessageTaskDB.STATUS_FAILED:
            def build_update(data):
                if (data['status'] == GroupMessageTaskDB.STATUS_FAILED and
                        data.get('current_batch', 0) == batch_index):
                    return {'status': GroupMessageTaskDB.STATUS_PENDING}
                return {}

            if not GroupMessageTaskDB.update_task(task_id, build_update):
                raise ValueError('配信タスクが見つかりません')
            task = self.get_task(task_id)
            if not task:
                raise ValueError('配信タスクが見つかりません')
            if self._is_finished(task) or task.get('current_batch', 0) != batch_index:
                return self._skip_task(task_id, task)
        return {
            'message': f"タスク {task_id} は予約実行（あと約{time_diff_seconds/60:.1f}分）のため再キューイングしました",
            'task_id': task_id,
            'status': task['status'],
            'scheduled_at': scheduled_time_dt.isoformat(),
        }, 200

    def process_batch(self, task_id, batch_index=0, batch_size=100, max_workers=20, max_rate=200):
        task = self.get_task(task_id)
        if not task:
            return {'error': 'タスクが見つかりません'}, 404

        group_id = task['group_id']

        if task.get('is_retry'):
            members = GroupMessageTaskDB.get_members_from_storage(task_id)
        else:
            members = users.get_group_members(group_id)

        if not members:
            saved_task = self._complete_empty_task(task_id, task)
            if saved_task['status'] != GroupMessageTaskDB.STATUS_COMPLETED:
                return self._skip_task(task_id, saved_task)
            return {
                'message': f"グループ {task['group_id']} にメンバーがいません",
                'task_id': task_id,
                'status': GroupMessageTaskDB.STATUS_COMPLETED,
                'members_count': 0
            }, 200

        total_count = len(members)
        batch_count = (total_count + batch_size - 1) // batch_size

        start_idx = batch_index * batch_size
        end_idx = min(start_idx + batch_size, total_count)
        current_batch_members = members[start_idx:end_idx]
        if task.get('is_retry'):
            current_batch_member_ids = current_batch_members
        else:
            current_batch_member_ids = [
                member.serialize() for member in current_batch_members
            ]

        batch_task_id = f"{task_id}_batch_{batch_index}"

        success_count, error_count, successful_members, error_logs = self._process_batch_members(
            batch_task_id, current_batch_member_ids, task, max_workers, max_rate
        )

        return self._handle_batch_completion(
            task_id, task, batch_index, batch_count,
            success_count, error_count, error_logs,
            batch_size, total_count
        )

    def _process_batch_members(self, batch_task_id, member_ids, task, max_workers, max_rate):
        logging.info(f"バッチの処理を開始（メンバー数: {len(member_ids)}, max_workers: {max_workers}, max_rate: {max_rate}）")

        return GroupMessageTaskDB.process_members_in_parallel(
            task_id=batch_task_id,
            process_function=lambda member_id: self._process_member(member_id, task),
            max_workers=max_workers,
            max_rate=max_rate,
            member_ids=member_ids
        )

    def _process_member(self, member_id, task):
        try:
            member = users.User.deserialize(member_id)
            interface = self.bot.get_interface(member.service_name)
            if interface is not None:
                context = interface.create_context(member, task['action'], task['attrs'])
                result = self.bot.handle_action(context)
                if result is None:
                    # handle_action は再試行を尽くすと None を返す。送信できなかったので失敗に数える
                    return False, 'handle_action failed'
                return True, None
            else:
                return False, f"インターフェースが見つかりません: {member.service_name}"
        except Exception as e:
            logging.error(f"Error processing group message for {member_id}: {str(e)}")
            return False, str(e)

    def _handle_batch_completion(self, task_id, task, batch_index, batch_count,
                                success_count, error_count, error_logs,
                                batch_size, total_count):
        errors = [f'{entry[0]}: {entry[1]}' for entry in error_logs]
        failed_member_ids = [entry[0] for entry in error_logs]
        if failed_member_ids:
            try:
                GroupMessageTaskDB._append_failed_member_list(
                    task_id, failed_member_ids)
            except Exception:
                logging.exception('失敗メンバー一覧を保存できませんでした')

        next_batch_index = batch_index + 1
        checkpoint_applied = False

        def build_update(data):
            # callbackの再試行ごとに、最新状態から適用結果も上書きする。
            nonlocal checkpoint_applied
            checkpoint_applied = (
                not self._is_finished(data) and data.get('current_batch', 0) == batch_index)
            if not checkpoint_applied:
                return {}
            return {
                'status': (GroupMessageTaskDB.STATUS_COMPLETED
                           if next_batch_index >= batch_count
                           else GroupMessageTaskDB.STATUS_RUNNING),
                'processed_members': data.get('processed_members', 0) + success_count + error_count,
                'successful_members': data.get('successful_members', 0) + success_count,
                'failed_members': data.get('failed_members', 0) + error_count,
                'error_messages': GroupMessageTaskDB.build_error_messages(
                    data, '\n'.join(errors) if errors else None),
                'current_batch': next_batch_index,
                'total_batches': batch_count,
                'total_members': total_count,
            }

        if not GroupMessageTaskDB.update_task(task_id, build_update):
            raise ValueError('配信タスクが見つかりません')
        saved_task = self.get_task(task_id)
        if not saved_task:
            raise ValueError('配信タスクが見つかりません')
        if (not checkpoint_applied or
                saved_task['status'] == GroupMessageTaskDB.STATUS_ABORTED or
                saved_task.get('current_batch', 0) > next_batch_index):
            return self._skip_task(task_id, saved_task)
        if saved_task['status'] == GroupMessageTaskDB.STATUS_COMPLETED:
            return {
                'message': '全バッチ処理が完了しました',
                'task_id': task_id,
                'status': saved_task['status'],
                'batch_count': saved_task['total_batches'],
                'total_count': saved_task['total_members'],
                'success_count': saved_task['successful_members'],
                'error_count': saved_task['failed_members'],
            }, 200
        return self._register_next_batch(task_id, saved_task, batch_index)

    def _register_next_batch(self, task_id, task, batch_index):
        next_batch_index = batch_index + 1
        if task.get('current_batch', 0) != next_batch_index:
            raise ValueError('当バッチの結果がまだ保存されていません')
        if next_batch_index >= task['total_batches']:
            raise ValueError('最終バッチの完了状態が保存されていません')
        task_client.create_task(
            queue_name='group-message-queue',
            url=f'/api/v1/bots/{self.bot_name}/process_group_batch',
            params={
                'message_task_id': task_id,
                'batch_index': next_batch_index,
            },
        )
        return {
            'message': f'次のバッチ {next_batch_index} を登録しました',
            'task_id': task_id,
            'status': GroupMessageTaskDB.STATUS_RUNNING,
            'batch_index': batch_index,
            'next_batch_index': next_batch_index,
            'batch_count': task['total_batches'],
            'total_count': task['total_members'],
        }, 200

    def _complete_empty_task(self, task_id, task):
        def build_update(data):
            if (self._is_finished(data) or
                    data.get('current_batch', 0) != task.get('current_batch', 0)):
                return {}
            return {
                'status': GroupMessageTaskDB.STATUS_COMPLETED,
                'total_batches': 0,
                'error_messages': GroupMessageTaskDB.build_error_messages(
                    data, f"グループ {task['group_id']} にメンバーがいません"),
            }

        if not GroupMessageTaskDB.update_task(task_id, build_update):
            raise ValueError('配信タスクが見つかりません')
        saved_task = self.get_task(task_id)
        if not saved_task:
            raise ValueError('配信タスクが見つかりません')
        return saved_task
