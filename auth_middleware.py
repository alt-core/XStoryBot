import logging
from functools import wraps

import firebase_admin
from bottle import HTTPError, request
from firebase_admin import auth, credentials

import settings


# Firebase Admin SDK を使ったページ認証のデコレータ

_app = None


def initialize():
    global _app
    if _app is None:
        cred = credentials.Certificate(settings.AUTH_SETTINGS['firebase_credentials_path'])
        _app = firebase_admin.initialize_app(cred)


def auth_required():
    """
    Firebase認証を必要とするエンドポイントのデコレータ
    settings.AUTH_SETTINGSのallowed_emailsで許可するメールアドレスを指定
    """
    allowed_emails = settings.AUTH_SETTINGS.get('allowed_emails', [])
    if not allowed_emails or not any(allowed_emails):
        logging.error("Security warning: No allowed emails configured")
        raise ValueError("allowed_emails must be configured in settings")

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Authorization ヘッダーからIDトークンを取得
            auth_header = request.headers.get('Authorization', '')
            if not auth_header.startswith('Bearer '):
                raise HTTPError(401, 'No token provided')

            id_token = auth_header.split('Bearer ')[1]

            try:
                # トークンの検証
                decoded_token = auth.verify_id_token(id_token)
            except auth.ExpiredIdTokenError:
                raise HTTPError(401, 'Expired token')
            except auth.RevokedIdTokenError:
                raise HTTPError(401, 'Revoked token')
            except auth.InvalidIdTokenError:
                raise HTTPError(401, 'Invalid token')
            except Exception:
                logging.exception('Firebase IDトークンの検証に失敗しました')
                raise HTTPError(401, 'Token verification failed')

            # メールアドレスの確認
            email = decoded_token.get('email')
            if not email or email not in allowed_emails:
                logging.warning('許可されていないアカウントからダッシュボードへアクセスされました')
                raise HTTPError(403, 'Unauthorized email')

            # リクエストにユーザー情報を追加
            request.firebase_user = decoded_token

            return func(*args, **kwargs)

        return wrapper
    return decorator
