# coding: utf-8
import hashlib
import logging


from google.appengine.ext import ndb

from plugin.render_text import renderer

from plugin.line import more
import commands
import utility
from plugin.line import default_commands


IMAGE_TEXT_CMDS = (u'@imagetext', u'@画像テキスト')


class ImageTextStatDB(ndb.Model):
    text = ndb.TextProperty()
    file_digest = ndb.StringProperty()
    url = ndb.StringProperty()
    width = ndb.IntegerProperty()
    height = ndb.IntegerProperty()
    rest = ndb.TextProperty()

    @classmethod
    def get_cached_image_text_stat(cls, text):
        text_digest = hashlib.md5(text.encode('utf-8')).hexdigest()
        stat = cls.get_by_id(id=text_digest)
        if stat is None or text != stat.text:
            return None
        size = (stat.width, stat.height)
        return stat.url, size, stat.rest

    @classmethod
    def put_cached_image_text_stat(cls, text, file_digest, url, size, rest):
        text_digest = hashlib.md5(text.encode('utf-8')).hexdigest()
        entry = cls.get_by_id(id=text_digest)
        if entry is None:
            entry = cls(id=text_digest, text=text, file_digest=file_digest, url=url, width=size[0], height=size[1], rest=rest)
        else:
            if entry.file_digest == file_digest and entry.url == url:
                # 更新しない
                return
            entry.text = text
            entry.file_digest = file_digest
            entry.url = url
            entry.rest = rest
            entry.width, entry.height = size
        entry.put()


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
                'margin_top': 0,
                'margin_bottom': 0,
                'margin_left': 0,
                'margin_right': 0,
            }, utility.extract_params(frames[key], ['size_x', 'size_y', 'margin_x', 'margin_y', 'more_mode', 'button_area', 'more_message', 'margin_top', 'margin_bottom', 'margin_left', 'margin_right']))
            self.frames[key]['text_rendering_options'] = utility.extract_params(frames[key], ['is_vertical', 'font_path', 'font_size', 'color', 'background', 'line_height', 'base_line_offset', 'disable_word_wrap', 'burasagari_chars', 'special_char_table'])
            if self.frames[key]['button_area'] is None:
                self.frames[key]['button_area'] = u'0,0,{},{}'.format(self.frames[key]['size_x'], self.frames[key]['size_y'])
            if self.frames[key]['margin_top'] is None:
                self.frames[key]['margin_top'] = self.frames[key]['margin_y']
            if self.frames[key]['margin_bottom'] is None:
                self.frames[key]['margin_bottom'] = self.frames[key]['margin_y']
            if self.frames[key]['margin_left'] is None:
                self.frames[key]['margin_left'] = self.frames[key]['margin_x']
            if self.frames[key]['margin_right'] is None:
                self.frames[key]['margin_right'] = self.frames[key]['margin_x']
        self.default_frame = params.get('default_frame', None)
        if self.default_frame is None and len(self.frames) == 1:
            self.default_frame = self.frames.keys()[0]

    def build_from_command(self, builder, sender, msg, options):
        text = options[0]
        frame = utility.safe_list_get(options, 1, self.default_frame)
        if frame is None:
            builder.raise_error(u"画像テキストで使用するフレームが指定されていません")
        frame_opt = self.frames.get(frame, None)
        if frame_opt is None:
            builder.raise_error(u"定義されていないフレーム名です: {}".format(frame))
        more_message = utility.safe_list_get(options, 2, frame_opt['more_message'])
        counter = 0
        while text:
            stat = None
            if not builder.option_force:
                stat = ImageTextStatDB.get_cached_image_text_stat(text)
            if stat is None:
                png_data, rest = renderer.render_text_to_png(text, frame_opt['size_x'], frame_opt['size_y'], frame_opt['margin_left'], frame_opt['margin_right'], frame_opt['margin_top'], frame_opt['margin_bottom'], **frame_opt['text_rendering_options'])
                file_digest = '{}_{}'.format('PNG', hashlib.md5(png_data).hexdigest())
                image_url, size = builder.build_image_for_imagemap_command_with_rawdata(png_data, file_digest=file_digest, logging_context=u'imagetext: {}'.format(text))

                #encoded_text = urllib.quote_plus(options[0].encode('utf-8'), safe='')
                ImageTextStatDB.put_cached_image_text_stat(text, file_digest, image_url, size, rest)
            else:
                #logging.debug(u'ImageTextStatDB has {}...'.format(text[:4]))
                image_url, size, rest = stat
            if frame_opt['more_mode'] == 'inner':
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [unicode(image_url), unicode(size[0]), unicode(size[1])], [[frame_opt['button_area'], more_message]])
                next_label = u'##IMGTEXT__{}__{}'.format(builder.scene.get_relative_position_desc(builder.node), counter)
                builder.add_command(sender, more.SET_NEXT_LABEL_CMD, [next_label, more_message], None)
                builder.add_new_string_index(next_label)
            else:
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [unicode(image_url), unicode(size[0]), unicode(size[1])], [])
            text = rest
            if (text and frame_opt['more_mode'] == 'between') or frame_opt['more_mode'] == 'always':
                # more_mode が between なら間に、always なら常に後ろに more button を表示
                filepath, size = builder.build_image_for_imagemap_command(self.more_image_url)
                builder.add_command(sender, default_commands.IMAGEMAP_CMDS[0], [unicode(filepath), unicode(size[0]), unicode(size[1])], [[u'0,0,{},{}'.format(size[0],size[1]), more_message]])
                next_label = u'##IMGTEXT__{}__{}'.format(builder.scene.get_relative_position_desc(builder.node), counter)
                builder.add_command(sender, more.SET_NEXT_LABEL_CMD, [next_label, more_message], None)
                builder.add_new_string_index(next_label)
            counter += 1
            if counter > 100:
                builder.raise_error(u'infinite loop detected')

        # 解釈はここで終了
        return True


def load_plugin(params):
    builder = LineImageTextPlugin_Builder(params)
    commands.register_command(commands.CommandEntry(
            names=IMAGE_TEXT_CMDS,
            options='text [text] [text]',
            builder=builder,
            service='line'))
