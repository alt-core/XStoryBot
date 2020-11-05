# coding: utf-8

import os

#SERVER_NAME = os.getenv('SERVER_NAME', '')
DEPLOY_ENV = os.getenv('XSBOT_DEPLOY_ENV', '')

OPTIONS = {
    'api_token': u'<<YOUR API TOKEN>>',
    'admins': [],
    'reset_keyword': u'強制リセット',
    'timezone': 'Asia/Tokyo',
}

PLUGINS = {
    'line': {
        'line_abort_duration': 27,
        'alt_text': u'LINEアプリで確認してください。',
    },
    # 'line.more': {
    #     'command': [u'▽'],
    #     'image_url': 'https://example.com/more_button.png',
    #     'message': u'「続きを読む」',
    #     'action_pattern': ur'^「続きを読む」$',
    #     'ignore_pattern': ur'^「',
    #     'please_push_more_button_label': u'##please_push_more_button',
    # },
    # 'line.image_text': {
    #     'more_message': u'「続きを読む」',
    #     'more_image_url': 'https://example.com/more_button.png',
    #     'frames': {
    #         'default': {
    #             'size_x': 2080,
    #             'size_y': 2080,
    #             'margin_x': 90,
    #             'margin_y': 90,
    #         }
    #     },
    # },
    # 'render_text': {},
    'google_sheets': {},
    # 'twilio': {
    #     'twilio_sid': '<<TWILIO_SID>>',
    #     'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
    #     'dial_from': '<<TEL_FOR_DIAL>>',
    #     'sms_from': '<<TEL_FOR_SMS_SEND>>',
    # },
    # 'pusher': {
    #     'app_id': '<<PUSHER_APP_ID>>',
    #     'key': '<<PUSHER_APP_KEY>>',
    #     'secret': '<<PUSHER_APP_SECRET>>',
    #     'cluster': '<<PUSHER_APP_CLUSTER>>',
    # }
    # 'timestamp': {
    #     'timezone': 'Asia/Tokyo'
    # },
}


if DEPLOY_ENV == 'PROD':
    # リリース環境のサーバ設定
    BOTS = {
        'bot': {
            'name': 'My Bot',
            'description': '<div class="alert" style="font-weidht: bold; color: red; background: #cc88cc">これは本番環境です。更新時はご注意ください。</div>',
            'interfaces': [{
                'type': 'line',
                'params': {
                    'line_access_token': '<<LINE_ACCESS_TOKEN>>',
                    'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
                }
            }],
            'scenario': {
                'type': 'google_sheets',
                'params': {
                    # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
                    'sheet_id': "<<sheet_id>>",
                    # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
                    'key_file_json': 'path_to_keyfile_sheets_prod.json',
                }
            }
        },
    }
elif DEPLOY_ENV == 'DEV1':
    # 開発環境のサーバ設定
    BOTS = {
        'bot': {
            'name': 'My Bot',
            'description': '<div class="alert" style="font-weidht: bold; color: white; background: #88cc88">これは開発環境です。</div>',
            'interfaces': [{
                'type': 'line',
                'params': {
                    'line_access_token': '<<LINE_ACCESS_TOKEN>>',
                    'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
                }
            }],
            'scenario': {
                'type': 'google_sheets',
                'params': {
                    # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
                    'sheet_id': "<<sheet_id>>",
                    # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
                    'key_file_json': 'path_to_keyfile_sheets_dev.json',
                }
            }
        },
    }
else:
    # ローカルテストはこの設定を使う
    BOTS = {
        'bot': {
            'name': 'My Bot',
            'interfaces': [{
                'type': 'line',
                'params': {
                    'line_access_token': '<<LINE_ACCESS_TOKEN>>',
                    'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
                }
            }],
            'scenario': {
                'type': 'google_sheets',
                'params': {
                    # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
                    'sheet_id': "<<sheet_id>>",
                    # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
                    'key_file_json': 'path_to_keyfile_sheets_test.json',
                }
            }
        },
    }
