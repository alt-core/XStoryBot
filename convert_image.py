# coding: utf-8
from StringIO import StringIO
from PIL import Image

DEFAULT_FORMAT = 'PNG'

CONTENT_TYPES = {
    'PNG': 'image/png',
    'JPEG': 'image/jpeg',
}

EXT_NAMES = {
    'PNG': 'png',
    'JPEG': 'jpg',
}


def get_content_type_from_format(format):
    return CONTENT_TYPES[format]


def get_ext_from_format(format):
    return EXT_NAMES[format]


def get_image_format(content):
    data = StringIO(content)
    try:
        image = Image.open(data)
    except (IOError, SyntaxError, ValueError):
        data.close()
        return None, None
    image_format = image.format or DEFAULT_FORMAT
    if image_format not in CONTENT_TYPES:
        image_format = DEFAULT_FORMAT
    return image_format


def resize_image(content, resize_to, force_fit_width=False, never_stretch=False):
    if force_fit_width:
        # 横幅強制モードを指定されているときは、never_stretch を無視
        never_stretch = False

    data = StringIO(content)
    try:
        image = Image.open(data)
    except (IOError, SyntaxError, ValueError):
        data.close()
        return None, None
    image_format = image.format or DEFAULT_FORMAT
    if image_format not in CONTENT_TYPES:
        image_format = DEFAULT_FORMAT

    size_x = image.size[0]
    size_y = image.size[1]

    if never_stretch and size_x <= resize_to and size_y <= resize_to:
        # 引き延ばさないモードの時、かつ、縦横のサイズが変換したいサイズより
        # 小さい場合はオリジナルサイズのままにする

        # EXIF などのことも考えて、オリジナルサイズのままでも書き出し直す
        pass
    else:
        # 通常は、長辺が resize_to に合うように resize する
        if size_x >= size_y or force_fit_width:
            # 横幅の方が大きい、あるいは強制的に横幅で合わせるモードの場合
            resize_y = size_y * resize_to / size_x
            resize_x = resize_to
        else:
            resize_x = size_x * resize_to / size_y
            resize_y = resize_to

        if resize_x > size_x:
            # 拡大する場合
            import logging
            logging.info(u'the image size is too small. stretch it. ({}, {}) -> ({}, {})'.format(size_x, size_y, resize_x, resize_y))
            image = image.resize((resize_x, resize_y))
        else:
            # 縮小する場合は ANTIALIAS を使った方が高品質
            #image = image.resize((resize_x, resize_y), resample=Image.ANTIALIAS)
            image.thumbnail((resize_x, resize_y), Image.ANTIALIAS)

    output = StringIO()
    if image_format == 'JPEG':
        #image.save(output, image_format, quality=80, optimize=True, progressive=True)
        image.save(output, image_format, quality=90)
    else:
        image.save(output, image_format)
    output_buffer = output.getvalue()
    output.close()

    width = image.size[0]
    height = image.size[1]
    del image
    data.close()
    return output_buffer, image_format, (width, height)


def calc_size(content):
    data = StringIO(content)
    try:
        image = Image.open(data)
    except (IOError, SyntaxError, ValueError):
        data.close()
        return None
    width = image.size[0]
    height = image.size[1]
    del image
    data.close()
    return (width, height)
