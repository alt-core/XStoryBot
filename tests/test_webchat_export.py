"""静的Webchatの出力と再生成を、外部通信や実設定なしで確認する。"""

import contextlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urljoin, urlsplit

from tools import export_webchat


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class WebchatExportTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.output = self.root / 'site'

    def test_公開設定をescapeし限定したファイルだけを任意subpathへ配布できる(self):
        bot = '物語"<&%20'
        result = export_webchat.export_webchat('https://api.example.test/stage///', bot, self.output)
        self.assertTrue(result['ok'])
        self.assertEqual(str(self.output), result['output'])
        self.assertEqual(str(self.output / 'index.html'), result['entry'])
        self.assertTrue(result['revision'])
        asset_root = f'assets/{result["revision"]}'
        expected = {'index.html', 'LICENSE'} | {
            f'{asset_root}/{name}' for name in ('app.js', 'style.css', 'ui_logic.js', 'client.js')}
        self.assertEqual(expected, set(result['files']))
        self.assertEqual(expected, {
            str(path.relative_to(self.output)) for path in self.output.rglob('*') if path.is_file()})
        self.assertEqual((PROJECT_ROOT / 'LICENSE').read_bytes(), (self.output / 'LICENSE').read_bytes())

        page = Page((self.output / 'index.html').read_text(encoding='utf-8'))
        html = next(attrs for tag, attrs in page.tags if tag == 'html')
        self.assertEqual(bot, html['data-webchat-bot'])
        self.assertEqual('https://api.example.test/stage', html['data-webchat-api-base-url'])
        scripts = [attrs for tag, attrs in page.tags if tag == 'script']
        self.assertEqual(1, len(scripts))
        self.assertEqual('module', scripts[0]['type'])
        metas = [attrs for tag, attrs in page.tags if tag == 'meta']
        self.assertTrue(any(attrs.get('name') == 'referrer' and attrs.get('content') == 'no-referrer'
                            for attrs in metas))
        csp = next(attrs['content'] for attrs in metas
                   if attrs.get('http-equiv', '').lower() == 'content-security-policy')
        directives = {part.split()[0]: part.split()[1:] for part in csp.split(';') if part.strip()}
        self.assertIn('https://api.example.test', directives['connect-src'])
        self.assertNotIn('*', directives['connect-src'])
        self.assertEqual(["'self'"], directives['script-src'])

        stylesheet = next(attrs['href'] for tag, attrs in page.tags
                          if tag == 'link' and attrs.get('rel') == 'stylesheet')
        imports = re.findall(r"from ['\"]([^'\"]+)['\"]",
                             (self.output / asset_root / 'app.js').read_text(encoding='utf-8'))
        self.assertEqual({'./client.js', './ui_logic.js'}, set(imports))
        for page_url in ('https://www.example.test/games/story/',
                         'https://www.example.test/games/story/index.html'):
            with self.subTest(page=page_url):
                script_url = urljoin(page_url, scripts[0]['src'])
                urls = [urljoin(page_url, stylesheet), script_url]
                urls.extend(urljoin(script_url, value) for value in imports)
                for url in urls:
                    self.assertEqual('www.example.test', urlsplit(url).netloc)
                    self.assertTrue(urlsplit(url).path.startswith('/games/story/'))
                    relative = urlsplit(url).path.removeprefix('/games/story/')
                    self.assertTrue((self.output / relative).is_file(), url)

    def test_空dirへ生成でき再生成は旧assetや利用者fileを削除しない(self):
        self.output.mkdir()
        first = export_webchat.export_webchat('https://api.example.test', 'first', self.output)
        saved = {name: (self.output / name).read_bytes() for name in first['files'] if name != 'index.html'}
        own_file = self.output / 'site-note.txt'
        own_file.write_bytes(b'keep user file')
        changed_style = self.root / 'changed-style.css'
        changed_style.write_bytes((PROJECT_ROOT / 'static/webchat/style.css').read_bytes() + b'\nbody { color: red; }\n')

        with patch.dict(export_webchat.ASSET_SOURCES, {'style.css': str(changed_style)}):
            second = export_webchat.export_webchat('http://127.0.0.1:8765/prefix/', 'second', self.output)

        self.assertNotEqual(first['revision'], second['revision'])
        html = next(attrs for tag, attrs in Page((self.output / 'index.html').read_text()).tags
                    if tag == 'html')
        self.assertEqual('second', html['data-webchat-bot'])
        self.assertEqual('http://127.0.0.1:8765/prefix', html['data-webchat-api-base-url'])
        for name, data in saved.items():
            self.assertEqual(data, (self.output / name).read_bytes())
        self.assertEqual(b'keep user file', own_file.read_bytes())

    def test_無関係なdirと不正入力は既存内容を変更しない(self):
        self.output.mkdir()
        existing = self.output / 'index.html'
        existing.write_bytes(b'original website')
        with self.assertRaises((ValueError, OSError)):
            export_webchat.export_webchat('https://api.example.test', 'bot', self.output)
        self.assertEqual(b'original website', existing.read_bytes())
        self.assertEqual([existing], list(self.output.iterdir()))
        for url, bot in (
                ('/relative', 'bot'), ('ftp://api.example.test', 'bot'),
                ('https://user:password@api.example.test', 'bot'),
                ('https://api.example.test?token=unused', 'bot'),
                ('https://api.example.test#section', 'bot'),
                ('https://api.example.test', ' \t')):
            with self.subTest(url=url, bot=bot), self.assertRaises(ValueError):
                export_webchat.export_webchat(url, bot, self.root / 'invalid')
            self.assertFalse((self.root / 'invalid').exists())

    def test_初回の途中失敗で半端な出力を残さず再生成失敗も旧入口を保つ(self):
        original_write = export_webchat._write_public

        def fail_stylesheet(path, data):
            if path.name == 'style.css':
                raise OSError('人工的な保存失敗')
            original_write(path, data)

        with patch.object(export_webchat, '_write_public', side_effect=fail_stylesheet):
            with self.assertRaises(OSError):
                export_webchat.export_webchat('https://api.example.test', 'bot', self.output)
        self.assertFalse(self.output.exists())
        export_webchat.export_webchat('https://api.example.test', 'bot', self.output)
        old_page = (self.output / 'index.html').read_bytes()
        with patch.object(export_webchat, '_write_public', side_effect=fail_stylesheet):
            with self.assertRaises(OSError):
                export_webchat.export_webchat('https://api.example.test', 'new-bot', self.output)
        self.assertEqual(old_page, (self.output / 'index.html').read_bytes())

    def test_出力symlinkが別directoryを上書きしない(self):
        self.output.mkdir()
        other = self.root / 'other'
        other.mkdir()
        (other / 'index.html').write_text(export_webchat.EXPORT_MARKER, encoding='utf-8')
        (self.output / 'index.html').symlink_to(other / 'index.html')
        with self.assertRaises(ValueError):
            export_webchat.export_webchat('https://api.example.test', 'bot', self.output)
        self.assertEqual(export_webchat.EXPORT_MARKER,
                         (other / 'index.html').read_text(encoding='utf-8'))

    def test_CLIは標準ライブラリだけで実設定を読まずJSON一件を返す(self):
        script = r'''
import os
from pathlib import Path
import runpy
import sys

forbidden = {os.environ['XSBOT_SETTINGS_FILE'], os.environ['GOOGLE_APPLICATION_CREDENTIALS']}
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = str(Path(os.fsdecode(args[0])).resolve())
        if path in forbidden or Path(path).name == 'settings.yaml':
            raise AssertionError('実設定や資格情報を読み込もうとしました')
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('外部通信を開始しようとしました')
sys.addaudithook(audit)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
        result = subprocess.run([
            sys.executable, '-I', '-S', '-B', '-c', script,
            str(PROJECT_ROOT / 'tools/export_webchat.py'),
            '--api-base-url', 'https://api.example.test', '--bot', 'bot', '--output', str(self.output),
        ], cwd=self.root, env={**os.environ,
            'XSBOT_SETTINGS_FILE': str(self.root / 'unread-settings.yaml'),
            'GOOGLE_APPLICATION_CREDENTIALS': str(self.root / 'unread-credentials.json'),
            'XSBOT_CLOUD_PROVIDER': 'gcp'}, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_CLIの入力とIOエラーはJSON一件と終了2になる(self):
        target = self.root / 'file'
        target.write_bytes(b'preserve')
        for url, output in (('/relative', self.output), ('https://api.example.test', target)):
            with self.subTest(url=url, output=output):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = export_webchat.main([
                        '--api-base-url', url, '--bot', 'bot', '--output', str(output)])
                self.assertEqual(2, code)
                self.assertEqual(1, len(stdout.getvalue().splitlines()))
                self.assertFalse(json.loads(stdout.getvalue())['ok'])
                self.assertNotIn('Traceback', stdout.getvalue() + stderr.getvalue())
        self.assertEqual(b'preserve', target.read_bytes())


if __name__ == '__main__':
    unittest.main()
