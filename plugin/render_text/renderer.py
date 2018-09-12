# coding: utf-8
from StringIO import StringIO
from PIL import Image, ImageFont, ImageDraw
import re

SPECIAL_CHAR_TABLE = [
    #    [u'ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ', +0.1, -0.12, 0],
    #    [u'、。', +0.6, -0.55, 0],
    #    [u'ー—―〜「」（）【】', 0, 0, 270],
]

NON_PRINTABLE_CHARS = u"\r"

BURASAGARI_CHARS = u"ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ、。」）】"


def draw_text_horizontal(image, text, margin_x, margin_y, font, font_size, color, line_height, base_line_offset, disable_word_wrap, burasagari_chars):
    text = text.rstrip() # 末尾の空白文字を除去
    draw = ImageDraw.Draw(image)

    sx, sy = (margin_x, margin_y)
    size_x, size_y = image.size
    ny = int((size_y - margin_y * 2 + font_size * (line_height - 1.0)) / (font_size * line_height))
    wx = size_x - margin_x * 2
    iy = 0
    line_str = u''
    f_burasagari = False

    for i in range(len(text)):
        c = text[i]

        if c == u"\n":
            # 現在処理中の行を出力
            y = sy + iy * font_size * line_height + font_size * base_line_offset
            draw.text((sx, y), line_str, fill=color, font=font)

            # 改行
            iy += 1
            f_burasagari = False
            line_str = u''

        elif c in NON_PRINTABLE_CHARS:
            continue
        
        else:
            line_size = draw.textsize(line_str + c, font=font)
            if line_size[0] > wx:
                # 現在の文字で行が溢れたので、溜めていた行を描画する
                if c in burasagari_chars and not f_burasagari:
                    # 1文字だけ禁則ぶら下がりを許す
                    f_burasagari = True
                else:
                    next_line = u''
                    if not disable_word_wrap:
                        # word wrap 処理
                        m = re.match(r'^(.*\S[-\s]+)([a-zA-Z0-9]+)[a-zA-Z0-9,.]$', line_str + c)
                        if m:
                            line_str = m.group(1)
                            next_line = m.group(2)

                    # 現在処理中の行を出力
                    y = sy + iy * font_size * line_height + font_size * base_line_offset
                    draw.text((sx, y), line_str, fill=color, font=font)

                    # 改行
                    iy += 1
                    f_burasagari = False
                    line_str = next_line

            line_str += c

            if not disable_word_wrap and line_str == u' ':
                # word wrap モード時は行頭の半角スペースを削除する
                line_str = u''

        if iy >= ny:
            # 縦の表示領域から溢れた
            return True, line_str + text[i+1:]

    if line_str != u'':
        # 最後の行を出力
        y = sy + iy * font_size * line_height + font_size * base_line_offset
        draw.text((sx, y), line_str, fill=color, font=font)

    return False, None


def draw_text_vertical(image, text, margin_x, margin_y, font, font_size, color, line_height, base_line_offset, burasagari_chars, special_char_table):
    text = text.rstrip() # 末尾の空白文字を除去
    draw = ImageDraw.Draw(image)

    size_x, size_y = image.size
    sx, sy = (size_x - margin_x, margin_y)
    nx = int((size_x - margin_x * 2 + font_size * (line_height - 1.0)) / (font_size * line_height))
    ny = int((size_y - margin_y * 2) / font_size)
    ix, iy = 0, 0
    f_burasagari = False

    for i in range(len(text)):
        c = text[i]

        if c == u"\n":
            ix += 1
            iy = 0
            f_burasagari = False
            if ix >= nx:
                # 表示が溢れた
                return True, text[i+1:]
            continue

        elif c in NON_PRINTABLE_CHARS:
            continue

        if iy >= ny:
            if c in burasagari_chars and not f_burasagari:
                # 1文字だけ禁則ぶら下がりを許す
                f_burasagari = True
            else:
                # 改行
                ix += 1
                iy = 0
                f_burasagari = False
                if ix >= nx:
                    # 表示が溢れた
                    return True, text[i:]

        x = sx - ix * font_size * line_height - font_size
        y = sy + iy * font_size
        base_x, base_y = x, y

        char_width, char_height = font.getsize(c)
        x += (font_size - char_width) / 2
        y += font_size * base_line_offset

        dx = 0
        dy = 0
        rotation = False
        for entry in special_char_table:
            if c in entry[0]:
                _, dx, dy, rotation = entry
                break

        x += font_size * dx
        y += font_size * dy

        draw.text((x, y), c, fill=color, font=font)

        if rotation != 0:
            tmp = image.crop((base_x, base_y, base_x + font_size, base_y + font_size))
            tmp2 = tmp.rotate(270)
            image.paste(tmp2, (base_x, base_y))
            del tmp2
            del tmp

        iy += 1

    return False, None


def get_background_image(size, color, background):
    if re.search(r'\.(png|jpeg|jpg)$', background, re.IGNORECASE):
        im = Image.open(background)
        return im.copy().resize(size, resample=Image.ANTIALIAS)
    else:
        if color in ['white', 'black'] and background in ['white', 'black']:
            return Image.new('L', size, background)
        else:
            return Image.new('RGBA', size, background)


def create_image_with_text(text, size_x, size_y, margin_x, margin_y, is_vertical=False, font_path=None, font_size=100, color='black', background='white', line_height=1.5, base_line_offset=0, disable_word_wrap=False, burasagari_chars=BURASAGARI_CHARS, special_char_table=SPECIAL_CHAR_TABLE):
    image = get_background_image((size_x, size_y), color, background)
    if is_vertical:
        if font_path is None:
            font_path = 'plugin/render_text/font/ipaexg_tate.ttf'
        font = ImageFont.truetype(font_path, font_size)
        flag, rest = draw_text_vertical(image, text, margin_x, margin_y, font, font_size, color, line_height, base_line_offset, burasagari_chars, special_char_table)
    else:
        if font_path is None:
            font_path = 'plugin/render_text/font/ipaexg.ttf'
        font = ImageFont.truetype(font_path, font_size)
        flag, rest = draw_text_horizontal(image, text, margin_x, margin_y, font, font_size, color, line_height, base_line_offset, disable_word_wrap, burasagari_chars)
    return image, rest


def render_text_to_png(text, size_x, size_y, margin_x, margin_y, **text_rendering_options):
    image, rest = create_image_with_text(text, size_x, size_y, margin_x, margin_y, **text_rendering_options)

    output = StringIO()
    image.save(output, 'PNG')
    output_buffer = output.getvalue()
    output.close()

    del image
    return output_buffer, rest
