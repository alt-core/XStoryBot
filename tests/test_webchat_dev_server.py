import unittest
from pathlib import Path

from tools.webchat_dev_server import (
    _avatar_svg,
    _map_svg,
    _scene_svg,
    build_turn,
)


class WebchatDevServerTest(unittest.TestCase):
    def setUp(self):
        self.base_url = 'http://127.0.0.1:8765'

    def test_startと主要messageを生成する(self):
        video_fixture = (
            Path(__file__).resolve().parent / 'fixtures/webchat/video.mp4')
        self.assertGreater(video_fixture.stat().st_size, 1000)
        status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        self.assertEqual(200, status)
        self.assertEqual(['text', 'button', 'text'], [
            message['type'] for message in started['messages']])
        self.assertEqual(
            '画像', started['messages'][-1]['quick_replies'][0]['label'])

        status, video = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {'type': 'text', 'text': 'video'},
        }, self.base_url)
        self.assertEqual(200, status)
        self.assertEqual(
            'postback', video['messages'][0]['completion_action']['type'])
        self.assertEqual(['video', 'text'], [
            message['type'] for message in video['messages']])
        self.assertEqual(
            '画像', video['messages'][1]['quick_replies'][0]['label'])

    def test_デモsenderを案内人へ統一する(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        self.assertEqual(
            {'案内人'},
            {message['sender']['name'] for message in started['messages']})

    def test_各応答後にQuick_Reply一覧を再表示する(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        prompt_text = (
            '試したい機能名を入力するか、'
            '以下のボタンから選んでください。')
        for keyword in (
                'image', 'audio', 'video', 'button', 'imagemap',
                'more', 'slow', 'unknown'):
            with self.subTest(keyword=keyword):
                _status, result = build_turn('bot', {
                    'state_token': started['state_token'],
                    'input': {'type': 'text', 'text': keyword},
                }, self.base_url, sleep=lambda _seconds: None)
                prompt = result['messages'][-1]
                self.assertEqual(prompt_text, prompt['text'])
                self.assertEqual(
                    '画像', prompt['quick_replies'][0]['label'])

        status, menu = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {'type': 'text', 'text': 'menu'},
        }, self.base_url)
        self.assertEqual(200, status)
        self.assertEqual(prompt_text, menu['messages'][0]['text'])
        self.assertEqual(
            '画像', menu['messages'][0]['quick_replies'][0]['label'])

    def test_チャット内linkと外部browser指定を生成する(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        status, result = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {'type': 'text', 'text': 'button'},
        }, self.base_url)
        self.assertEqual(200, status)
        actions = result['messages'][0]['actions']
        self.assertEqual(
            f'{self.base_url}/devpage/help', actions[2]['href'])
        self.assertIn(
            'openExternalBrowser=1', actions[3]['href'])

    def test_Imagemapにactionのない領域を残す(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        status, result = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {'type': 'text', 'text': 'imagemap'},
        }, self.base_url)
        self.assertEqual(200, status)
        imagemap = result['messages'][0]
        self.assertEqual('imagemap', imagemap['type'])
        # 画像上部中央はどのhotspotにも含めず、無反応を確認できる。
        point = (imagemap['width'] // 2, 100)
        self.assertFalse(any(
            area['x'] <= point[0] < area['x'] + area['width']
            and area['y'] <= point[1] < area['y'] + area['height']
            for area in imagemap['areas']))

    def test_longを4messageずつ続きを読むで継続する(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        state_token = started['state_token']
        input_data = {'type': 'text', 'text': 'long'}

        for page in range(3):
            status, result = build_turn('bot', {
                'state_token': state_token,
                'input': input_data,
            }, self.base_url)
            self.assertEqual(200, status)
            self.assertEqual(
                f'長文表示の確認 {page * 4 + 1}。',
                result['messages'][0]['text'].split('本文です。', 1)[0])
            self.assertIn(
                f'長文表示の確認 {page * 4 + 4}。',
                result['messages'][3]['text'])

            if page < 2:
                self.assertEqual(4, len(result['messages']))
                action = result['messages'][-1]['quick_replies'][0]
                self.assertEqual('postback', action['type'])
                self.assertEqual('続きを読む', action['label'])
                input_data = {
                    'type': 'postback',
                    'postback_token': action['token'],
                }
            else:
                self.assertEqual(5, len(result['messages']))
                self.assertEqual(
                    '画像',
                    result['messages'][-1]['quick_replies'][0]['label'])

            state_token = result['state_token']

        self.assertEqual('続きを読む', result['echo_message'])

    def test_video完了とerrorを再現する(self):
        _status, started = build_turn(
            'bot', {'input': {'type': 'start'}}, self.base_url)
        status, completed = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {
                'type': 'postback',
                'postback_token': 'video-complete',
            },
        }, self.base_url)
        self.assertEqual(200, status)
        self.assertIn('再生完了', completed['messages'][0]['text'])

        status, error = build_turn('bot', {
            'state_token': started['state_token'],
            'input': {'type': 'text', 'text': 'error'},
        }, self.base_url)
        self.assertEqual(500, status)
        self.assertEqual('internal-error', error['code'])

    def test_表示用SVGを用途別の絵として生成する(self):
        scene = _scene_svg('夕暮れの街並み')
        poster = _scene_svg('動画デモ', show_play=True)
        imagemap = _map_svg()
        avatar = _avatar_svg('#4f66a8')

        self.assertIn(b'id="city"', scene)
        self.assertNotIn(b'<text', scene)
        self.assertIn(b'aria-label="\xe5\x86\x8d\xe7\x94\x9f"', poster)
        self.assertNotIn(b'<text', imagemap)
        self.assertIn(b'id="left-road"', imagemap)
        self.assertIn(b'id="right-road"', imagemap)
        self.assertIn(b'id="face"', avatar)


if __name__ == '__main__':
    unittest.main()
