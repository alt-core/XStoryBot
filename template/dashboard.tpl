<!doctype html>
<html lang="ja">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">

    <link rel="stylesheet" href="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0-beta.3/css/bootstrap.min.css" integrity="sha384-Zug+QiDoJOrZ5t4lssLdxGhVrurbmBWopoEl+M6BdEfwnCJZtKxi1KgxUyJq13dy" crossorigin="anonymous">
    <style>
        body {
            padding-top: 5rem;
        }
        #tab {
            padding-bottom: 1rem;
        }
    </style>

    <title>XStoryBot Dashboard</title>
</head>
<body>

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
                <a class="nav-link" href="{{logout_url}}">Logout</a>
            </li>
        </ul>
    </div>
</nav>

<main role="main">

    <!--
    <div class="jumbotron">
        <div class="container">
            <h1 class="display-3">Dashboard</h1>
        </div>
    </div>
    -->

    <div class="container">
        <header id="tab">
            <ul class="nav nav-tabs">
                % for name in bot_list:
                <li class="nav-item">
                    <a class="nav-link{{" active" if bot_name == name else ""}}" href="/dashboard/{{name}}">{{bot_settings[name]["name"]}}</a>
                </li>
                % end
            </ul>
        </header>

        % if bot_settings[name].has_key("description"):
        <div class="row">
            <div class="col">
                {{!bot_settings[name]["description"]}}
            </div>
        </div>
        % end
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
                <h3>反映時のエラー状況</h3>
                <p>
                <div class="embed-responsive embed-responsive-21by9">
                    <iframe class="embed-responsive-item" src="/api/last_build_result/{{bot_name}}" id="build_result" style="border: 1px solid black"></iframe>
                </div>
                </p>
                <button type="button" class="btn btn-primary" style="width:100%" id="build_result_reload">更新</button>
            </div>
        </div>

        <hr>

    </div> <!-- /container -->

</main>

<footer class="container">
    <p>&copy; alt-core 2018</p>
</footer>




<script src="https://code.jquery.com/jquery-3.2.1.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.12.9/umd/popper.min.js" integrity="sha384-ApNbgh9B+Y1QKtv3Rn7W3mgPxhU9K/ScQsAP7hUibX39j7fakFPskvXusvfa0b4Q" crossorigin="anonymous"></script>
<script src="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0-beta.3/js/bootstrap.min.js" integrity="sha384-a5N7Y/aK3qNeh15eJKGWxsqtnX/wWdSZSKp+81YjTmS15nvnvxKHuzaWwXHDli+4" crossorigin="anonymous"></script>
<script type="text/javascript">
    function request_build(path, options) {
        var hostname = location.hostname;
        var m = hostname.match(/^(.+-dot-)(.+)\.appspot\.com$/);
        if (m) {
            // m[1]: version
            hostname = m[1]+'builder-dot-'+m[2];
        } else {
            hostname = 'builder-dot-' + hostname;
        }
        var endpoint ='https://'+hostname+path;
        $.ajax({
            crossDomain: true,
            url: endpoint,
            type: 'POST',
            data: options
        })
            .done(function(data) {
                console.log(data);
                var resp = JSON.parse(data);
                if (resp.result === 'Success') { alert('反映作業を開始しました'); }
                else { alert('反映作業の開始に失敗しました'); }
            })
            .fail(function() {
                alert('反映作業の開始に失敗しました');
            });

    }

    $(function(){
        $("#build_button").on("click",function(){
            request_build('/api/build_async/{{bot_name}}', {})
        });
        $("#force_build_button").on("click",function(){
            request_build('/api/build_async/{{bot_name}}', {'force': 'true'})
        });
        $("#quick_build_button").on("click",function(){
            request_build('/api/build_async/{{bot_name}}', {'skip_image': 'true'})
        });
        $("#build_result_reload").on("click",function(){
            $('#build_result')[0].contentDocument.location.reload(true);
        });
    });
</script>
</body>
</html>

