"""Decode every image frame and record its dimensions and byte fingerprint."""
import hashlib
import io
import warnings

from dependencies import enable_bundled_dependencies


def require_pillow():
    enable_bundled_dependencies()
    try:
        from PIL import Image
    except ImportError:
        from downloader import DownloadError
        raise DownloadError("사진 검사 구성요소가 없습니다. requirements.txt의 패키지를 설치해 주세요.") from None
    return Image


def inspect_image(content):
    from downloader import DownloadError
    Image = require_pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                size = image.size
                image_format = image.format
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                frames = getattr(image, "n_frames", 1)
                for frame in range(frames):
                    image.seek(frame)
                    image.load()
        return {"sha256": hashlib.sha256(content).hexdigest(), "width": size[0],
                "height": size[1], "format": image_format, "frames": frames,
                "integrity": "ok"}
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as error:
        raise DownloadError(f"사진 파일이 손상되었거나 읽을 수 없습니다: {error}") from None
