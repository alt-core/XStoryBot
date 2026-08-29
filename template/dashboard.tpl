<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@4.6.2/dist/css/bootstrap.min.css" integrity="sha384-xOolHFLEh07PJGoPkLv1IbcEPTNtaed2xpHsD9ESMhqIYd0nLMwNLD69Npy4HI+N" crossorigin="anonymous">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.10.0/font/bootstrap-icons.css">
    <style>
        body {
            padding-top: 5rem;
        }
        #tab {
            padding-bottom: 1rem;
        }
        #auth-dialog {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 9999;
            justify-content: center;
            align-items: center;
        }
        #auth-dialog.show {
            display: flex;
        }
        #auth-box {
            background: white;
            padding: 2rem;
            border-radius: 8px;
            text-align: center;
        }
        .main-content {
            display: none;
        }
        .main-content.show {
            display: block;
        }
    </style>

    <title>XStoryBot Dashboard</title>
</head>
<body>

<!-- 認証ダイアログ -->
<div id="auth-dialog" class="show">
    <div id="auth-box">
        <h2>ログインが必要です</h2>
        <form id="login-form">
            <div class="form-group text-left">
                <label for="login-username">ユーザー名</label>
                <input id="login-username" name="username" type="text" class="form-control" autocomplete="username" required>
            </div>
            <div class="form-group text-left">
                <label for="login-password">パスワード</label>
                <input id="login-password" name="password" type="password" class="form-control" autocomplete="current-password" required>
            </div>
            <button type="submit" id="login-button" class="btn btn-primary">ログイン</button>
        </form>
        <p id="auth-error" class="text-danger mt-3" role="alert"></p>
    </div>
</div>

<nav class="navbar navbar-expand-md navbar-dark fixed-top bg-dark">
    <a class="navbar-brand" href="#">XStoryBot Dashboard</a>
    <button class="navbar-toggler" type="button" data-toggle="collapse" data-target="#navbars" aria-controls="navbars" aria-expanded="false" aria-label="Toggle navigation">
        <span class="navbar-toggler-icon"></span>
    </button>

    <div class="collapse navbar-collapse" id="navbars">
        <ul class="navbar-nav mr-auto">
            <li class="nav-item active">
                <a class="nav-link" href="#">Home <span class="sr-only">(current)</span></a>
            </li>
        </ul>
        <ul class="navbar-nav navbar-right">
            <li class="nav-item">
                <span class="nav-link" id="user-email"></span>
            </li>
            <li class="nav-item">
                <a class="nav-link" href="#" id="logout-link">Logout</a>
            </li>
        </ul>
    </div>
</nav>

<main role="main" class="main-content">
    <div class="container">
        <header id="tab">
            <ul class="nav nav-tabs" id="bot-tabs">
            </ul>
        </header>

        <div class="row">
            <div class="col" id="bot-description"></div>
        </div>
        <div class="row">
            <div class="col">
                <h3>シナリオ修正の反映</h3>
                <p>
                    <button type="button" class="btn btn-danger" id="build_button">反映する</button>
                    <button type="button" class="btn btn-danger" id="quick_build_button">反映する（画像更新チェック省略）</button>
                    <br>
                    <br>
                    <button type="button" class="btn btn-danger" id="force_build_button">全データ強制変換</button>
                </p>
            </div>
        </div>
        <div class="row">
            <div class="col">
                <h3>処理結果</h3>
                <p>
                <div class="embed-responsive embed-responsive-21by9">
                    <textarea class="embed-responsive-item" id="build_result" style="border: 1px solid black"></textarea>
                </div>
                </p>
            </div>
        </div>

        <hr>

        <!-- グループ管理機能 -->
        <div class="row mt-4">
            <div class="col">
                <h3>グループ管理</h3>

                <ul class="nav nav-tabs" id="groupTabs" role="tablist">
                    <li class="nav-item">
                        <a class="nav-link active" id="groups-tab" data-toggle="tab" href="#groups-panel" role="tab">グループ一覧</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" id="add-group-tab" data-toggle="tab" href="#add-group-panel" role="tab">グループへの追加</a>
                    </li>
                </ul>

                <div class="tab-content mt-3" id="groupTabContent">
                    <!-- グループ一覧タブ -->
                    <div class="tab-pane fade show active" id="groups-panel" role="tabpanel">
                        <div class="card">
                            <div class="card-body">
                                <div class="table-responsive">
                                    <table class="table table-striped table-sm" id="group-table">
                                        <thead>
                                            <tr>
                                                <th>グループID</th>
                                                <th>メンバー数</th>
                                                <th>操作</th>
                                            </tr>
                                        </thead>
                                        <tbody id="group-list-body">
                                            <tr>
                                                <td colspan="3" class="text-center">認証後に読み込みます</td>
                                            </tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- グループへの追加タブ -->
                    <div class="tab-pane fade" id="add-group-panel" role="tabpanel">
                        <div class="card">
                            <div class="card-body">
                                <form id="add-group-form">
                                    <div class="form-group">
                                        <label for="group-id-input">グループID</label>
                                        <input type="text" class="form-control" id="group-id-input" required>
                                        <small class="form-text text-muted">存在しないグループIDを入力すると、新しいグループが作成されます</small>
                                    </div>
                                    <div class="form-group">
                                        <label for="new-member-ids-form">メンバーID (複数入力可)</label>
                                        <textarea class="form-control" id="new-member-ids-form" rows="5" required></textarea>
                                        <small class="form-text text-muted">LINE IDなどを改行区切りで入力してください</small>
                                    </div>
                                    <button type="submit" class="btn btn-primary">メンバー追加</button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- タスク管理 -->
        <div class="row mt-4">
            <div class="col">
                <h3>タスク管理</h3>

                <ul class="nav nav-tabs" id="taskTabs" role="tablist">
                    <li class="nav-item">
                        <a class="nav-link active" id="task-list-tab" data-toggle="tab" href="#task-list-panel" role="tab">タスク一覧</a>
                    </li>
                    <li class="nav-item">
                        <a class="nav-link" id="create-task-tab" data-toggle="tab" href="#create-task-panel" role="tab">送信タスク作成</a>
                    </li>
                </ul>

                <div class="tab-content mt-3" id="taskTabContent">
                    <!-- タスク一覧タブ -->
                    <div class="tab-pane fade show active" id="task-list-panel" role="tabpanel">
                        <div class="card">
                            <div class="card-body">
                                <div class="table-responsive">
                                    <table class="table table-striped table-sm" id="task-table">
                                        <thead>
                                            <tr>
                                                <th>タスクID</th>
                                                <th>グループ</th>
                                                <th>アクション</th>
                                                <th>ステータス</th>
                                                <th>予約日時</th>
                                                <th>操作</th>
                                            </tr>
                                        </thead>
                                        <tbody id="task-list-body">
                                            <!-- タスク一覧はJavaScriptで非同期に読み込まれます -->
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- 送信タスク作成タブ -->
                    <div class="tab-pane fade" id="create-task-panel" role="tabpanel">
                        <div class="card">
                            <div class="card-body">
                                <form id="group-message-form">
                                    <div class="form-group">
                                        <label for="group-select">送信先グループ</label>
                                        <select class="form-control" id="group-select" required>
                                            <option value="">選択してください</option>
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label for="message-action">メッセージアクション</label>
                                        <input type="text" class="form-control" id="message-action" required>
                                        <small class="form-text text-muted">送信するメッセージアクションを入力してください</small>
                                    </div>
                                    <div class="form-group">
                                        <label for="schedule-date">予約送信日時</label>
                                        <input type="text" class="form-control" id="schedule-date" placeholder="2025-03-29 22:30:00">
                                        <small class="form-text text-muted">予約送信する場合は日時を指定してください（形式: YYYY-MM-DD HH:MM:SS）</small>
                                    </div>
                                    <div class="form-group form-check">
                                        <input type="checkbox" class="form-check-input" id="immediate-send">
                                        <label class="form-check-label" for="immediate-send">即時送信</label>
                                        <small class="form-text text-muted">チェックを入れると即時送信します。予約日時は無視されます。</small>
                                    </div>
                                    <button type="submit" class="btn btn-primary">送信開始</button>
                                </form>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- タスク詳細モーダル -->
        <div class="modal fade" id="task-detail-modal" tabindex="-1" role="dialog">
            <div class="modal-dialog modal-lg" role="document">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">タスク詳細</h5>
                        <button type="button" class="close" data-dismiss="modal" aria-label="Close">
                            <span aria-hidden="true">&times;</span>
                        </button>
                    </div>
                    <div class="modal-body">
                        <div class="task-progress-container mb-3">
                            <h6>進捗状況</h6>
                            <div class="progress">
                                <div class="progress-bar" id="task-progress-bar" role="progressbar" style="width: 0%"></div>
                            </div>
                            <div class="d-flex justify-content-between mt-1">
                                <small id="task-progress-text">0/0 (0%)</small>
                                <small id="task-status-text">ステータス: -</small>
                            </div>
                        </div>

                        <div class="row">
                            <div class="col-md-6">
                                <h6>基本情報</h6>
                                <table class="table table-sm">
                                    <tr>
                                        <th>タスクID</th>
                                        <td id="detail-task-id"></td>
                                    </tr>
                                    <tr>
                                        <th>グループID</th>
                                        <td id="detail-group-id"></td>
                                    </tr>
                                    <tr>
                                        <th>アクション</th>
                                        <td id="detail-action"></td>
                                    </tr>
                                    <tr>
                                        <th>作成者</th>
                                        <td id="detail-created-by"></td>
                                    </tr>
                                    <tr>
                                        <th>予約日時</th>
                                        <td id="detail-scheduled-at">-</td>
                                    </tr>
                                    <tr>
                                        <th>作成日時</th>
                                        <td id="detail-created-at"></td>
                                    </tr>
                                </table>
                            </div>
                            <div class="col-md-6">
                                <h6>処理状況</h6>
                                <table class="table table-sm">
                                    <tr>
                                        <th>処理済み</th>
                                        <td id="detail-processed"></td>
                                    </tr>
                                    <tr>
                                        <th>成功</th>
                                        <td id="detail-successful"></td>
                                    </tr>
                                    <tr>
                                        <th>失敗</th>
                                        <td id="detail-failed"></td>
                                    </tr>
                                    <tr>
                                        <th>現在のバッチ</th>
                                        <td id="detail-current-batch"></td>
                                    </tr>
                                    <tr>
                                        <th>送信間隔</th>
                                        <td id="detail-interval"></td>
                                    </tr>
                                </table>
                            </div>
                        </div>

                        <div class="mt-3">
                            <h6>エラーメッセージ</h6>
                            <div class="border p-2 bg-light" style="max-height: 200px; overflow-y: auto;">
                                <ul id="detail-errors" class="list-unstyled mb-0">
                                    <li class="text-muted">エラーはありません</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-dismiss="modal">閉じる</button>
                        <button type="button" class="btn btn-danger" id="detail-abort-button">中止</button>
                        <button type="button" class="btn btn-warning" id="detail-retry-button">エラーのみ再送</button>
                    </div>
                </div>
            </div>
        </div>

        <hr>

    </div>
</main>

<footer class="container">
    <p>&copy; alt-core 2018</p>
</footer>

<script src="https://code.jquery.com/jquery-3.7.1.min.js" integrity="sha256-/JqT3SQfawRcv/BIHPThkBvs0OEvtFFmqPF/lYI/Cxo=" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/popper.js@1.16.1/dist/umd/popper.min.js" integrity="sha384-9/reFTGAW83EW2RDu2S0VKaIzap3H66lZH81PoYlFhbGU+6BZp6G7niu735Sk7lN" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@4.6.2/dist/js/bootstrap.min.js" integrity="sha384-+sLIOodYLS7CIrQpBjl+C7nPvqq+FbNUBDunl/OZv93DB7Ln/533i8e/mZXLi/P+" crossorigin="anonymous"></script>

<script type="text/javascript">
    const initialBotName = {{!initial_bot_name_json}};

    let csrfToken = null;
    let pollingInterval = null;
    let lastBuildTaskId = null;
    let botName = null;

    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function(character) {
            return {
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                '"': '&quot;',
                "'": '&#39;'
            }[character];
        });
    }

    function renderBotNavigation(bots) {
        const tabs = $('#bot-tabs');
        tabs.empty();
        bots.forEach(function(bot) {
            const link = $('<a>')
                .addClass('nav-link' + (bot.id === botName ? ' active' : ''))
                .attr('href', '/dashboard/' + bot.id)
                .text(bot.name);
            tabs.append($('<li>').addClass('nav-item').append(link));
        });
    }

    function renderGroups(groups) {
        const groupSelect = $('#group-select');
        groupSelect.empty().append(
            $('<option>').attr('value', '').text('選択してください')
        );

        let groupRows = '';
        if (!groups || groups.length === 0) {
            groupRows = '<tr><td colspan="3" class="text-center">グループはありません</td></tr>';
        } else {
            groups.forEach(function(group) {
                const groupId = group.id || '';
                const safeGroupId = escapeHtml(groupId);
                groupRows += `
                    <tr class="group-row" data-group-id="${safeGroupId}">
                        <td>${safeGroupId}</td>
                        <td id="member-count-${safeGroupId}">
                            <span class="badge badge-secondary">読込中...</span>
                        </td>
                        <td>
                            <button class="btn btn-sm btn-outline-info view-members" data-group-id="${safeGroupId}">メンバー表示</button>
                            <button class="btn btn-sm btn-outline-primary select-group" data-group-id="${safeGroupId}">選択</button>
                        </td>
                    </tr>
                `;
                groupSelect.append(
                    $('<option>').attr('value', groupId).text(groupId)
                );
            });
        }
        $('#group-list-body').html(groupRows);
    }

    async function loadGroups() {
        const response = await $.ajax({
            url: '/dashboard/api/groups',
            type: 'GET',
            dataType: 'json'
        });
        renderGroups(response.groups || []);
    }

    async function loadDashboardConfig() {
        const response = await $.ajax({
            url: '/dashboard/api/config',
            type: 'GET',
            dataType: 'json'
        });
        const bots = response.data.bots || [];
        if (bots.length === 0) {
            throw new Error('利用できるBotがありません');
        }

        let selectedBot = bots.find(function(bot) {
            return bot.id === initialBotName;
        });
        if (!selectedBot) {
            selectedBot = bots[0];
        }
        botName = selectedBot.id;
        renderBotNavigation(bots);
        $('#bot-description').html(selectedBot.description || '');
        $('#user-email').text(response.data.user_email || '');
        csrfToken = response.data.csrf_token || null;

        try {
            await loadGroups();
        } catch (error) {
            console.error('Failed to get groups:', error);
            renderGroups([]);
        }
        updateTaskList();
        loadMemberCounts();
    }

    async function initializeDashboard() {
        try {
            await loadDashboardConfig();
            $('#auth-error').text('');
            $('#auth-dialog').removeClass('show');
            $('.main-content').addClass('show');
        } catch (error) {
            csrfToken = null;
            $('#auth-dialog').addClass('show');
            $('.main-content').removeClass('show');
        }
    }

    $('#login-form').submit(async function(event) {
        event.preventDefault();
        $('#auth-error').text('');
        try {
            await $.ajax({
                url: '/dashboard/login',
                type: 'POST',
                dataType: 'json',
                data: {
                    username: $('#login-username').val(),
                    password: $('#login-password').val()
                }
            });
            $('#login-password').val('');
            await initializeDashboard();
        } catch (error) {
            $('#auth-error').text('ユーザー名またはパスワードが正しくありません');
        }
    });

    $('#logout-link').click(async function(event) {
        event.preventDefault();
        if (csrfToken) {
            await $.ajax({
                url: '/dashboard/logout',
                type: 'POST',
                headers: {'X-CSRF-Token': csrfToken}
            });
        }
        csrfToken = null;
        $('.main-content').removeClass('show');
        $('#auth-dialog').addClass('show');
    });

    // グループメンバーカウントの取得
    function loadMemberCounts() {
        $('.group-row').each(function() {
            const groupId = $(this).data('group-id');
            fetchMemberCount(groupId);
        });
    }

    function fetchMemberCount(groupId) {
        $.ajax({
            url: `/dashboard/api/group_members/${groupId}`,
            type: 'GET',
            dataType: 'json',
            success: function(data) {
                if (data.code === 200 && data.data) {
                    $(`#member-count-${groupId}`).html(`<span class="badge badge-primary">${data.data.count}名</span>`);
                } else {
                     $(`#member-count-${groupId}`).html(`<span class="badge badge-warning">取得失敗</span>`);
                }
            },
            error: function() {
                $(`#member-count-${groupId}`).html(`<span class="badge badge-danger">エラー</span>`);
            }
        });
    }

    // APIリクエスト関数
    function request_build(endpoint, options) {
        if (!csrfToken) {
            alert('認証が必要です');
            return;
        }
        var textarea = $("#build_result")
        textarea.val('反映作業を開始します...');

        $.ajax({
            url: endpoint,
            type: 'POST',
            dataType: 'json',
            headers: {'X-CSRF-Token': csrfToken},
            data: options
        })
            .done(function(resp) {
                console.log(resp);
                if (resp.message === 'Queued') {
                    textarea.val('反映作業を開始しました...');
                    var task_id = resp.task_id;
                    // 最後のビルド結果をポーリングして結果を取得
                    if (pollingInterval) {
                        clearInterval(pollingInterval);
                    }
                    lastBuildTaskId = null;
                    pollingInterval = setInterval(function() {
                        $.ajax({
                            url: '/dashboard/last_build_result/' + botName,
                            type: 'GET',
                            dataType: 'json'
                        }).done(function(result) {
                            console.log(result);
                            if (result.task_id === task_id) {
                                // 結果が出た
                                var status = result.status;
                                if (status === 'Success') {
                                    textarea.val('反映作業が完了しました');
                                } else {
                                    textarea.val('反映作業が失敗しました\n' + (result.error || '不明なエラーが発生しました'));
                                }
                                clearInterval(pollingInterval);
                            } else {
                                // 反映作業中？
                                if (lastBuildTaskId == null) {
                                    lastBuildTaskId = result.task_id;
                                } else if (lastBuildTaskId != result.task_id) {
                                    // 知らないタスクIDに変わった
                                    textarea.val('別のタスクに割り込まれたようです。再度実行してください。');
                                    clearInterval(pollingInterval);
                                    return;
                                }
                                // 呼び出されるたびに . の数を1～3で変化させる
                                var dots = textarea.val().match(/\./g);
                                var dotCount = dots ? dots.length : 0;
                                dotCount = (dotCount % 3) + 1;
                                textarea.val('反映作業中' + '.'.repeat(dotCount));
                            }
                        });
                    }, 5000); // Check every 5 seconds
                } else {
                    textarea.val(resp.message);
                }
            })
            .fail(function(jqXHR) {
                if (jqXHR.status === 401 || jqXHR.status === 403) {
                    textarea.val('認証エラー: アクセス権限がありません');
                } else {
                    errorMessage = '';
                    // responseText に <pre> タグがあったらその中身を抽出する
                    var match = jqXHR.responseText.match(/<pre>([\s\S]+)<\/pre>/);
                    if (match) {
                        errorMessage = match[1];
                    } else {
                        errorMessage = jqXHR.responseText;
                    }
                    textarea.val('反映作業の開始に失敗しました\n' + errorMessage);
                }
        });
    }

    function formatDateTime(timestamp) {
        if (!timestamp) return '-';

        let date;

        if (typeof timestamp === 'string') {
            // ISOフォーマット文字列の場合
            date = new Date(timestamp);
        } else if (timestamp.seconds !== undefined) {
            // secondsを持つタイムスタンプの場合
            date = new Date(timestamp.seconds * 1000);
        } else if (timestamp.getTime) {
            // Dateオブジェクトの場合
            date = timestamp;
        } else {
            // その他の形式（数値など）
            date = new Date(timestamp);
        }

        // Invalid Dateチェック
        if (isNaN(date.getTime())) {
            console.error('Invalid date from input:', timestamp);
            return '-';
        }

        return date.toLocaleString('ja-JP');
    }

    function updateTaskList() {
        if (!csrfToken) {
            console.error('Cannot update task list: No session available');
            return;
        }

        $.ajax({
            url: `/dashboard/api/bots/${botName}/group_tasks`,
            type: 'GET',
            data: {
                limit: 200
            }
        }).done(function(resp) {
            const tasks = resp.data.tasks;
            let html = '';

            if (!tasks || tasks.length === 0) { // tasks が存在しない場合も考慮
                html = '<tr><td colspan="6" class="text-center">タスクはありません</td></tr>';
            } else {
                    tasks.forEach(task => {
                    const id = task.id;
                    const group = task.group_id;
                    const status = task.status;
                    const progress = `${task.processed_members}/${task.total_members}`;
                    const percent = task.total_members > 0 ? Math.round(task.processed_members / task.total_members * 100) : 0;
                    const scheduledAt = task.scheduled_at ? formatDateTime(task.scheduled_at) : '-';
                    const safeId = escapeHtml(id);
                    const safeGroup = escapeHtml(group);
                    const safeProgress = escapeHtml(progress);
                    const safeScheduledAt = escapeHtml(scheduledAt);

                    let statusClass = '';
                    let statusText = status;

                    switch (status) {
                        case 'pending':
                            statusClass = 'text-secondary';
                            statusText = '待機中';
                            break;
                        case 'running':
                            statusClass = 'text-primary';
                            statusText = '実行中';
                            break;
                        case 'completed':
                            statusClass = 'text-success';
                            statusText = '完了';
                            break;
                        case 'failed':
                            statusClass = 'text-danger';
                            statusText = '失敗';
                            break;
                        case 'aborted':
                            statusClass = 'text-warning';
                            statusText = '中止';
                            break;
                    }

                    html += `
                        <tr>
                            <td><a href="#" class="task-detail" data-task-id="${safeId}">${escapeHtml(id.substring(0, 8))}...</a></td>
                            <td>${safeGroup}</td>
                            <td class="${statusClass}">${escapeHtml(statusText)}</td>
                            <td>
                                <div class="progress">
                                    <div class="progress-bar" role="progressbar" style="width: ${percent}%" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100"></div>
                                </div>
                                <small>${safeProgress} (${percent}%)</small>
                            </td>
                            <td>${safeScheduledAt}</td>
                            <td>
                                <div class="btn-group btn-group-sm">
                                    <button class="btn btn-outline-info task-detail" data-task-id="${safeId}">詳細</button>
                                    ${status === 'running' || status === 'pending' ? `<button class="btn btn-outline-danger task-abort" data-task-id="${safeId}">中止</button>` : ''}
                                    ${(status === 'completed' || status === 'failed' || status === 'aborted') && task.failed_members > 0 ? `<button class="btn btn-outline-warning task-retry" data-task-id="${safeId}">再送</button>` : ''}
                                </div>
                            </td>
                        </tr>
                    `;
                });
            }

            $('#task-list-body').html(html);

            // イベントハンドラの設定
            $('.task-detail').click(function(e) {
                e.preventDefault();
                const taskId = $(this).data('task-id');
                showTaskDetail(taskId);
            });

            $('.task-abort').click(function(e) {
                e.preventDefault();
                if (confirm('タスクを中止してもよろしいですか？')) {
                    const taskId = $(this).data('task-id');
                    abortTask(taskId);
                }
            });

            $('.task-retry').click(function(e) {
                e.preventDefault();
                if (confirm('失敗したメンバーに再送信しますか？')) {
                    const taskId = $(this).data('task-id');
                    retryFailedMembers(taskId);
                }
            });

        }).fail(function(err) {
            $('#task-list-body').html('<tr><td colspan="6" class="text-center text-danger">タスク一覧の取得に失敗しました</td></tr>');
            console.error('Failed to get task list:', err);
        });
    }

    function showTaskDetail(taskId) {
        $.ajax({
            url: `/dashboard/api/group_tasks/${taskId}`,
            type: 'GET'
        }).done(function(resp) {
            const task = resp.data.task;

            // プログレスバー更新
            const percent = task.total_members > 0 ? Math.round(task.processed_members / task.total_members * 100) : 0;
            $('#task-progress-bar').css('width', `${percent}%`);
            $('#task-progress-text').text(`${task.processed_members}/${task.total_members} (${percent}%)`);

            // ステータス表示
            let statusText = '';

            switch (task.status) {
                case 'pending':
                    statusText = '待機中';
                    $('#task-progress-bar').addClass('bg-secondary').removeClass('bg-primary bg-success bg-danger bg-warning');
                    break;
                case 'running':
                    statusText = '実行中';
                    $('#task-progress-bar').addClass('bg-primary').removeClass('bg-secondary bg-success bg-danger bg-warning');
                    break;
                case 'completed':
                    statusText = '完了';
                    $('#task-progress-bar').addClass('bg-success').removeClass('bg-secondary bg-primary bg-danger bg-warning');
                    break;
                case 'failed':
                    statusText = '失敗';
                    $('#task-progress-bar').addClass('bg-danger').removeClass('bg-secondary bg-primary bg-success bg-warning');
                    break;
                case 'aborted':
                    statusText = '中止';
                    $('#task-progress-bar').addClass('bg-warning').removeClass('bg-secondary bg-primary bg-success bg-danger');
                    break;
            }

            $('#task-status-text').text(`ステータス: ${statusText}`);

            // 基本情報
            $('#detail-task-id').text(task.id);
            $('#detail-group-id').text(task.group_id);
            $('#detail-action').text(task.action);
            $('#detail-created-by').text(task.created_by);
            $('#detail-scheduled-at').text(task.scheduled_at ? formatDateTime(task.scheduled_at) : '-');
            $('#detail-created-at').text(formatDateTime(task.created_at));

            // 処理状況
            $('#detail-processed').text(`${task.processed_members} / ${task.total_members}`);
            $('#detail-successful').text(task.successful_members);
            $('#detail-failed').text(task.failed_members);
            $('#detail-current-batch').text(`${task.current_batch} / ${task.total_batches}`);
            $('#detail-interval').text(`${task.interval_ms}ms`);

            // エラーメッセージ
            const errors = task.error_messages || [];
            if (errors.length > 0) {
                let errorHtml = '';
                errors.forEach(error => {
                    errorHtml += `<li class="text-danger">${escapeHtml(error)}</li>`;
                });
                $('#detail-errors').html(errorHtml);
            } else {
                $('#detail-errors').html('<li class="text-muted">エラーはありません</li>');
            }

            // ボタン制御
            $('#detail-abort-button').prop('disabled', task.status !== 'running' && task.status !== 'pending');
            $('#detail-retry-button').prop('disabled', task.failed_members === 0 || task.status === 'pending' || task.status === 'running');

            // タスクIDを保存
            $('#detail-abort-button').data('task-id', task.id);
            $('#detail-retry-button').data('task-id', task.id);

            // モーダル表示
            $('#task-detail-modal').modal('show');
        }).fail(function(err) {
            alert('タスク詳細の取得に失敗しました');
            console.error('Failed to get task detail:', err);
        });
    }

    function abortTask(taskId) {
        $.ajax({
            url: `/dashboard/api/group_tasks/${taskId}/abort`,
            type: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        }).done(function(resp) {
            alert(`タスク ${taskId} を中止しました`);
            updateTaskList();

            // モーダルが開いている場合は更新
            if ($('#task-detail-modal').is(':visible')) {
                showTaskDetail(taskId);
            }
        }).fail(function(err) {
            alert('タスクの中止に失敗しました');
            console.error('Failed to abort task:', err);
        });
    }

    function retryFailedMembers(taskId) {
        $.ajax({
            url: `/dashboard/api/group_tasks/${taskId}/retry_failed`,
            type: 'POST',
            headers: {'X-CSRF-Token': csrfToken}
        }).done(function(resp) {
            alert(`失敗したメンバーへの再送信タスクを作成しました。新しいタスクID: ${resp.data.new_task_id}`);
            updateTaskList();
            $('#task-detail-modal').modal('hide');
        }).fail(function(err) {
            alert('再送信タスクの作成に失敗しました');
            console.error('Failed to retry failed members:', err);
        });
    }


    // ポーリング
    let taskPollingInterval = null;

    function startTaskPolling() {
        stopTaskPolling();
        taskPollingInterval = setInterval(function() {
            if ($('#task-detail-modal').is(':visible')) {
                const taskId = $('#detail-task-id').text();
                if (taskId) {
                        $.ajax({
                            url: `/dashboard/api/group_tasks/${taskId}`,
                            type: 'GET'
                    }).done(function(resp) {
                        const task = resp.data.task;

                        // プログレスバー更新
                        const percent = task.total_members > 0 ? Math.round(task.processed_members / task.total_members * 100) : 0;
                        $('#task-progress-bar').css('width', `${percent}%`);
                        $('#task-progress-text').text(`${task.processed_members}/${task.total_members} (${percent}%)`);

                        // 処理状況更新
                        $('#detail-processed').text(`${task.processed_members} / ${task.total_members}`);
                        $('#detail-successful').text(task.successful_members);
                        $('#detail-failed').text(task.failed_members);
                        $('#detail-current-batch').text(`${task.current_batch} / ${task.total_batches}`);
                        $('#detail-interval').text(`${task.interval_ms}ms`);

                        // 予約日時の更新
                        $('#detail-scheduled-at').text(task.scheduled_at ? formatDateTime(task.scheduled_at) : '-');

                        // ステータス表示
                        let statusText = '';

                        switch (task.status) {
                            case 'pending':
                                statusText = '待機中';
                                $('#task-progress-bar').addClass('bg-secondary').removeClass('bg-primary bg-success bg-danger bg-warning');
                                break;
                            case 'running':
                                statusText = '実行中';
                                $('#task-progress-bar').addClass('bg-primary').removeClass('bg-secondary bg-success bg-danger bg-warning');
                                break;
                            case 'completed':
                                statusText = '完了';
                                $('#task-progress-bar').addClass('bg-success').removeClass('bg-secondary bg-primary bg-danger bg-warning');
                                break;
                            case 'failed':
                                statusText = '失敗';
                                $('#task-progress-bar').addClass('bg-danger').removeClass('bg-secondary bg-primary bg-success bg-warning');
                                break;
                            case 'aborted':
                                statusText = '中止';
                                $('#task-progress-bar').addClass('bg-warning').removeClass('bg-secondary bg-primary bg-success bg-danger');
                                break;
                        }

                        $('#task-status-text').text(`ステータス: ${statusText}`);

                        // エラーメッセージ
                        const errors = task.error_messages || [];
                        if (errors.length > 0) {
                            let errorHtml = '';
                            errors.forEach(error => {
                                errorHtml += `<li class="text-danger">${escapeHtml(error)}</li>`;
                            });
                            $('#detail-errors').html(errorHtml);
                        }

                        // ボタン制御
                        $('#detail-abort-button').prop('disabled', task.status !== 'running' && task.status !== 'pending');
                        $('#detail-retry-button').prop('disabled', task.failed_members === 0 || task.status === 'pending' || task.status === 'running');
                    });
                }
            }
        }, 3000); // 3秒ごとに更新
    }

    function stopTaskPolling() {
        if (taskPollingInterval) {
            clearInterval(taskPollingInterval);
            taskPollingInterval = null;
        }
    }

    $(function(){
        initializeDashboard();

        function buildEndpoint() {
            return `/dashboard/build_async/${botName}`;
        }

        // ビルドボタンのイベントハンドラ
        $("#build_button").on("click",function(){
            request_build(buildEndpoint(), {})
        });

        $("#force_build_button").on("click",function(){
            request_build(buildEndpoint(), {'force': 'true'})
        });

        $("#quick_build_button").on("click",function(){
            request_build(buildEndpoint(), {'skip_image': 'true'})
        });

        // グループ選択ボタン
        $(document).on("click", ".select-group", function() {
            const groupId = $(this).data('group-id');
            // グループIDを入力フィールドに設定
            $("#group-id-input").val(groupId);
            // グループへの追加タブに切り替え
            $('#add-group-tab').tab('show');
        });

        // グループへの追加フォーム
        $("#add-group-form").on("submit", function(e) {
            e.preventDefault();

            const groupId = $("#group-id-input").val();
            const membersStr = $("#new-member-ids-form").val();

            if (!groupId || groupId.trim() === '') {
                alert('グループIDを入力してください');
                return;
            }

            if (!membersStr || membersStr.trim() === '') {
                alert('メンバーIDを入力してください');
                return;
            }

            $.ajax({
                url: '/dashboard/api/add_members',
                type: 'POST',
                dataType: 'json',
                contentType: 'application/json',
                data: JSON.stringify({
                    group_id: groupId,
                    members: membersStr
                }),
                headers: {
                    'X-CSRF-Token': csrfToken
                },
                success: function(data) {
                    if (data.code === 200) {
                        let message = data.message || 'メンバー追加処理が完了しました。';
                        if (data.data && data.data.failed_count > 0) {
                            message += `\n失敗: ${data.data.failed_count}件`;
                        }
                        alert(message);
                        // フォームをリセット
                        $("#new-member-ids-form").val('');
                        // メンバー数表示を更新（グループ一覧に存在する場合）
                        if ($(`#member-count-${groupId}`).length) {
                            fetchMemberCount(groupId);
                        } else {
                            // 新しいグループが作成された場合は一覧を更新（ページリロード）
                            location.reload();
                        }
                    } else {
                        alert('エラー: ' + (data.message || 'メンバー追加に失敗しました'));
                    }
                },
                error: function(xhr) {
                    let errorMsg = 'メンバー追加に失敗しました';
                    try {
                        const errResp = JSON.parse(xhr.responseText);
                        errorMsg = errResp.message || errorMsg;
                    } catch (e) {}
                    alert('エラー: ' + errorMsg);
                }
            });
        });

        // メンバー表示ボタン
        $(document).on("click", ".view-members", function() {
            const groupId = $(this).data('group-id');

        $.ajax({
            url: '/dashboard/api/group_members/' + groupId,
            type: 'GET',
            dataType: 'json',
                success: function(data) {
                    if (data.code === 200 && data.data) {
                        const members = data.data.members;
                        let memberList = '';

                        if (members && members.length > 0) {
                            members.forEach(function(member) {
                                const safeMember = escapeHtml(member);
                                const safeGroupId = escapeHtml(groupId);
                                memberList += `
                                    <li class="list-group-item d-flex justify-content-between align-items-center">
                                        <span>${safeMember}</span>
                                        <button class="btn btn-sm btn-outline-danger delete-member" data-member-id="${safeMember}" data-group-id="${safeGroupId}" title="メンバーを削除">
                                            <i class="bi bi-trash"></i>
                                        </button>
                                    </li>`;
                            });
                        } else {
                            memberList = '<li class="list-group-item">メンバーはいません</li>';
                        }

                        $("#members-list").html(memberList);
                        $("#view-members-modal").modal('show');
                    } else {
                        alert('エラー: ' + (data.message || 'メンバー取得に失敗しました'));
                    }
                },
                error: function() {
                    alert('メンバー取得に失敗しました');
                }
            });
        });



        $('#group-message-form').submit(function(e) {
            e.preventDefault();
            const groupId = $('#group-select').val();
            const action = $('#message-action').val();
            const scheduledTime = $("#schedule-date").val();
            const isImmediate = $("#immediate-send").prop('checked');

            // 即時送信か予約送信のいずれかが必要
            if (!scheduledTime && !isImmediate) {
                alert('予約送信日時を指定するか、即時送信にチェックを入れてください');
                return;
            }

            const taskData = {
                bot_name: botName,
                group_id: groupId,
                action: action,
                created_by: $('#user-email').text() || 'dashboard',
                scheduled_at: isImmediate ? null : scheduledTime // 即時送信の場合は予約日時を無視
            };

            let confirmMessage = `グループ ${groupId} に「${action}」を`;
            if (isImmediate) {
                confirmMessage += '即時送信しますか？';
            } else {
                confirmMessage += `${scheduledTime}に予約送信しますか？`;
            }

            if (confirm(confirmMessage)) {
                 $.ajax({
                    url: `/dashboard/api/create_group_message_task`,
                    type: 'POST',
                    contentType: 'application/json',
                    data: JSON.stringify(taskData),
                    headers: {
                        'X-CSRF-Token': csrfToken
                    }
                }).done(function(resp) {
                    alert(resp.message || `タスク ${resp.data.task_id} を作成しました`);
                    updateTaskList();
                    $('#group-message-form')[0].reset(); // フォームリセット
                }).fail(function(err) {
                    let errorMsg = 'タスク作成に失敗しました';
                     try {
                        const response = JSON.parse(err.responseText);
                        errorMsg = response.message || errorMsg;
                    } catch (e) {}
                    alert('エラー: ' + errorMsg);
                    console.error('Failed to create group message task:', err);
                });
            }
        });

        $('#detail-abort-button').click(function() {
            if (confirm('タスクを中止してもよろしいですか？')) {
                const taskId = $(this).data('task-id');
                abortTask(taskId);
            }
        });

        $('#detail-retry-button').click(function() {
            if (confirm('失敗したメンバーに再送信しますか？')) {
                const taskId = $(this).data('task-id');
                retryFailedMembers(taskId);
            }
        });

        $('#task-detail-modal').on('shown.bs.modal', function() {
            startTaskPolling();
        });

        $('#task-detail-modal').on('hidden.bs.modal', function() {
            stopTaskPolling();
            updateTaskList(); // モーダルを閉じたらリスト更新
        });

        // メンバー削除ボタンのイベントハンドラ
        $(document).on('click', '.delete-member', function() {
            const memberId = $(this).data('member-id');
            const groupId = $(this).data('group-id');

            if (confirm(`グループ ${groupId} からメンバー ${memberId} を削除してもよろしいですか？`)) {
                $.ajax({
                    url: '/dashboard/api/remove_member',
                    type: 'POST',
                    contentType: 'application/json',
                    data: JSON.stringify({
                        group_id: groupId,
                        member_id: memberId
                    }),
                    headers: {
                        'X-CSRF-Token': csrfToken
                    }
                }).done(function(resp) {
                    if (resp.code === 200) {
                        // メンバーリストから該当の行を削除
                        $(`.delete-member[data-member-id="${memberId}"][data-group-id="${groupId}"]`).closest('li').remove();

                        // メンバー数表示を更新
                        fetchMemberCount(groupId);

                        // リストが空になったら表示を更新
                        if ($('#members-list li').length === 0) {
                            $('#members-list').html('<li class="list-group-item">メンバーはいません</li>');
                        }
                    } else {
                        alert('エラー: ' + (resp.message || 'メンバー削除に失敗しました'));
                    }
                }).fail(function(err) {
                    let errorMsg = 'メンバー削除に失敗しました';
                    try {
                        const response = JSON.parse(err.responseText);
                        errorMsg = response.message || errorMsg;
                    } catch (e) {}
                    alert('エラー: ' + errorMsg);
                });
            }
        });
    });
</script>




<!-- メンバー表示モーダル -->
<div class="modal fade" id="view-members-modal" tabindex="-1" role="dialog">
    <div class="modal-dialog" role="document">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">グループメンバー</h5>
                <button type="button" class="close" data-dismiss="modal" aria-label="Close">
                    <span aria-hidden="true">&times;</span>
                </button>
            </div>
            <div class="modal-body">
                <ul class="list-group" id="members-list">
                    <!-- メンバーリストがここに挿入されます -->
                </ul>
            </div>
        </div>
    </div>
</div>
</body>
</html>
