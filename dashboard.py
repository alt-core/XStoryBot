# coding: utf-8
from bottle import request, response, Bottle, abort, route, view, redirect

from google.appengine.api import users

import settings


app = Bottle()


@app.get('/dashboard/')
@app.get('/dashboard/<bot_name>')
@view('template/dashboard')
def dashboard(bot_name=None):
    # アクセス権の確認
    user = users.get_current_user()
    ok = False
    if user and users.is_current_user_admin():
        ok = True
    if user and user.email() in settings.OPTIONS.get('admins', {}):
        ok = True
    if not ok:
        # TODO: ちゃんとアカウント系のエラーページを出す
        if user:
            redirect(users.create_logout_url('/dashboard/'))
        else:
            redirect(users.create_login_url('/dashboard/'))

    bot_list = sorted(settings.BOTS.keys(), key=lambda item: settings.BOTS[item]['name'])
    return dict(bot_name=bot_name or bot_list[0], bot_list=bot_list, bot_settings=settings.BOTS,
                logout_url=users.create_logout_url('/dashboard/'))


if __name__ == "__main__":
    app.run(host='localhost', port=8080, debug=True)
