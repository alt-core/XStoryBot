# coding: utf-8
from bottle import request, response, Bottle, abort, view, static_file, HTTPError
import logging
import requests
import json
from datetime import datetime

import settings
import auth_middleware
import main
import task_client
from models import db
import utility
import build_cache
import users
from group_message_task_db import GroupMessageTaskDB
import auth

app = Bottle()


auth_middleware.initialize()


def abort_json(code, msg):
    abort(code, utility.make_error_json(code, msg))

def _firebase_config_json():
    firebase_settings = settings.GCP_SETTINGS['firebase']
    config = {
        'apiKey': firebase_settings['api_key'],
        'authDomain': firebase_settings['auth_domain'],
        'projectId': settings.GCP_SETTINGS['project_id'],
        'storageBucket': firebase_settings['storage_bucket'],
        'messagingSenderId': firebase_settings['messaging_sender_id'],
        'appId': firebase_settings['app_id'],
    }
    return json.dumps(config, ensure_ascii=False).replace('<', '\\u003c')


@app.get('/dashboard/')
@app.get('/dashboard/<bot_name>')
@view('template/dashboard')
def dashboard(bot_name=None):
    return {
        'initial_bot_name_json': json.dumps(
            bot_name or '', ensure_ascii=False).replace('<', '\\u003c'),
        'firebase_config_json': _firebase_config_json(),
    }


@app.get('/dashboard/api/config')
@auth_middleware.auth_required()
def api_config():
    bots = [
        {
            'id': bot_id,
            'name': bot_settings['name'],
            'description': bot_settings.get('description', ''),
        }
        for bot_id, bot_settings in sorted(
            settings.BOTS.items(), key=lambda item: item[1]['name'])
    ]
    user = request.firebase_user
    logging.info(f"Dashboard accessed by: {user.get('email', '')}")
    return utility.make_ok_json('設定を取得しました', {
        'bots': bots,
        'user_email': user.get('email', ''),
    })


@app.get('/dashboard/static/<filepath:path>')
def serve_static(filepath):
    return static_file(filepath, root='./static/dashboard')

@app.get('/dashboard/build_async/<bot_name>')
@app.post('/dashboard/build_async/<bot_name>')
@auth_middleware.auth_required()
def api_build_async(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    options = {}
    skip_option = request.params.getunicode('skip_image')
    if skip_option:
        options['skip_image'] = skip_option
    force_option = request.params.getunicode('force')
    if force_option:
        options['force'] = force_option

    url = f"/api/build/{bot_name}"

    # DEPLOY_ENV が local の場合は task queue に投げず、直接リクエストを送る
    if settings.DEPLOY_ENV == 'local':
        full_url = f"{settings.SERVICE_SETTINGS['builder']['base_url']}{url}"
        builder_response = requests.post(
            full_url,
            params=options,
            headers={'X-API-Token': auth.get_api_token()}
        )
        return builder_response.text
    else:
        task_id = task_client.create_task(
            queue_name='build-queue',
            url=url,
            params=options
        )
        logging.info(f"enqueue a build task: {task_id}, options: {options}")

        return json.dumps({
            'code': 200,
            'result': 'Success',
            'message': 'Queued',
            'task_id': task_id
        }, ensure_ascii=False)

@app.get('/dashboard/last_build_result/<bot_name>')
@auth_middleware.auth_required()
def api_get_last_build_result(bot_name):
    bot = main.get_bot(bot_name)
    if not bot:
        abort_json(404, 'not found')

    result = build_cache.get_cache(f'last_build_result:{bot.name}')
    if result is None:
        result = json.dumps({
            "status": "Failure",
            "error": "Not Found"
        })
    response.set_header('Content-Type', 'application/json; charset=utf-8')
    return result

@app.get('/dashboard/api/groups')
@auth_middleware.auth_required()
def api_get_groups():
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        # webapi.py の同等機能を呼び出し
        # 注: webapi.py にはグループ一覧を取得するAPIがないため、直接処理
        all_groups = users.get_all_groups()
        all_groups = sorted(
            all_groups, key=lambda group: group.get('id', '').lower())
        return json.dumps({
            'code': 200,
            'result': 'Success',
            'groups': all_groups
        }, ensure_ascii=False)
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error fetching groups: {str(e)}")
        abort_json(500, f'Failed to fetch groups: {str(e)}')

@app.get('/dashboard/api/group_members/<group_id>')
@auth_middleware.auth_required()
def api_get_group_members(group_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        members = users.get_group_members(group_id)
        member_ids = [m.serialize() for m in members]

        return utility.make_ok_json(
            f'グループ {group_id} のメンバー情報を取得しました ({len(member_ids)} 件)',
            {
                'group_id': group_id,
                'count': len(member_ids),
                'members': member_ids
            }
        )
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error getting members for group {group_id}: {str(e)}")
        abort_json(500, f'Failed to get group members: {str(e)}')



@app.post('/dashboard/api/remove_member')
@auth_middleware.auth_required()
def api_remove_member():
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        data = request.json
        group_id = data.get('group_id')
        member_id = data.get('member_id')

        if not group_id:
            abort_json(400, 'Group ID is required')
        if not member_id:
            abort_json(400, 'Member ID is required')

        from users import User, remove_group_member

        user = User.deserialize(member_id)
        if user is None:
            abort_json(400, f'Invalid member ID format: {member_id}')

        # メンバー削除（ログ記録）
        logging.info(f"メンバー削除: グループ {group_id} からメンバー {member_id} を削除します")
        remove_group_member(group_id, user)

        return utility.make_ok_json(
            f'グループ {group_id} からメンバーを削除しました',
            {
                'group_id': group_id,
                'member_id': member_id,
                'removed': True
            }
        )
    except HTTPError:
        raise
    except Exception as e:
        logging.error(f"Error removing member: {str(e)}")
        abort_json(500, f'Failed to remove member: {str(e)}')


@app.post('/dashboard/api/add_members')
@auth_middleware.auth_required()
def api_add_members():
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        data = request.json
        group_id = data.get('group_id')
        members_str = data.get('members', '').strip()

        if not group_id:
            abort_json(400, 'Group ID is required')
        if not members_str:
            abort_json(400, 'Members are required')

        from users import User, append_group_member

        # メンバーIDを改行で分割
        member_ids = [m.strip() for m in members_str.split('\n') if m.strip()]

        added_count = 0
        failed_count = 0
        failed_ids = []

        for member_id in member_ids:
            try:
                user = User.deserialize(member_id)
                if user is None:
                    logging.warning(f"Invalid member ID format skipped: {member_id} for group {group_id}")
                    failed_count += 1
                    failed_ids.append(member_id + " (Invalid Format)")
                    continue

                # 既存メンバーチェックは append_group_member 内で行われる想定
                logging.info(f"メンバー追加: グループ {group_id} にメンバー {member_id} を追加します")
                append_group_member(group_id, user)
                added_count += 1
            except Exception as e_inner:
                logging.error(f"Error adding member {member_id} to group {group_id}: {str(e_inner)}")
                failed_count += 1
                failed_ids.append(member_id + f" (Error: {str(e_inner)})")

        # utility.make_ok_json を使用してレスポンスを返す
        return utility.make_ok_json(
            f'グループ {group_id} に {added_count} 人のメンバーを追加試行しました。成功: {added_count}, 失敗: {failed_count}',
            {
                'group_id': group_id,
                'added_count': added_count,
                'failed_count': failed_count,
                'failed_ids': failed_ids
            }
        )

    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error in api_add_member: {str(e)}")
        abort_json(500, f'Failed to add member: {str(e)}')

@app.post('/dashboard/api/create_group_message_task')
@auth_middleware.auth_required()
def api_create_group_message_task():
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        data = request.json
        bot_name = data.get('bot_name')
        group_id = data.get('group_id')
        action = data.get('action')
        attrs = data.get('attrs', {})
        created_by = data.get('created_by', 'dashboard')
        scheduled_at_str = data.get('scheduled_at')

        if not bot_name or not group_id or not action:
            abort_json(400, 'Bot name, group ID, and action are required')

        scheduled_at = None
        if scheduled_at_str:
            try:
                # タイムゾーン指定を明示的に処理
                import pytz
                jst = pytz.timezone('Asia/Tokyo')

                # ISOフォーマット文字列をパース
                naive_dt = datetime.fromisoformat(scheduled_at_str)

                # JSTタイムゾーンを設定
                scheduled_at = jst.localize(naive_dt)

                logging.info(f"予約送信時刻を設定: {scheduled_at_str} → {scheduled_at.isoformat()}")
            except ValueError:
                abort_json(400, 'Invalid scheduled_at format. Use ISO 8601 format (YYYY-MM-DDTHH:MM:SS)')

        task_id = GroupMessageTaskDB.create_task(
            bot_name=bot_name,
            group_id=group_id,
            action=action,
            attrs=attrs,
            created_by=created_by,
            scheduled_at=scheduled_at
        )

        enqueue_succeeded = False
        try:
            task_client.create_task(
                queue_name='group-message-queue',
                url=f'/api/v1/bots/{bot_name}/process_group_batch',
                params={
                    'message_task_id': task_id,
                    'batch_index': 0
                }
            )
            enqueue_succeeded = True
            status_message = f'グループメッセージタスク {task_id} を作成し、処理を開始しました'
        except Exception as e_queue:
            logging.error(f"Failed to queue task {task_id} for immediate execution: {str(e_queue)}")
            status_message = f'グループメッセージタスク {task_id} を作成しましたが、即時実行の開始に失敗しました: {str(e_queue)}'
            # 必要であればここで500エラーを返すことも検討

        if scheduled_at and enqueue_succeeded:
            # scheduled_at が datetime オブジェクトの場合、表示用に文字列に変換
            scheduled_at_display = scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else scheduled_at_str
            status_message = f'グループメッセージタスク {task_id} を作成し、{scheduled_at_display} に実行予定です'

        # utility.make_ok_json を使う
        return utility.make_ok_json(
            status_message,
            {'task_id': task_id}
        )
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error creating/sending group message task: {str(e)}")
        abort_json(500, f'Failed to create group message task: {str(e)}')



@app.get('/dashboard/api/bots/<bot_name>/group_tasks')
@auth_middleware.auth_required()
def api_get_group_tasks(bot_name):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        limit = int(request.params.get('limit', 200))
        tasks = GroupMessageTaskDB.get_recent_tasks(bot_name, limit)

        if tasks is None:
            tasks = []
        else:
            # 日時オブジェクトをISO形式文字列に変換（日本時間を明示的に指定）
            import datetime, pytz
            jst = pytz.timezone('Asia/Tokyo')

            for task in tasks:
                # Firestore Timestamp を ISO 形式文字列に変換
                if 'created_at' in task:
                    if hasattr(task['created_at'], 'timestamp'):  # Firestore Timestampの場合
                        # UTCタイムスタンプをJST（日本時間）に変換
                        dt = datetime.datetime.fromtimestamp(task['created_at'].timestamp(), tz=jst)
                        task['created_at'] = dt.isoformat()
                    elif hasattr(task['created_at'], 'isoformat'):  # datetime型の場合
                        task['created_at'] = task['created_at'].isoformat()

                if 'updated_at' in task:
                    if hasattr(task['updated_at'], 'timestamp'):
                        dt = datetime.datetime.fromtimestamp(task['updated_at'].timestamp(), tz=jst)
                        task['updated_at'] = dt.isoformat()
                    elif hasattr(task['updated_at'], 'isoformat'):
                        task['updated_at'] = task['updated_at'].isoformat()

                if 'scheduled_at' in task:
                    if hasattr(task['scheduled_at'], 'timestamp'):
                        dt = datetime.datetime.fromtimestamp(task['scheduled_at'].timestamp(), tz=jst)
                        task['scheduled_at'] = dt.isoformat()
                    elif hasattr(task['scheduled_at'], 'isoformat'):
                        task['scheduled_at'] = task['scheduled_at'].isoformat()

        return utility.make_ok_json(
            f'{bot_name} のグループタスク一覧を取得しました ({len(tasks)} 件)',
            {'tasks': tasks}
        )
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error getting group tasks for {bot_name}: {str(e)}")
        abort_json(500, f'Failed to get group tasks: {str(e)}')


@app.get('/dashboard/api/group_tasks/<task_id>')
@auth_middleware.auth_required()
def api_get_group_task(task_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        task = GroupMessageTaskDB.get_task(task_id)
        if task:
            # 日時オブジェクトをISO形式文字列に変換 (JSONシリアライズ可能にするため)
            # 日本時間(JST)に変換
            import datetime, pytz
            jst = pytz.timezone('Asia/Tokyo')

            # Firestore Timestamp を ISO 形式文字列に変換
            if 'created_at' in task:
                if hasattr(task['created_at'], 'timestamp'):  # Firestore Timestampの場合
                    # UTCタイムスタンプをJST（日本時間）に変換
                    dt = datetime.datetime.fromtimestamp(task['created_at'].timestamp(), tz=jst)
                    task['created_at'] = dt.isoformat()
                elif hasattr(task['created_at'], 'isoformat'):  # datetime型の場合
                    task['created_at'] = task['created_at'].isoformat()

            if 'updated_at' in task:
                if hasattr(task['updated_at'], 'timestamp'):
                    dt = datetime.datetime.fromtimestamp(task['updated_at'].timestamp(), tz=jst)
                    task['updated_at'] = dt.isoformat()
                elif hasattr(task['updated_at'], 'isoformat'):
                    task['updated_at'] = task['updated_at'].isoformat()

            if 'scheduled_at' in task:
                if hasattr(task['scheduled_at'], 'timestamp'):
                    dt = datetime.datetime.fromtimestamp(task['scheduled_at'].timestamp(), tz=jst)
                    task['scheduled_at'] = dt.isoformat()
                elif hasattr(task['scheduled_at'], 'isoformat'):
                    task['scheduled_at'] = task['scheduled_at'].isoformat()

            # utility.make_ok_json を使うように修正 (他のAPIと形式を合わせる)
            return utility.make_ok_json(
                f'タスク {task_id} の情報を取得しました',
                {'task': task}
            )
        else:
            abort_json(404, 'Task not found')
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error getting group task {task_id}: {str(e)}")
        abort_json(500, f'Server error: {str(e)}')


@app.post('/dashboard/api/group_tasks/<task_id>/abort')
@auth_middleware.auth_required()
def api_abort_group_task(task_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        result = GroupMessageTaskDB.abort_task(task_id)
        if result:
            # utility.make_ok_json を使うように修正
            return utility.make_ok_json(
                f'タスク {task_id} を中止しました',
                {
                    'task_id': task_id,
                    'status': 'aborted'
                }
            )
        else:
            abort_json(400, 'failed to abort task or task not found')
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error aborting group task {task_id}: {str(e)}")
        abort_json(500, f'Server error: {str(e)}')


@app.post('/dashboard/api/group_tasks/<task_id>/retry_failed')
@auth_middleware.auth_required()
def api_retry_failed_group_task(task_id):
    response.set_header('Content-Type', 'application/json; charset=utf-8')

    try:
        created_by = 'dashboard_retry'

        new_task_id = GroupMessageTaskDB.retry_failed_members(task_id, created_by)

        if new_task_id:
            original_task = GroupMessageTaskDB.get_task(task_id)
            if not original_task:
                 abort_json(404, f'Original task {task_id} not found for retry')

            task_client.create_task(
                queue_name='group-message-queue',
                url=f'/api/v1/bots/{original_task["bot_name"]}/process_group_batch',
                params={
                    'message_task_id': new_task_id,
                    'batch_index': 0
                }
            )
            # utility.make_ok_json を使うように修正
            return utility.make_ok_json(
                 f'タスク {task_id} の失敗メンバーに対して新しいタスク {new_task_id} を作成しました',
                 {
                    'original_task_id': task_id,
                    'new_task_id': new_task_id,
                    'status': 'pending'
                 }
            )
        else:
            abort_json(400, 'no failed members to retry or task not found')
    except Exception as e:
        if isinstance(e, HTTPError):
            raise e
        logging.error(f"Error retrying failed group task {task_id}: {str(e)}")
        abort_json(500, f'Server error: {str(e)}')


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
