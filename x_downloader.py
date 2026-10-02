"""X single-post and media-feed collection, separate from the YouTube extractor."""
import copy
import http.cookiejar
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from dependencies import enable_bundled_dependencies
from downloader import (
    Cancelled, DownloadError, Post, Report, YouTubeClient, allowed_image_url,
    save_post, write_json,
)
from image_checks import require_pillow
from run_control import RunControl

X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
USERNAME = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
RESERVED = {"home", "search", "explore", "notifications", "messages", "settings", "i", "intent", "share", "compose"}


@dataclass(frozen=True)
class XTarget:
    url: str
    post_id: str | None = None
    username: str = ""


def normalize_url(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in X_HOSTS:
            raise ValueError
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError
    except ValueError:
        raise DownloadError("x.com 또는 twitter.com의 게시글·계정 링크를 입력해 주세요.") from None
    path = parsed.path.rstrip("/")
    single = re.fullmatch(r"/(i/web|[A-Za-z0-9_]{1,15})/status/(\d{1,25})(?:/(?:photo|video)/\d+)?", path)
    if single:
        user, post_id = single.groups()
        if user != "i/web" and user.lower() in RESERVED:
            raise DownloadError("X 게시글 링크 형식을 확인해 주세요.")
        return XTarget(f"https://x.com/{user}/status/{post_id}", post_id, "" if user == "i/web" else user)
    account = re.fullmatch(r"/([A-Za-z0-9_]{1,15})(?:/media)?", path)
    if account and account[1].lower() not in RESERVED:
        return XTarget(f"https://x.com/{account[1]}/media", username=account[1])
    raise DownloadError("X 게시글(/계정/status/번호) 또는 계정 미디어 탭(/계정/media) 링크를 입력해 주세요.")


def image_candidates(url):
    url = allowed_image_url(url, ("pbs.twimg.com",))
    parsed = urlsplit(url)
    if not parsed.path.startswith("/media/"):
        raise DownloadError("X 게시글의 첨부 사진 주소가 아닙니다.")
    query = parse_qs(parsed.query)
    path = parsed.path
    extension = query.get("format", [None])[0]
    if not extension:
        path, dot, extension = path.rpartition(".")
        if not dot:
            raise DownloadError("X 사진 파일 형식을 확인할 수 없습니다.")
        extension = extension.split(":", 1)[0]
    if extension not in {"jpg", "jpeg", "png", "webp", "gif"}:
        raise DownloadError("지원하지 않는 X 사진 형식입니다.")
    return [urlunsplit(("https", "pbs.twimg.com", path,
                       urlencode({"format": extension, "name": size}), ""))
            for size in ("orig", "4096x4096", "large", "medium", "small")]


class XImageClient(YouTubeClient):
    image_domains = ("pbs.twimg.com",)
    site_hosts = X_HOSTS
    origin = "https://x.com"
    service_name = "X"

    def validate_image_url(self, url):
        validated = super().validate_image_url(url)
        if not urlsplit(validated).path.startswith("/media/"):
            raise DownloadError("X 첨부 사진 주소가 아닙니다.")
        return validated


def load_cookies(path):
    if not path:
        return None
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise DownloadError("선택한 쿠키 파일을 읽을 수 없습니다.")
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(str(path), ignore_discard=True, ignore_expires=False)
    except (OSError, http.cookiejar.LoadError):
        raise DownloadError("Netscape 형식의 X cookies.txt 파일을 선택해 주세요.") from None
    selected = http.cookiejar.CookieJar()
    for cookie in jar:
        if cookie.domain.lstrip(".") in {"x.com", "twitter.com"} and not cookie.is_expired():
            cookie = copy.copy(cookie)
            cookie.domain = ".x.com"
            cookie.domain_initial_dot = True
            selected.set_cookie(cookie)
    if not any(cookie.name == "auth_token" and cookie.value for cookie in selected):
        raise DownloadError("쿠키 파일에 사용 가능한 X 로그인 정보가 없습니다. 로그인한 계정의 쿠키를 다시 선택해 주세요.")
    return selected


def create_extractor(target, control, cookies):
    enable_bundled_dependencies()
    try:
        from gallery_dl import extractor
    except ImportError:
        raise DownloadError("X 수집 구성요소가 없습니다. requirements.txt의 패키지를 설치해 주세요.") from None
    worker = extractor.find(target.url)
    if worker is None or worker.category != "twitter":
        raise DownloadError("이 X 링크를 처리할 수 없습니다.")
    options = {"videos": False, "previews": False, "articles": False, "cards": False,
               "quoted": False, "retweets": False, "text-tweets": False, "conversations": False,
               "username": None, "password": None, "netrc": False, "cookies": None,
               "proxy-env": False, "timeout": 20, "retries": 2, "sleep-request": 0,
               "ratelimit": "abort", "sleep-429": 3, "write-pages": False}
    worker.config = lambda key, default=None: options.get(key, default)
    # An isolated logger prevents gallery-dl diagnostics from exposing session data
    # in the launcher log. User-facing failures are classified below.
    worker.log = logging.Logger("photo_downloader.x", level=logging.CRITICAL + 1)
    worker.log.addHandler(logging.NullHandler())
    worker.log.traceback = lambda *args, **kwargs: None
    original_cache = worker.cache
    worker.cache = lambda *args, **kwargs: original_cache(*args, **{**kwargs, "_mem": True})
    worker.initialize()
    worker.session.trust_env = False
    if cookies:
        for cookie in cookies:
            worker.cookies.set_cookie(copy.copy(cookie))
        # TwitterAPI is initialized before the explicit cookies are inserted.
        from gallery_dl.extractor.twitter import TwitterAPI
        worker.api = TwitterAPI(worker)
    original_request = worker.request

    def request(*args, **kwargs):
        control.before_request()
        try:
            result = original_request(*args, **kwargs)
            control.check()
            return result
        finally:
            control.after_request()

    worker.request = request
    worker.sleep = lambda seconds, reason=None: control.wait(seconds)
    return worker


def messages_to_posts(messages, target, limit, control):
    enable_bundled_dependencies()
    from gallery_dl.extractor.message import Message
    current = None
    expected = processed = yielded = 0
    seen = set()

    def make_post(data):
        post_id = str(data.get("tweet_id", ""))
        author = data.get("author") or data.get("user") or {}
        user = author.get("name", "")
        if not re.fullmatch(r"\d{1,25}", post_id) or not USERNAME.fullmatch(user):
            return None
        if target.post_id and post_id != target.post_id:
            return None
        if post_id in seen:
            return None
        return Post(post_id, user, str(data.get("content", "")), str(data.get("date", "")),
                    platform="x", source_url=f"https://x.com/{user}/status/{post_id}")

    for message in messages:
        control.check()
        if message[0] == Message.Directory:
            if current and current.images:
                seen.add(current.post_id)
                yield current
                yielded += 1
                if target.post_id or (limit and yielded >= limit):
                    return
            current = make_post(message[-1])
            expected = int(message[-1].get("count", 0))
            processed = 0
        elif message[0] == Message.Url and current:
            processed += 1
            data = message[-1]
            if data.get("type") in (None, "photo"):
                try:
                    candidates = image_candidates(message[1])
                    if candidates not in current.images:
                        current.images.append(candidates)
                except DownloadError:
                    pass
            if expected and processed >= expected:
                if current.images:
                    seen.add(current.post_id)
                    yield current
                    yielded += 1
                    if target.post_id or (limit and yielded >= limit):
                        return
                current = None
    if current and current.images:
        yield current


def x_error(error):
    name = type(error).__name__
    if name == "NotFoundError":
        return "게시글 또는 계정을 찾을 수 없습니다. 삭제·비공개 여부와 주소를 확인해 주세요."
    if name in {"AuthenticationError", "AuthorizationError", "AuthRequired"}:
        return "X 로그인 확인이 필요합니다. 사용 가능한 로그인 쿠키 파일을 선택해 주세요."
    return "X 정보를 가져오지 못했습니다. 로그인 쿠키·접속 제한·인터넷 연결을 확인해 주세요."


def download_links(urls, output, limit=100, stop=None, log=print, cookies_path=None,
                   interval=1.0, control=None, client=None, extractor_factory=None):
    if not urls or limit < 0:
        raise DownloadError("X 링크와 0 이상의 게시글 수를 입력해 주세요.")
    targets = [normalize_url(url) for url in urls]
    if not cookies_path and any(not target.post_id for target in targets):
        raise DownloadError("X 계정 미디어 탭 조회에는 로그인 쿠키가 필요합니다. 본인 계정의 cookies.txt 파일을 선택해 주세요.")
    cookies = load_cookies(cookies_path)
    require_pillow()
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    control = control or RunControl(stop, interval)
    client = client or XImageClient(control=control)
    factory = extractor_factory or create_extractor
    report = Report()
    seen = set()
    try:
        for target in targets:
            worker = None
            try:
                control.check()
                log(f"X 게시글 확인: {target.url}")
                worker = factory(target, control, cookies)
                found = False
                for post in messages_to_posts(iter(worker), target, limit, control):
                    found = True
                    if post.post_id not in seen:
                        save_post(post, output, client, report, log)
                        seen.add(post.post_id)
                if not found:
                    log("다운로드할 첨부 사진이 없습니다. 비공개·접속 제한이 있다면 로그인 쿠키를 확인해 주세요.")
            except Cancelled:
                raise
            except (DownloadError, OSError) as error:
                message = str(error)
                report.errors.append(f"{target.url}: {message}")
                log(f"오류: {message}")
            except Exception as error:
                message = x_error(error)
                report.errors.append(f"{target.url}: {message}")
                log(f"오류: {message}")
            finally:
                if worker and getattr(worker, "session", None):
                    worker.session.close()
    except (Cancelled, KeyboardInterrupt):
        report.cancelled = True
        log("중지했습니다. 지금까지 저장한 사진은 유지됩니다.")
    finally:
        manifest = output / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        report.manifest = str(manifest)
        write_json(manifest, {"platform": "x", "source_urls": [target.url for target in targets], **asdict(report)})
    log(report.summary())
    log(f"저장 폴더: {output}")
    return report
