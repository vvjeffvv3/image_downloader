"""Public integration entry points and platform-specific storage roots."""
from pathlib import Path

import downloader
import x_downloader
from downloader import DownloadError

PLATFORMS = {"youtube": "YouTube", "x": "X"}


def platform_folder(platform, output):
    if platform not in PLATFORMS:
        raise DownloadError("유튜브 또는 X를 선택해 주세요.")
    return Path(output).expanduser().resolve() / PLATFORMS[platform]


def normalize_urls(platform, urls):
    if platform not in PLATFORMS:
        raise DownloadError("유튜브 또는 X를 선택해 주세요.")
    if not urls:
        raise DownloadError("링크를 한 개 이상 입력해 주세요.")
    normalize = downloader.normalize_url if platform == "youtube" else x_downloader.normalize_url
    return [normalize(url) for url in urls]


def run(platform, operation, urls, output, limit=100, stop=None, log=print,
        cookies_path=None, interval=1.0, control=None, check_original=True, repair=False):
    root = platform_folder(platform, output)
    if platform == "x":
        if operation != "download":
            raise DownloadError("이어받기·실패 재시도·사진 검사는 유튜브에서 사용할 수 있습니다.")
        return x_downloader.download_links(urls, root, limit, stop, log, cookies_path, interval, control)
    if operation in {"download", "resume"}:
        return downloader.download_links(urls, root, limit, stop, log,
                                         resume=operation == "resume", interval=interval, control=control)
    from youtube_jobs import inspect_saved, retry_failed
    if operation == "retry":
        return retry_failed(root, stop, log, interval, control, scan_root=output)
    if operation == "inspect":
        return inspect_saved(root, check_original, repair, stop, log, interval, control, scan_root=output)
    raise DownloadError("지원하지 않는 작업입니다.")
