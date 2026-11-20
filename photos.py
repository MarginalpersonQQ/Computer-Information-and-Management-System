"""Validate uploads, remove metadata and store bounded JPEG previews."""
import base64, binascii, io, warnings
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_ENCODED=6_000_000
MAX_PHOTOS=10

def process(value):
    if not isinstance(value,str) or len(value)>MAX_ENCODED: raise ValueError('照片過大，請先縮小後再上傳')
    try:raw=base64.b64decode(value,validate=True)
    except (ValueError,binascii.Error):raise ValueError('照片格式不正確')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in ('JPEG','PNG','WEBP'): raise ValueError('請使用 JPEG、PNG 或 WebP 照片')
                if image.width*image.height>24_000_000: raise ValueError('照片尺寸過大，請先縮小')
                image=ImageOps.exif_transpose(image)
                image.thumbnail((1600,1600))
                if image.mode in ('RGBA','LA') or 'transparency' in image.info:
                    rgba=image.convert('RGBA'); background=Image.new('RGB',rgba.size,'white');background.paste(rgba,mask=rgba.getchannel('A'));image=background
                else:image=image.convert('RGB')
                image.info.clear()
                full=io.BytesIO();image.save(full,format='JPEG',quality=82,optimize=True)
                image.thumbnail((320,320));thumb=io.BytesIO();image.save(thumb,format='JPEG',quality=75,optimize=True)
                if len(full.getvalue())>2_000_000: raise ValueError('照片過大，請選擇較小的照片')
                return full.getvalue(),thumb.getvalue()
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError('無法讀取照片，請改選其他 JPEG、PNG 或 WebP 圖片')
