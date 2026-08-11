# coding: utf-8

import logging
from group_message_task_db import GroupMessageTaskDB
from models import GroupMembersDB
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

    @classmethod
    def mark_task_as_running(cls, task_id):
        return GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_RUNNING
        )

    @classmethod
    def mark_task_as_completed(cls, task_id, processed=None, successful=None, failed=None, error=None, current_batch=None):
        return GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_COMPLETED,
            processed=processed,
            successful=successful,
            failed=failed,
            error=error,
            current_batch=current_batch
        )

    @classmethod
    def mark_task_as_aborted(cls, task_id, error=None):
        return GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_ABORTED,
            error=error
        )

    @classmethod
    def update_task_progress(cls, task_id, processed=None, current_batch=None):
        return GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_RUNNING,
            processed=processed,
            current_batch=current_batch
        )

    def handle_batch_process_request(self, task_id, batch_index=0, batch_size=None, max_workers=None, max_rate=None):
        batch_size = batch_size or settings.OPTIONS.get('group_batch_size', 2000)
        max_workers = max_workers or settings.OPTIONS.get('group_max_workers', 150)
        max_rate = max_rate or settings.OPTIONS.get('group_max_rate', 500)

        logging.info(f"バッチ処理リクエスト処理: task_id={task_id}, batch_index={batch_index}")

        task = self.get_task(task_id)
        if not task:
            logging.error(f"タスクが見つかりません: task_id={task_id}")
            return {'error': 'タスクが見つかりません'}, 404

        # タスクが中止または完了状態ならスキップ
        if task['status'] in [GroupMessageTaskDB.STATUS_COMPLETED, GroupMessageTaskDB.STATUS_ABORTED]:
            return {
                'message': f"タスク {task_id} は既に {task['status']} 状態です",
                'task_id': task_id,
                'status': task['status']
            }, 200

        # 予約タスクの場合は現在時刻と比較
        import datetime
        if 'scheduled_at' in task and task['scheduled_at']:
            now = datetime.datetime.now(datetime.timezone.utc)
            scheduled_time = task['scheduled_at']

            # Firestoreのタイムスタンプをdatetimeに変換
            if hasattr(scheduled_time, 'timestamp'):
                scheduled_time_dt = datetime.datetime.fromtimestamp(
                    scheduled_time.timestamp(), tz=datetime.timezone.utc
                )

                time_diff_seconds = (scheduled_time_dt - now).total_seconds()

                # 1分以上先の予約の場合は再キューイング
                if time_diff_seconds > 60:
                    logging.info(f"タスク {task_id} は予約実行です（あと約{time_diff_seconds/60:.1f}分）。再キューイングします。")

                    task_client.create_task(
                        queue_name='group-message-queue',
                        url=f'/api/v1/bots/{self.bot_name}/process_group_batch',
                        params={
                            'message_task_id': task_id,
                            'batch_index': batch_index
                        },
                        delay_seconds=time_diff_seconds
                    )

                    return {
                        'message': f"タスク {task_id} は予約実行（あと約{time_diff_seconds/60:.1f}分）のため再キューイングしました",
                        'task_id': task_id,
                        'status': GroupMessageTaskDB.STATUS_PENDING,
                        'scheduled_at': scheduled_time_dt.isoformat()
                    }, 200

                # 時間差が1分以内なら即時実行（以降の処理に進む）
                logging.info(f"タスク {task_id} の予約時間は1分以内（{time_diff_seconds:.1f}秒）なので実行します")

        self.mark_task_as_running(task_id)

        try:
            return self.process_batch(
                task_id, batch_index, batch_size, max_workers, max_rate
            )
        except Exception as e:
            logging.exception(f"Error processing batch: {str(e)}")
            self.mark_task_as_aborted(task_id, error=str(e))
            return {'error': f'failed to process batch: {str(e)}'}, 500

    def process_batch(self, task_id, batch_index=0, batch_size=100, max_workers=20, max_rate=200):
        task = self.get_task(task_id)
        if not task:
            return {'error': 'タスクが見つかりません'}, 404

        group_id = task['group_id']

        temp_members = users.get_group_members(group_id)
        if temp_members is None:
            return {'error': 'グループが見つかりません'}, 404

        members = users.get_group_members(task['group_id'])
        if not members:
            self._complete_empty_task(task_id, task)
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
        member_ids = [member.serialize() for member in current_batch_members]

        batch_task_id = f"{task_id}_batch_{batch_index}"

        success_count, error_count, successful_members, error_logs = self._process_batch_members(
            batch_task_id, member_ids, task, max_workers, max_rate
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
                self.bot.handle_action(context)
                return True, None
            else:
                return False, f"インターフェースが見つかりません: {member.service_name}"
        except Exception as e:
            logging.error(f"Error processing group message for {member_id}: {str(e)}")
            return False, str(e)

    def _complete_empty_task(self, task_id, task):
        # メンバーのない特殊ケースを完了状態にする
        error_message = f"グループ {task['group_id']} にメンバーがいません"

        self.mark_task_as_completed(
            task_id=task_id,
            processed=0,
            successful=0,
            failed=0,
            error=error_message
        )

    def _handle_batch_completion(self, task_id, task, batch_index, batch_count,
                               success_count, error_count, error_logs, batch_size, total_count):
        errors = [f"{err[0]}: {err[1]}" for err in error_logs]
        failed_member_ids = [err[0] for err in error_logs]
        if failed_member_ids:
            try:
                GroupMessageTaskDB._append_failed_member_list(
                    task_id, failed_member_ids
                )
            except Exception as e:
                logging.error(
                    f"失敗メンバー一覧を保存できませんでした: "
                    f"task_id={task_id}, error={str(e)}"
                )

        next_batch_index = batch_index + 1
        if next_batch_index < batch_count:
            return self._schedule_next_batch(
                task_id, task, batch_index, next_batch_index,
                batch_count, success_count, error_count,
                errors, batch_size, total_count
            )
        else:
            return self._complete_all_batches(
                task_id, task, batch_count, total_count,
                success_count, error_count, errors
            )

    def _schedule_next_batch(self, task_id, task, batch_index, next_batch_index,
                          batch_count, success_count, error_count,
                          errors, batch_size, total_count):
        task_client.create_task(
            queue_name='group-message-queue',
            url=f'/api/v1/bots/{self.bot_name}/process_group_batch',
            params={
                'message_task_id': task_id,
                'batch_index': next_batch_index
            }
        )

        # 途中経過を更新
        processed_count = next_batch_index * batch_size
        current_success = task.get('successful_members', 0)
        current_error = task.get('failed_members', 0)
        error_summary = "\n".join(errors) if errors else None
        GroupMessageTaskDB.update_task_status(
            task_id=task_id,
            status=GroupMessageTaskDB.STATUS_RUNNING,
            processed=processed_count,
            successful=current_success + success_count,
            failed=current_error + error_count,
            error=error_summary,
            current_batch=next_batch_index
        )

        return {
            'message': f"バッチ {batch_index} 処理完了。次のバッチ {next_batch_index} をキューに追加しました",
            'task_id': task_id,
            'status': GroupMessageTaskDB.STATUS_RUNNING,
            'batch_index': batch_index,
            'next_batch_index': next_batch_index,
            'batch_count': batch_count,
            'total_count': total_count,
            'success_count': success_count,
            'error_count': error_count
        }, 200

    def _complete_all_batches(self, task_id, task, batch_count, total_count,
                           success_count, error_count, errors):
        current_success = task.get('successful_members', 0)
        current_error = task.get('failed_members', 0)
        current_errors = task.get('error_messages', [])

        recent_errors = errors
        if current_errors:
            recent_errors = (current_errors + errors)[:10]

        error_summary = "\n".join(recent_errors) if recent_errors else None

        self.mark_task_as_completed(
            task_id=task_id,
            processed=total_count,
            successful=current_success + success_count,
            failed=current_error + error_count,
            error=error_summary,
            current_batch=batch_count
        )

        return {
            'message': f"全バッチ処理完了。成功: {current_success + success_count}, エラー: {current_error + error_count}",
            'task_id': task_id,
            'status': GroupMessageTaskDB.STATUS_COMPLETED,
            'batch_count': batch_count,
            'total_count': total_count,
            'success_count': current_success + success_count,
            'error_count': current_error + error_count
        }, 200
