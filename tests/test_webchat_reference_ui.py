import json
from pathlib import Path
import re
import unittest

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WebchatReferenceUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (PROJECT_ROOT / 'static/webchat/index.html').read_text(
            encoding='utf-8')
        cls.script = (PROJECT_ROOT / 'static/webchat/app.js').read_text(
            encoding='utf-8')
        cls.style = (PROJECT_ROOT / 'static/webchat/style.css').read_text(
            encoding='utf-8')
        cls.web_app = (PROJECT_ROOT / 'app_webchat.py').read_text(
            encoding='utf-8')
        cls.dev_server = (
            PROJECT_ROOT / 'tools/webchat_dev_server.py'
        ).read_text(encoding='utf-8')
        cls.schema = json.loads((
            PROJECT_ROOT / 'docs/webchat-message-spec.schema.json'
        ).read_text(encoding='utf-8'))
        cls.openapi = yaml.safe_load((
            PROJECT_ROOT / 'docs/webchat.openapi.yaml'
        ).read_text(encoding='utf-8'))
        cls.client_types = (
            PROJECT_ROOT / 'webchat-client/index.d.ts'
        ).read_text(encoding='utf-8')
        cls.svelte_example = (
            PROJECT_ROOT / 'webchat-client/examples/svelte/Webchat.svelte'
        ).read_text(encoding='utf-8')

    def test_inline_scriptと外部CDNを使わない(self):
        self.assertIn(
            '<script type="module" src="/static/webchat/app.js"></script>',
            self.html)
        self.assertEqual(1, self.html.count('<script'))
        self.assertNotIn('https://', self.html)

    def test_viewport_fitに対応する全safe_areaを確保する(self):
        self.assertIn('viewport-fit=cover', self.html)
        for name in ('top', 'right', 'bottom', 'left'):
            self.assertIn(f'env(safe-area-inset-{name})', self.style)

    def test_非表示bannerが送信欄の上余白を増やさない(self):
        self.assertIn('.banner[hidden] { display: none; }', self.style)

    def test_video完了actionをUIとschemaへ公開する(self):
        self.assertIn('createVideoCompletionQueue', self.script)
        self.assertIn(
            'completion_action', self.schema['properties'])

    def test_mediaの遅延resize後も最下部追従を補正する(self):
        self.assertIn('createBottomResizeFollower', self.script)
        self.assertIn('new ResizeObserver', self.script)
        self.assertIn(
            'messagesResizeObserver.observe(messagesElement)', self.script)

    def test_連続する吹き出し間隔を4px相当にする(self):
        stack_rule = re.search(r'\.stack \{(.*?)\n\}', self.style, re.S)
        self.assertIsNotNone(stack_rule)
        self.assertIn('gap: 0.25rem;', stack_rule.group(1))

    def test_Imagemapはチャット幅でtouch領域だけをactionにする(self):
        self.assertIn("group.className = `group ${role} wide`", self.script)
        self.assertIn(
            "message.type === 'text', message.type === 'imagemap');",
            self.script)
        self.assertIn('.group.wide {', self.style)
        self.assertIn('margin-inline: -0.75rem;', self.style)
        self.assertIn('.bubble.imagemap-bubble {', self.style)
        self.assertIn("hotspot.className = 'hotspot'", self.script)
        self.assertIn('-webkit-tap-highlight-color: transparent;', self.style)
        self.assertNotIn('.imagemap .hotspot:hover', self.style)
        self.assertIn('.imagemap .hotspot:focus-visible', self.style)
        self.assertNotIn(
            'openMediaViewer(message, hotspot)', self.script)

    def test_touch端末でbuttonとlinkのhighlightを残さない(self):
        self.assertIn(
            'button, a { -webkit-tap-highlight-color: transparent; }',
            self.style)
        hover_media = re.search(
            r'@media \(hover: hover\) and \(pointer: fine\) \{(.*?)\n\}',
            self.style, re.S)
        self.assertIsNotNone(hover_media)
        for selector in (
                '.header-button:hover', '.card-action:hover', '.chip:hover',
                '#send:hover:not(:disabled)', '.media-viewer-close:hover'):
            self.assertIn(selector, hover_media.group(1))

    def test_画像と動画をチャット内dialogで拡大表示する(self):
        self.assertIn('<dialog id="media-viewer"', self.html)
        self.assertIn('autofocus>×</button>', self.html)
        self.assertIn("typeof mediaViewer.showModal === 'function'",
                      self.script)
        self.assertIn('mediaViewer.showModal()', self.script)
        self.assertIn("mediaViewer.setAttribute('open', '')", self.script)
        self.assertIn('event.target === mediaViewer', self.script)
        self.assertIn("mediaViewer.addEventListener('cancel'", self.script)
        self.assertIn("mediaViewer.addEventListener('close'", self.script)
        self.assertIn("trigger.setAttribute('aria-haspopup', 'dialog')",
                      self.script)
        self.assertNotIn('media.controls = true', self.script)
        self.assertIn('createVideoPlaybackController', self.script)
        self.assertIn('playback?.start()', self.script)
        self.assertIn(
            '.media-viewer::backdrop { background: transparent; }',
            self.style)
        self.assertIn('.media-viewer.fallback-open {', self.style)
        self.assertIn('.media-viewer.pointer-open', self.style)
        self.assertIn("event.detail > 0", self.script)
        self.assertIn(
            "mediaViewer.classList.remove('pointer-open')", self.script)

    def test_URIをiframeと外部tabへ分ける(self):
        self.assertIn('classifyUriTarget', self.script)
        self.assertIn("frame.className = 'link-viewer-frame'", self.script)
        self.assertIn(
            "'sandbox', 'allow-forms allow-scripts allow-same-origin'",
            self.script)
        self.assertIn("link.target = '_blank'", self.script)
        self.assertIn("frame-src 'self' https:", self.web_app)
        self.assertIn("frame-src 'self' https:", self.dev_server)
        self.assertIn('.link-viewer-frame {', self.style)

    def test_Quick_Replyを対象message直後へ表示する(self):
        self.assertNotIn('id="quick-replies"', self.html)
        self.assertIn('renderQuickReplies(message)', self.script)
        self.assertIn('bindHorizontalDrag(container)', self.script)
        self.assertIn("row.className = 'quick-replies-row'", self.script)
        self.assertIn('historyElement.append(row)', self.script)
        self.assertIn(
            'container.dataset.messageId = message.id', self.script)
        self.assertIn(
            'snapshot.activeResponse.map((message) => message.id)',
            self.script)
        self.assertIn(
            "group.classList.toggle('is-placeholder', reserveSpace)",
            self.script)
        self.assertIn('.quick-replies-row {', self.style)
        self.assertIn('justify-content: flex-start;', self.style)
        self.assertIn('justify-content: safe center;', self.style)
        self.assertIn('cursor: pointer;', self.style)
        self.assertIn('cursor: grabbing;', self.style)
        self.assertIn('.quick-replies[hidden] { display: none; }', self.style)
        self.assertIn('visibility: hidden', self.style)

    def test_OpenAPIがstate条件と固定problem_codeを表す(self):
        schemas = self.openapi['components']['schemas']
        turn_request = schemas['TurnRequest']
        self.assertIn('allOf', turn_request)
        condition = turn_request['allOf'][0]
        self.assertEqual(
            'start',
            condition['if']['properties']['input']['properties']['type'][
                'const'])
        self.assertEqual(
            ['state_token'], condition['else']['required'])
        self.assertEqual(
            ['state_token'], condition['then']['not']['required'])

        problem = schemas['Problem']
        self.assertIn('type', problem['required'])
        self.assertEqual('about:blank', problem['properties']['type']['const'])
        self.assertIn(
            'action-not-active', problem['properties']['code']['enum'])

    def test_MessageSpecとTypeScriptのrequired契約を揃える(self):
        self.assertFalse(self.schema['additionalProperties'])
        self.assertEqual(
            ['name', 'icon_url'],
            self.schema['properties']['sender']['required'])
        conditions = {
            item['if']['properties']['type']['const']: item['then']
            for item in self.schema['allOf']
        }
        self.assertIn('mime_type', conditions['audio']['required'])
        self.assertTrue(
            {'title', 'image_url'}.issubset(conditions['button']['required']))
        postback = next(
            item for item in self.schema['$defs']['action']['oneOf']
            if item['properties']['type']['const'] == 'postback')
        self.assertIn('echo_text', postback['required'])
        self.assertIn('completion_action?', self.client_types)

    def test_追加した公開文書の内部linkが存在する(self):
        sources = [
            PROJECT_ROOT / 'README.md',
            *sorted((PROJECT_ROOT / 'docs').glob('*.md')),
            PROJECT_ROOT / 'webchat-client/README.md',
        ]
        missing = []
        for source in sources:
            text = source.read_text(encoding='utf-8')
            for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
                if '://' in target or target.startswith('#'):
                    continue
                path = (source.parent / target.split('#', 1)[0]).resolve()
                if not path.exists():
                    missing.append((str(source), target))
        self.assertEqual([], missing)

    def test_Svelte最小例は送信中も入力を維持する(self):
        self.assertIn('<input bind:value={draft} />', self.svelte_example)
        self.assertIn("chat.getSnapshot().status === 'sending'",
                      self.svelte_example)


if __name__ == '__main__':
    unittest.main()
