# coding: utf-8
import hashlib
import logging
import json

from google.cloud import firestore

from plugin.render_text import renderer

from plugin.line import more
import commands
import utility
from plugin.line import default_commands, quick_reply


IMAGE_TEXT_CMDS = ('@imagetext', '@画像テキスト', '@novel', '@小説')

# Firestoreクライアントの初期化
db = firestore.Client()

class ImageTextStatDB:
    @classmethod
    def get_cached_image_text_stat(cls, text, frame_opt):
        text_digest = hashlib.md5(f'{text}\n{frame_opt}'.encode('utf-8')).hexdigest()
        doc_ref = db.collection('image_text_stats').document(text_digest)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            if data['text'] == text and data['frame_opt'] == frame_opt:
                return data.get('url'), (data.get('width'), data.get('height')), data.get('rest')
        return None

    @classmethod
    def put_cached_image_text_stat(cls, text, frame_opt, file_digest, url, size, rest):
        text_digest = hashlib.md5(f'{text}\n{frame_opt}'.encode('utf-8')).hexdigest()
        doc_ref = db.collection('image_text_stats').document(text_digest)
        doc_ref.set({
            'text': text,
            'frame_opt': frame_opt,
            'file_digest': file_digest,
            'url': url,
            'width': size[0],
            'height': size[1],
            'rest': rest
        })


class LineImageTextPlugin_Builder(object):
    def __init__(self, params):
        self.default_more_message = params['more_message']
        self.more_image_url = params['more_image_url']
        frames = params.get('frames', {})
        if len(frames) == 0:
            frames['default'] = {}
        self.frames = {}
        for key in frames.keys():
            self.frames[key] = utility.merge_params({
                'size_x': 2080,
                'size_y': 2080,
                'margin_x': 0,
                'margin_y': 0,
                'more_mode': 'between',
                'button_area': None,
                'more_message': self.default_more_message,
                'margin_top': None,
                'margin_bottom': None,
                'margin_left': None,
                'margin_right': None,
                'prevent_overflow': False,
                'auto_scaling': None,
                'reply_message': '続きを読む=>「続きを読む」',
                'please_select_quick_reply_label': '##please_select_quick_reply',
            }, utility.extract_params(frames[key], ['size_x', 'size_y', 'margin_x', 'margin_y', 'more_mode', 'button_area', 'more_message', 'margin_top', 'margin_bottom', 'margin_left', 'margin_right', 'prevent_overflow', 'auto_scaling', 'reply_message', 'please_select_quick_reply_label']))
            self.frames[key]['text_rendering_options'] = utility.extract_params(frames[key], ['auto_scaling', 'is_vertical', 'font_path', 'font_size', 'color', 'background', 'line_height', 'base_line_offset', 'disable_word_wrap', 'burasagari_chars', 'special_char_table', 'centering_x', 'centering_y'])
            if self.frames[key]['button_area'] is None:
                self.frames[key]['button_area'] = '0,0,{},{}'.format(self.frames[key]['size_x'], self.frames[key]['size_y'])
            if self.frames[key]['margin_top'] is None:
                self.frames[key]['margin_top'] = self.frames[key]['margin_y']
            if self.frames[key]['margin_bottom'] is None:
                self.frames[key]['margin_bottom'] = self.frames[key]['margin_y']
            if self.frames[key]['margin_left'] is None:
                self.frames[key]['margin_left'] = self.frames[key]['margin_x']
            if self.frames[key]['margin_right'] is None:
                self.frames[key]['margin_right'] = self.frames[key]['margin_x']
        #from utility import deep_dump
        #deep_dump(self.frames)
        self.default_frame = params.get('default_frame', None)
        if self.default_frame is None and len(self.frames) == 1:
            self.default_frame = list(self.frames.keys())[0]

    def build_from_command(self, builder, sender, msg, options):
        text = options[0]
        frame = utility.safe_list_get(options, 1, self.default_frame)
        if frame is None:
            builder.raise_error("画像テキストで使用するフレームが指定されていません")
        frame_opt = self.frames.get(frame, None)
        if frame_opt is None:
            builder.raise_error("定義されていないフレーム名です: {}".format(frame))
        more_message = utility.safe_list_get(options, 2, frame_opt['more_message'])
        counter = 0
        while text:
            stat = None
            if not builder.option_force:
                stat = ImageTextStatDB.get_cached_image_text_stat(text, json.dumps(frame_opt))
            if stat is None:
                png_data, rest = renderer.render_text_to_png(text, frame_opt['size_x'], frame_opt['size_y'], frame_opt['margin_left'], frame_opt['margin_right'], frame_opt['margin_top'], frame_opt['margin_bottom'], **frame_opt['text_rendering_options'])
                if frame_opt['prevent_overflow'] or (frame_opt.get('auto_scaling', None) is not None):
                    if rest:
                        builder.raise_error(f'画像テキストで文字が溢れました: {rest}')
                file_digest = '{}_{}'.format('PNG', hashlib.md5(png_data).hexdigest())
                image_url, size = builder.build_image_for_imagemap_command_with_rawdata(png_data, file_digest=file_digest, logging_context='imagetext: {}'.format(text))

                #encoded_text = urllib.quote_plus(options[0].encode('utf-8'), safe='')
                ImageTextStatDB.put_cached_image_text_stat(text, json.dumps(frame_opt), file_digest, image_url, size, rest)
            else:
                #logging.debug('ImageTextStatDB has {}...'.format(text[:4]))
                image_url, size, rest = stat
            if frame_opt['more_mode'] == 'inner':
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [str(image_url), str(size[0]), str(size[1])], [[frame_opt['button_area'], more_message]])
                next_label = '##IMGTEXT__{}__{}'.format(builder.scene.get_relative_position_desc(builder.node), counter)
                builder.add_command(sender, more.SET_NEXT_LABEL_CMD, [next_label, more_message], None)
                builder.add_new_string_block(next_label)
            else:
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [str(image_url), str(size[0]), str(size[1])], [])
            text = rest
            if (text and frame_opt['more_mode'] == 'quick_between') or frame_opt['more_mode'] == 'quick_always':
                # more_mode が quick なら間に quick reply を挿入
                quick_reply_base_label = '##IMGTEXT_QREP__{}__{}_'.format(builder.scene.get_relative_position_desc(builder.node), counter)
                quick_reply.append_quick_reply(builder, quick_reply_base_label, [frame_opt['reply_message']], sender, frame_opt['please_select_quick_reply_label'])
            elif (text and frame_opt['more_mode'] == 'between') or frame_opt['more_mode'] == 'always':
                # more_mode が between なら間に、always なら常に後ろに more button を表示
                filepath, size = builder.build_image_for_imagemap_command(self.more_image_url)
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [str(filepath), str(size[0]), str(size[1])], [['0,0,{},{}'.format(size[0],size[1]), more_message]])
                next_label = '##IMGTEXT__{}__{}'.format(builder.scene.get_relative_position_desc(builder.node), counter)
                builder.add_command(sender, more.SET_NEXT_LABEL_CMD, [next_label, more_message], None)
                builder.add_new_string_block(next_label)
            counter += 1
            if counter > 100:
                builder.raise_error('infinite loop detected')

        # 解釈はここで終了
        return True


def load_plugin(params):
    builder = LineImageTextPlugin_Builder(params)
    commands.register_command(commands.CommandEntry(
            names=IMAGE_TEXT_CMDS,
            options='text [text] [text]',
            builder=builder,
            service='line'))
