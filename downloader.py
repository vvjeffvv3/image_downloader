"""YouTube collection and shared, atomic photo storage."""
from __future__ import annotations

import copy
import gzip
import hashlib
import http.client
import http.cookiejar
import json
import re
import socket
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from image_checks import inspect_image, require_pillow
from run_control import RunControl

BASE = "https://www.youtube.com"
POST_ID = re.compile(r"Ug[A-Za-z0-9_-]{8,150}\Z")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
IMAGE_DOMAINS = ("ggpht.com", "googleusercontent.com", "ytimg.com")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class DownloadError(Exception):
    """An error that can be shown directly to the user."""


class Cancelled(DownloadError):
    pass


@dataclass(frozen=True)
class Target:
    url: str
    post_id: str | None = None


@dataclass
class Post:
    post_id: str
    author: str
    text: str
    published: str
    images: list[list[str]] = field(default_factory=list)
    platform: str = "youtube"
    source_url: str = ""

    @property
    def url(self) -> str:
        return self.source_url or f"{BASE}/post/{self.post_id}"


@dataclass
class Report:
    posts: int = 0
    images_saved: int = 0
    images_skipped: int = 0
    images_failed: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    manifest: str = ""
    posts_without_photos: int = 0
    images_checked: int = 0
    images_damaged: int = 0
    images_original: int = 0
    images_different: int = 0
    images_original_unknown: int = 0
    images_repaired: int = 0
    operation: str = "download"

    def summary(self) -> str:
        label = "중지됨" if self.cancelled else (
            "일부 실패" if self.errors or self.images_failed else "완료"
        )
        if self.operation == "inspect":
            return (f"{label} · 검사 {self.images_checked}장 · 손상/누락 {self.images_damaged}장 · "
                    f"최대 크기 일치 {self.images_original}장 · 차이 {self.images_different}장 · "
                    f"대조 불가 {self.images_original_unknown}장 · 복구 {self.images_repaired}장")
        return (
            f"{label} · 게시글 {self.posts}개 · 새 사진 {self.images_saved}장 · "
            f"기존 사진 {self.images_skipped}장 · 실패 사진 {self.images_failed}장"
        )


def normalize_url(value: str) -> Target:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in YOUTUBE_HOSTS:
            raise ValueError
        if parsed.username or parsed.password or parsed.port not in (None, 80, 443):
            raise ValueError
    except ValueError:
        raise DownloadError("youtube.com의 게시글 또는 채널 링크를 입력해 주세요.") from None
    parts = [part for part in parsed.path.split("/") if part]
    query_id = parse_qs(parsed.query).get("lb", [None])[0]
    post_id = parts[1] if len(parts) == 2 and parts[0] == "post" else query_id
    if post_id:
        if not POST_ID.fullmatch(post_id):
            raise DownloadError("게시글 ID가 올바르지 않습니다. 공유 링크를 다시 확인해 주세요.")
        return Target(f"{BASE}/post/{post_id}", post_id)
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        channel = parts[:1]
    elif len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
        channel = parts[:2]
    else:
        raise DownloadError(
            "게시글 공유 링크(/post/...) 또는 채널 링크(/@채널명/posts)를 입력해 주세요. "
            "동영상 링크는 지원하지 않습니다."
        )
    if len(parts) > len(channel) + 1:
        raise DownloadError("채널 링크 형식을 확인해 주세요.")
    encoded_channel = "/".join(quote(unquote(part), safe="@_-.") for part in channel)
    return Target(f"{BASE}/{encoded_channel}/posts")


def walk_dicts(value: object, skip_posts: bool = False) -> Iterator[dict]:
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if skip_posts and key in ("backstagePostThreadRenderer", "backstagePostRenderer", "postRenderer"):
                continue
            yield from walk_dicts(child, skip_posts)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child, skip_posts)


def text_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    return value.get("simpleText", value.get("content", "")) or "".join(
        str(run.get("text", "")) for run in value.get("runs", [])
    )


def parse_page(page: str) -> tuple[dict, dict]:
    decoder = json.JSONDecoder()
    data = None
    pattern = r'(?:var\s+)?ytInitialData\s*=\s*|window\s*\[\s*[\"\x27]ytInitialData[\"\x27]\s*\]\s*=\s*'
    for match in re.finditer(pattern, page):
        try:
            candidate, _ = decoder.raw_decode(page[match.end():])
            if isinstance(candidate, dict):
                data = candidate
                break
        except ValueError:
            continue
    if data is None:
        raise DownloadError(
            "게시글 정보를 읽을 수 없습니다. 로그인·동의·접속 확인 화면이 나오는지 "
            "브라우저에서 확인해 주세요. 유튜브 화면 구조가 바뀌었을 수도 있습니다."
        )
    config = {}
    for match in re.finditer(r"ytcfg\.set\(\s*", page):
        try:
            candidate, _ = decoder.raw_decode(page[match.end():])
            if isinstance(candidate, dict):
                config.update(candidate)
        except ValueError:
            continue
    return data, config


def selected_feed(data: dict) -> object:
    for node in walk_dicts(data.get("contents", {})):
        tab = node.get("tabRenderer", {})
        if tab.get("selected") and "content" in tab:
            return tab["content"]
    return data.get("contents", data)


def allowed_image_url(url: str, domains: tuple[str, ...] = IMAGE_DOMAINS) -> str:
    url = "https:" + url if url.startswith("//") else url
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            raise ValueError
    except ValueError:
        raise DownloadError("지원하지 않는 사진 주소입니다.") from None
    return url


def image_candidates(thumbnails: list[dict]) -> list[str]:
    if not thumbnails:
        return []
    thumbnails = sorted(thumbnails, key=lambda item: item.get("width", 0) * item.get("height", 0), reverse=True)
    urls = []
    for item in thumbnails:
        try:
            url = allowed_image_url(item.get("url", ""))
        except DownloadError:
            continue
        parsed = urlsplit(url)
        if not urls and any((parsed.hostname or "").endswith(domain) for domain in IMAGE_DOMAINS[:2]):
            # Google image size/crop options follow '='. s0 requests the uncropped full image.
            original = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.split("=", 1)[0] + "=s0", "", ""))
            urls.append(original)
        if url not in urls:
            urls.append(url)
    return urls


def extract_posts(feed: object) -> list[Post]:
    result = []
    seen = set()
    for node in walk_dicts(feed):
        renderer = node.get("backstagePostRenderer") or node.get("postRenderer")
        if not isinstance(renderer, dict):
            continue
        post_id = renderer.get("postId", "")
        if not isinstance(post_id, str) or not POST_ID.fullmatch(post_id) or post_id in seen:
            continue
        seen.add(post_id)
        images = []
        identities = set()
        # Deliberately inspect photo attachments only; avatars and video thumbnails are unrelated.
        attachment = renderer.get("backstageAttachment", renderer.get("attachment", {}))
        for attachment_node in walk_dicts(attachment):
            image = attachment_node.get("backstageImageRenderer")
            if not isinstance(image, dict):
                continue
            candidates = image_candidates(image.get("image", {}).get("thumbnails", []))
            if candidates and candidates[0] not in identities:
                identities.add(candidates[0])
                images.append(candidates)
        result.append(Post(
            post_id=post_id,
            author=text_value(renderer.get("authorText")) or "YouTube",
            text=text_value(renderer.get("contentText")),
            published=text_value(renderer.get("publishedTimeText")),
            images=images,
        ))
    return result


def continuation_token(feed: object) -> str | None:
    for node in walk_dicts(feed, skip_posts=True):
        item = node.get("continuationItemRenderer")
        if isinstance(item, dict):
            endpoint = item.get("continuationEndpoint", {})
            command = endpoint.get("continuationCommand", {})
            token = command.get("token")
            if token:
                return token
        # Older layouts put a token directly on section/list continuations.
        for entry in node.get("continuations", []):
            token = entry.get("nextContinuationData", {}).get("continuation")
            if token:
                return token
    return None


def continuation_feed(data: dict) -> object:
    batches = []
    for key in ("onResponseReceivedActions", "onResponseReceivedEndpoints", "onResponseReceivedCommands"):
        for command in data.get(key, []):
            for name in ("appendContinuationItemsAction", "reloadContinuationItemsCommand"):
                items = command.get(name, {}).get("continuationItems")
                if items is not None:
                    batches.extend(items)
    return batches if batches else data.get("continuationContents", {})


class YouTubeClient:
    image_domains = IMAGE_DOMAINS
    site_hosts = YOUTUBE_HOSTS
    origin = BASE
    service_name = "유튜브"

    def __init__(self, stop: threading.Event | None = None, timeout: float = 20,
                 interval: float = 1.0, control: RunControl | None = None):
        self.control = control or RunControl(stop, interval)
        self.stop = self.control.stop
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def check_cancelled(self) -> None:
        self.control.check()

    def pause(self, seconds: float) -> None:
        self.control.wait(seconds)

    def validate_image_url(self, url: str) -> str:
        return allowed_image_url(url, self.image_domains)

    def request(self, url: str, payload: dict | None = None, max_bytes: int = 16 * 1024 * 1024) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", "Referer": self.origin + "/"}
        body = None
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Origin": BASE})
            client = payload.get("context", {}).get("client", {})
            headers["X-Youtube-Client-Name"] = "1"
            headers["X-Youtube-Client-Version"] = str(client.get("clientVersion", ""))
            if client.get("visitorData"):
                headers["X-Goog-Visitor-Id"] = client["visitorData"]
            body = json.dumps(payload).encode("utf-8")
        for attempt in range(3):
            self.control.before_request()
            try:
                with self.opener.open(Request(url, data=body, headers=headers), timeout=self.timeout) as response:
                    final_host = urlsplit(response.url).hostname or ""
                    if "/youtubei/" in url and final_host not in YOUTUBE_HOSTS:
                        raise DownloadError("유튜브가 로그인 또는 접속 확인 화면으로 이동했습니다.")
                    if urlsplit(url).hostname not in self.site_hosts:
                        self.validate_image_url(response.url)
                    chunks = []
                    length = 0
                    while True:
                        self.check_cancelled()
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        length += len(chunk)
                        if length > max_bytes:
                            raise DownloadError("응답 파일이 허용 크기를 초과했습니다.")
                        chunks.append(chunk)
                    expected = response.headers.get("Content-Length")
                    if expected and length != int(expected):
                        raise DownloadError("파일 수신이 중간에 끊겼습니다. 다시 시도해 주세요.")
                    content = b"".join(chunks)
                    if response.headers.get("Content-Encoding") == "gzip":
                        content = gzip.decompress(content)
                    return content
            except HTTPError as error:
                error.close()
                retry = error.code in (429, 500, 502, 503, 504)
                message = f"{self.service_name} 요청 실패 (HTTP {error.code}). 잠시 후 다시 시도해 주세요."
            except (URLError, OSError, socket.timeout, http.client.HTTPException) as error:
                retry = True
                message = f"인터넷 연결 또는 응답 시간 문제: {error}"
            finally:
                self.control.after_request()
            if not retry or attempt == 2:
                raise DownloadError(message) from None
            self.pause(1 + attempt * 2)
        raise DownloadError("요청을 완료하지 못했습니다.")

    def iter_posts(self, target: Target, limit: int, log: Callable[[str], None],
                   state: dict | None = None, checkpoint: Callable[[], None] | None = None) -> Iterator[Post]:
        state = state if state is not None else {}
        checkpoint = checkpoint or (lambda: None)
        if state.get("finished"):
            log(f"이미 끝까지 확인한 주소: {target.url}")
            return
        log(f"게시글 확인: {target.url}")
        if not state.get("started"):
            page = self.request(target.url + "?hl=en").decode("utf-8", errors="replace")
            data, config = parse_page(page)
            feed = selected_feed(data)
            posts = extract_posts(feed)
            if target.post_id:
                posts = [post for post in posts if post.post_id == target.post_id]
                if not posts:
                    raise DownloadError("게시글을 찾을 수 없습니다. 삭제되었거나 로그인이 필요한 게시글일 수 있습니다.")
            context = copy.deepcopy(config.get("INNERTUBE_CONTEXT", {}))
            client = context.setdefault("client", {})
            version = client.get("clientVersion") or config.get("INNERTUBE_CLIENT_VERSION")
            client.update({"clientName": "WEB", "hl": "en", "gl": "US"})
            if version:
                client["clientVersion"] = version
            visitor = config.get("VISITOR_DATA") or data.get("responseContext", {}).get("visitorData")
            if visitor:
                client["visitorData"] = visitor
            state.update({"started": True, "finished": False, "context": context,
                          "version": version, "api_key": config.get("INNERTUBE_API_KEY"),
                          "pending": [asdict(post) for post in posts],
                          "next_token": None if target.post_id else continuation_token(feed),
                          "seen_ids": [], "seen_tokens": [], "page_number": 1})
            checkpoint()
        else:
            log(f"기록된 위치에서 이어받기: {state.get('page_number', 1)}번째 페이지")
        seen_ids = set(state.get("seen_ids", []))
        seen_tokens = set(state.get("seen_tokens", []))
        while True:
            self.check_cancelled()
            while state["pending"]:
                self.check_cancelled()
                post = Post(**state["pending"][0])
                if post.post_id in seen_ids:
                    state["pending"].pop(0)
                    checkpoint()
                    continue
                # The caller saves the post before resuming the generator. A stop while
                # saving leaves it pending, so the incomplete album is retried on resume.
                yield post
                seen_ids.add(post.post_id)
                state["seen_ids"] = sorted(seen_ids)
                state["pending"].pop(0)
                checkpoint()
                if limit and len(seen_ids) >= limit:
                    log(f"설정한 게시글 수({limit}개)에 도달했습니다.")
                    state["finished"] = True
                    checkpoint()
                    return
            token = state.get("next_token")
            if not token:
                if not seen_ids:
                    raise DownloadError("공개 게시글을 찾지 못했습니다. 채널의 게시글 탭을 확인해 주세요.")
                log("마지막 게시글까지 확인했습니다.")
                state["finished"] = True
                checkpoint()
                return
            if token in seen_tokens:
                raise DownloadError("다음 페이지에서 새 게시글을 확인하지 못했습니다. 저장된 자료는 유지됩니다.")
            if not state.get("version"):
                raise DownloadError("다음 페이지를 읽는 데 필요한 유튜브 정보를 찾지 못했습니다.")
            payload = {"context": state["context"], "continuation": token}
            endpoint = BASE + "/youtubei/v1/browse?" + urlencode({
                "prettyPrint": "false", **({"key": state["api_key"]} if state.get("api_key") else {})
            })
            try:
                response = json.loads(self.request(endpoint, payload))
            except ValueError:
                raise DownloadError("다음 페이지 응답을 해석할 수 없습니다.") from None
            if response.get("error"):
                raise DownloadError("유튜브가 다음 페이지 요청을 거절했습니다.")
            feed = continuation_feed(response)
            if not feed:
                raise DownloadError("다음 페이지에 게시글 정보가 없습니다. 저장된 자료는 유지됩니다.")
            posts = [post for post in extract_posts(feed) if post.post_id not in seen_ids]
            next_token = continuation_token(feed)
            if next_token and not posts:
                raise DownloadError("다음 페이지에서 새 게시글을 확인하지 못했습니다. 저장된 자료는 유지됩니다.")
            seen_tokens.add(token)
            state.update({"pending": [asdict(post) for post in posts], "next_token": next_token,
                          "seen_tokens": sorted(seen_tokens), "page_number": state["page_number"] + 1})
            checkpoint()
            log(f"{state['page_number']}번째 페이지 확인 중…")


def safe_name(value: str, length: int = 32) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:length].rstrip(" .")
    if not value or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "channel_" + value
    return value


def image_extension(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    if len(content) >= 12 and content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"):
        return ".avif"
    raise DownloadError("사진 대신 다른 형식의 응답을 받았습니다.")


def write_atomic(path: Path, content: bytes) -> None:
    temp = path.with_name(path.name + ".part")
    try:
        temp.write_bytes(content)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: object) -> None:
    write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path) -> dict:
    if path.stat().st_size > 16 * 1024 * 1024:
        raise DownloadError("저장 기록이 너무 커서 읽을 수 없습니다.")
    try:
        value = json.loads(path.read_text("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise DownloadError(f"저장 기록을 읽을 수 없습니다: {path.name}") from None


def saved_image_path(folder: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or "\\" in filename:
        raise DownloadError("저장 사진의 파일 이름이 올바르지 않습니다.")
    path = folder / filename
    if path.resolve().parent != folder.resolve():
        raise DownloadError("저장 폴더 밖의 파일은 처리하지 않습니다.")
    return path


def full_size_url(url: str) -> bool:
    return url.endswith("=s0") or parse_qs(urlsplit(url).query).get("name") == ["orig"]


def save_image(client: YouTubeClient, candidates: list[str], folder: Path, number: int,
               previous: dict | None = None, force: bool = False, prefetched: dict | None = None) -> dict:
    if not candidates:
        raise DownloadError("사진 주소가 없습니다.")
    previous = previous or {}
    digest = hashlib.sha256(candidates[0].encode()).hexdigest()[:10]
    stem = f"image_{number:02d}_{digest}"
    for suffix in (() if force else (".jpg", ".png", ".gif", ".webp", ".avif")):
        path = folder / (stem + suffix)
        if path.is_file() and path.stat().st_size > 16:
            try:
                if path.stat().st_size > 100 * 1024 * 1024:
                    continue
                content = path.read_bytes()
                details = inspect_image(content)
                if image_extension(content) == suffix and (
                        not previous.get("sha256") or previous["sha256"] == details["sha256"]):
                    return {**previous, **details, "status": "skipped", "file": path.name,
                            "bytes": len(content)}
            except (DownloadError, OSError):
                pass
    last_error = "사진 주소가 없습니다."
    for url in candidates:
        client.check_cancelled()
        try:
            validated = client.validate_image_url(url)
            content = (prefetched[url] if prefetched and url in prefetched else
                       client.request(validated, max_bytes=100 * 1024 * 1024))
            suffix = image_extension(content)
            details = inspect_image(content)
            path = folder / (stem + suffix)
            write_atomic(path, content)
            return {**details, "status": "saved", "file": path.name, "bytes": len(content),
                    "download_url": url, "quality": "full" if full_size_url(url) else "available"}
        except Cancelled:
            raise
        except DownloadError as error:
            last_error = str(error)
    raise DownloadError(last_error)


def save_post(post: Post, root: Path, client: YouTubeClient, report: Report, log: Callable[[str], None]) -> None:
    valid_id = POST_ID.fullmatch(post.post_id) if post.platform == "youtube" else re.fullmatch(r"\d{1,25}", post.post_id)
    if not valid_id or post.platform not in ("youtube", "x"):
        raise DownloadError("게시글 식별 정보가 올바르지 않습니다.")
    if not post.images:
        report.posts_without_photos += 1
        log("사진이 없는 게시글은 건너뜁니다.")
        return
    folder = root / f"{safe_name(post.author)}__{post.post_id}"
    if folder.resolve().parent != root.resolve():
        raise DownloadError("저장 폴더 밖에는 게시글을 저장하지 않습니다.")
    folder.mkdir(parents=True, exist_ok=True)
    old_images = {}
    if (folder / "post.json").is_file():
        try:
            old = read_json(folder / "post.json")
            if old.get("post_id") == post.post_id and old.get("platform", "youtube") == post.platform:
                old_images = {item.get("source_url"): item for item in old.get("images", []) if isinstance(item, dict)}
        except DownloadError:
            log("기존 게시글 기록을 읽을 수 없어 새 기록을 만듭니다.")
    metadata = {
        "platform": post.platform, "post_id": post.post_id, "url": post.url, "author": post.author, "text": post.text,
        "published_display": post.published, "saved_at": datetime.now(timezone.utc).isoformat(),
        "images": [{**old_images.get(candidates[0], {}), "source_url": candidates[0],
                    "candidates": candidates, "status": "pending"} for candidates in post.images],
    }
    body = f"작성자: {post.author}\n게시 시점: {post.published}\n링크: {post.url}\n\n{post.text}\n"
    write_atomic(folder / "post.txt", body.encode("utf-8-sig"))
    write_json(folder / "post.json", metadata)
    report.posts += 1
    report.folders.append(str(folder))
    log(f"게시글 {report.posts}: {post.author} · 사진 {len(post.images)}장")
    for index, candidates in enumerate(post.images):
        try:
            client.check_cancelled()
            outcome = save_image(client, candidates, folder, index + 1, old_images.get(candidates[0]))
            metadata["images"][index].update(outcome)
            metadata["images"][index].pop("error", None)
            if outcome["status"] == "saved":
                metadata["images"][index].pop("verification", None)
            if outcome["status"] == "skipped":
                report.images_skipped += 1
                log(f"  사진 {index + 1}: 이미 저장되어 있음")
            else:
                report.images_saved += 1
                quality = "원본 크기" if outcome["quality"] == "full" else "제공되는 크기"
                log(f"  사진 {index + 1}: 저장됨 ({quality})")
        except (Cancelled, KeyboardInterrupt):
            metadata["images"][index]["status"] = "cancelled"
            raise
        except DownloadError as error:
            report.images_failed += 1
            metadata["images"][index].update({"status": "failed", "error": str(error)})
            log(f"  사진 {index + 1}: 실패 — {error}")
        finally:
            write_json(folder / "post.json", metadata)


def load_checkpoint(output: Path) -> dict:
    path = output / "checkpoint.json"
    if not path.is_file():
        raise DownloadError("이 폴더에 이어받기 기록이 없습니다. 먼저 사진을 받아 주세요.")
    data = read_json(path)
    if data.get("platform") != "youtube" or data.get("version") != 1 or not data.get("source_urls"):
        raise DownloadError("이어받기 기록 형식이 올바르지 않습니다.")
    if data.get("completed"):
        raise DownloadError("이전에 요청한 범위는 이미 완료했습니다. 새로 확인하려면 ‘사진 받기’를 누르세요.")
    if not isinstance(data.get("states"), dict) or not isinstance(data.get("limit"), int) or data["limit"] < 0:
        raise DownloadError("이어받기 기록 형식이 올바르지 않습니다.")
    return data


def download_links(urls: list[str], output: Path, limit: int = 100,
                   stop: threading.Event | None = None, log: Callable[[str], None] = print,
                   client: YouTubeClient | None = None, resume: bool = False,
                   interval: float = 1.0, control: RunControl | None = None) -> Report:
    output = output.expanduser().resolve()
    checkpoint_data = load_checkpoint(output) if resume else None
    if checkpoint_data:
        urls, limit = checkpoint_data["source_urls"], checkpoint_data["limit"]
    if limit < 0:
        raise DownloadError("게시글 수는 0 이상의 숫자로 입력해 주세요.")
    if not urls:
        raise DownloadError("링크를 한 개 이상 입력해 주세요.")
    targets = [normalize_url(url) for url in urls]
    require_pillow()
    output.mkdir(parents=True, exist_ok=True)
    client = client if client is not None else YouTubeClient(stop, interval=interval, control=control)
    report = Report()
    manifest_path = output / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    report.manifest = str(manifest_path)
    checkpoint_data = checkpoint_data or {"version": 1, "platform": "youtube",
        "source_urls": [target.url for target in targets], "limit": limit, "states": {}, "completed": False}

    def checkpoint():
        checkpoint_data["updated_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint_data["completed"] = all(checkpoint_data["states"].get(target.url, {}).get("finished") for target in targets)
        write_json(output / "checkpoint.json", checkpoint_data)

    checkpoint()
    seen_ids = set()
    try:
        for target in targets:
            try:
                state = checkpoint_data["states"].setdefault(target.url, {})
                for post in client.iter_posts(target, limit, log, state=state, checkpoint=checkpoint):
                    client.check_cancelled()
                    if post.post_id not in seen_ids:
                        save_post(post, output, client, report, log)
                        seen_ids.add(post.post_id)
            except Cancelled:
                raise
            except (DownloadError, OSError) as error:
                message = f"{target.url}: {error}"
                report.errors.append(message)
                log(f"오류: {message}")
    except (Cancelled, KeyboardInterrupt):
        report.cancelled = True
        log("중지했습니다. 지금까지 저장한 파일은 유지됩니다.")
    finally:
        checkpoint()
        write_json(manifest_path, {"platform": "youtube", "resumed": resume,
                                  "source_urls": [target.url for target in targets], **asdict(report)})
    log(report.summary())
    log(f"저장 폴더: {output}")
    return report
