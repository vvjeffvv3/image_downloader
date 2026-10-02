"""Remember explicit cookie-file choices without copying authentication data."""
from pathlib import Path

from downloader import DownloadError, read_json, write_json

DEFAULT_SETTINGS = Path(__file__).resolve().parent / "settings.json"


class SettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_SETTINGS

    def cookie_path(self):
        if not self.path.exists():
            return ""
        data = read_json(self.path)
        filename = data.get("x_cookies_path", "")
        if data.get("version") != 1 or not isinstance(filename, str) or "\x00" in filename:
            raise DownloadError("저장한 쿠키 파일 선택 정보를 읽을 수 없습니다. 파일을 다시 선택해 주세요.")
        # Keep the choice even if the cookie expires or its file is temporarily absent.
        # Authentication is checked when a download starts, rather than at startup.
        return filename

    def remember_cookie_path(self, filename):
        value = str(Path(filename).expanduser().resolve()) if filename else ""
        try:
            data = read_json(self.path) if self.path.exists() else {}
        except (DownloadError, OSError):
            data = {}
        data.update(version=1, x_cookies_path=value)
        write_json(self.path, data)
        return value
