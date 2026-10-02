import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pipelines
import x_downloader
from dependencies import enable_bundled_dependencies
from downloader import BASE, Cancelled, DownloadError, download_links, read_json
from image_checks import inspect_image, require_pillow
from run_control import RunControl
from test_downloader import FakeClient, GIF, next_item, page_html, post
from youtube_jobs import inspect_saved, retry_failed

enable_bundled_dependencies()
from gallery_dl.extractor.message import Message


def png(size=(16, 12)):
    buffer = io.BytesIO()
    require_pillow().new("RGB", size, "#23aaff").save(buffer, "PNG")
    return buffer.getvalue()


class PhotosClient(FakeClient):
    def __init__(self, content=GIF, **kwargs):
        super().__init__(page_html([post()]), **kwargs)
        self.content = content

    def request(self, url, payload=None, max_bytes=None):
        self.check_cancelled()
        self.calls.append((url, payload))
        if self.fail_photos:
            raise DownloadError("접속 실패")
        return self.content


class FeatureTests(unittest.TestCase):
    def test_text_only_posts_create_no_post_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            report = download_links([BASE + "/post/UgABCDEFGHIJK"], Path(temp),
                                    client=FakeClient(page_html([post(identities=())])), log=lambda _: None)
            self.assertEqual((report.posts, report.images_saved, report.posts_without_photos), (0, 0, 1))
            self.assertFalse(list(Path(temp).rglob("post.txt")))

    def test_resume_incomplete_album_uses_checkpoint_without_refetching_feed(self):
        class StopSecond(FakeClient):
            def request(self, url, payload=None, max_bytes=None):
                if "photo-b" in url:
                    raise Cancelled("중지")
                return super().request(url, payload, max_bytes)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                                   client=StopSecond(page_html([post()])), log=lambda _: None)
            self.assertTrue(first.cancelled)
            self.assertEqual(first.images_saved, 1)
            client = FakeClient(b"must not fetch HTML")
            second = download_links([], root, resume=True, client=client, log=lambda _: None)
            self.assertEqual((second.images_saved, second.images_skipped), (1, 1))
            self.assertTrue(all("ggpht.com" in url for url, _ in client.calls))
            self.assertTrue(read_json(root / "checkpoint.json")["completed"])
            with self.assertRaises(DownloadError):
                download_links([], root, resume=True, client=client, log=lambda _: None)

    def test_resume_fetches_saved_next_page_cursor(self):
        class BrokenNext(FakeClient):
            def request(self, url, payload=None, max_bytes=None):
                if payload:
                    raise DownloadError("다음 페이지 연결 실패")
                return super().request(url, payload, max_bytes)

        response = {"onResponseReceivedActions": [{"appendContinuationItemsAction": {
            "continuationItems": [post("UgSECONDPOST1")]}}]}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links(["youtube.com/@Example"], root, limit=0,
                                   client=BrokenNext(page_html([post(), next_item("cursor-2")])), log=lambda _: None)
            self.assertTrue(first.errors)
            client = FakeClient(b"must not fetch HTML", pages={"cursor-2": response})
            second = download_links([], root, resume=True, client=client, log=lambda _: None)
            self.assertEqual(second.images_saved, 2)
            self.assertFalse(second.errors)
            self.assertEqual(client.calls[0][1]["continuation"], "cursor-2")
            self.assertFalse(any(payload is None and "youtube.com" in url for url, payload in client.calls))

    def test_failed_only_retry_does_not_scan_channel_or_redownload_success(self):
        class FailOne(FakeClient):
            def request(self, url, payload=None, max_bytes=None):
                if "photo-a" in url:
                    raise DownloadError("사진 실패")
                return super().request(url, payload, max_bytes)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                                   client=FailOne(page_html([post()])), log=lambda _: None)
            self.assertEqual((first.images_saved, first.images_failed), (1, 1))
            folder = Path(first.folders[0])
            before = read_json(folder / "post.json")["images"][1]
            client = PhotosClient()
            retried = retry_failed(root, client=client, log=lambda _: None)
            self.assertEqual((retried.images_saved, retried.images_skipped), (1, 1))
            self.assertEqual(len(client.calls), 1)
            self.assertIn("photo-a", client.calls[0][0])
            self.assertEqual(read_json(folder / "post.json")["images"][1], before)

    def test_full_decode_detects_truncated_image_with_valid_header(self):
        content = png()
        self.assertEqual(inspect_image(content)["width"], 16)
        with self.assertRaises(DownloadError):
            inspect_image(content[:40])

    def test_retry_skips_intact_files_marked_pending_by_interrupted_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                                   client=FakeClient(page_html([post()])), log=lambda _: None)
            path = Path(first.folders[0]) / "post.json"
            data = read_json(path)
            data["images"][1]["status"] = "pending"
            path.write_text(json.dumps(data), encoding="utf-8")
            client = PhotosClient()
            retried = retry_failed(root, client=client, log=lambda _: None)
            self.assertEqual((retried.images_saved, retried.images_skipped), (0, 2))
            self.assertFalse(client.calls)

    def test_local_inspection_marks_corruption_then_failed_retry_repairs_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                                   client=FakeClient(page_html([post()])), log=lambda _: None)
            folder = Path(first.folders[0])
            name = read_json(folder / "post.json")["images"][0]["file"]
            (folder / name).write_bytes(GIF[:20])
            client = PhotosClient()
            checked = inspect_saved(root, check_original=False, client=client, log=lambda _: None)
            self.assertEqual((checked.images_checked, checked.images_damaged), (2, 1))
            self.assertFalse(client.calls)
            self.assertEqual(read_json(folder / "post.json")["images"][0]["status"], "failed")
            repaired = retry_failed(root, client=client, log=lambda _: None)
            self.assertEqual(repaired.images_saved, 1)
            self.assertEqual(inspect_image((folder / name).read_bytes())["integrity"], "ok")
            self.assertTrue(list((folder / "backups").iterdir()))

    def test_original_comparison_and_optional_repair_preserve_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                client=FakeClient(page_html([post(identities=("photo-a",))]), original_fails=True), log=lambda _: None)
            folder = Path(first.folders[0])
            old = read_json(folder / "post.json")["images"][0]
            checked = inspect_saved(root, client=PhotosClient(png()), log=lambda _: None)
            self.assertEqual((checked.images_different, checked.images_repaired), (1, 0))
            self.assertEqual((folder / old["file"]).read_bytes(), GIF)
            self.assertEqual(read_json(folder / "post.json")["images"][0]["verification"]["original"], "smaller")
            client = PhotosClient(png())
            repaired = inspect_saved(root, repair=True, client=client, log=lambda _: None)
            self.assertEqual((repaired.images_repaired, repaired.images_original), (1, 1))
            self.assertEqual(len(client.calls), 1, "reuse the original fetched during comparison")
            new = read_json(folder / "post.json")["images"][0]
            self.assertEqual((new["width"], new["height"], new["quality"]), (16, 12, "full"))
            self.assertEqual((folder / new["verification"]["backup_file"]).read_bytes(), GIF)
            self.assertFalse((folder / old["file"]).exists())

    def test_original_fetch_failure_is_unknown_not_false_match(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                client=FakeClient(page_html([post(identities=("photo-a",))])), log=lambda _: None)
            checked = inspect_saved(root, client=PhotosClient(fail_photos=True), log=lambda _: None)
            self.assertEqual((checked.images_damaged, checked.images_original_unknown), (0, 1))
            item = read_json(Path(first.folders[0]) / "post.json")["images"][0]
            self.assertEqual(item["verification"]["original"], "unknown")

    def test_pause_wait_is_interruptible_by_stop(self):
        control = RunControl(interval=1)
        control.paused.set()
        result = []

        def worker():
            try:
                control.check()
            except Cancelled:
                result.append("cancelled")

        thread = threading.Thread(target=worker)
        thread.start()
        control.stop.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["cancelled"])

    def test_request_spacing_waits_only_remaining_interval(self):
        control = RunControl(interval=2.5)
        control.last_request = 10
        with patch("run_control.time.monotonic", return_value=11), patch.object(control, "wait") as wait:
            control.before_request()
        wait.assert_called_once_with(1.5)

    def test_repeat_skip_keeps_original_quality_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for _ in range(2):
                report = download_links([BASE + "/post/UgABCDEFGHIJK"], root,
                    client=FakeClient(page_html([post(identities=("photo-a",))])), log=lambda _: None)
            item = read_json(Path(report.folders[0]) / "post.json")["images"][0]
            self.assertEqual(item["quality"], "full")
            self.assertTrue(item["sha256"])
            self.assertTrue(item["download_url"].endswith("=s0"))

    def test_inspection_includes_legacy_youtube_folder_and_excludes_x(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], base,
                client=FakeClient(page_html([post(identities=("photo-a",))])), log=lambda _: None)
            path = Path(first.folders[0]) / "post.json"
            old = read_json(path)
            old.pop("platform")
            path.write_text(json.dumps(old), encoding="utf-8")
            xfolder = base / "X" / "Example__12345"
            xfolder.mkdir(parents=True)
            (xfolder / "post.json").write_text(json.dumps({"platform": "x", "images": [{"status": "failed"}]}), encoding="utf-8")
            root = base / "YouTube"
            checked = inspect_saved(root, check_original=False, scan_root=base,
                                    client=PhotosClient(), log=lambda _: None)
            self.assertEqual((checked.posts, checked.images_checked), (1, 1))
            self.assertTrue(path.is_file(), "legacy files must stay in their original location")
            self.assertEqual(Path(checked.manifest).parent, root)


class XTests(unittest.TestCase):
    def test_normalize_single_post_and_media_forms(self):
        for value in ("x.com/Example/status/12345/photo/2?s=20", "https://mobile.twitter.com/Example/status/12345"):
            target = x_downloader.normalize_url(value)
            self.assertEqual((target.url, target.post_id), ("https://x.com/Example/status/12345", "12345"))
        self.assertEqual(x_downloader.normalize_url("https://x.com/i/web/status/12345").post_id, "12345")
        for value in ("twitter.com/Example", "https://x.com/Example/media"):
            self.assertEqual(x_downloader.normalize_url(value).url, "https://x.com/Example/media")

    def test_wrong_hosts_and_unsupported_routes_rejected(self):
        for value in ("https://x.com.evil.invalid/Example", "https://x.com@evil.invalid/Example", "https://x.com/home",
                      "https://x.com/search?q=x", "https://x.com/Example/likes", "https://x.com/../outside", "https://x.com:8080/Example"):
            with self.subTest(value=value), self.assertRaises(DownloadError):
                x_downloader.normalize_url(value)

    def test_media_requires_explicit_login_cookie_before_any_request(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(DownloadError, "로그인 쿠키"):
                x_downloader.download_links(["x.com/Example/media"], Path(temp) / "X")
            self.assertFalse(list(Path(temp).iterdir()))

    @staticmethod
    def messages(post_id="12345", count=2):
        data = {"tweet_id": int(post_id), "author": {"name": "Example"}, "content": "사진 두 장", "date": "2026-10-02", "count": count}
        yield Message.Directory, "", data
        for number in range(count):
            yield Message.Url, f"https://pbs.twimg.com/media/photo{number}?format=jpg&name=orig", {**data, "type": "photo"}

    def test_album_and_limit_do_not_request_next_post(self):
        def messages():
            yield from self.messages()
            raise AssertionError("must stop at configured limit before another API request")

        posts = list(x_downloader.messages_to_posts(messages(), x_downloader.normalize_url("x.com/Example/media"), 1, RunControl()))
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(posts[0].images), 2)
        self.assertEqual(posts[0].url, "https://x.com/Example/status/12345")
        self.assertIn("name=orig", posts[0].images[0][0])

    def test_single_target_excludes_unrelated_and_video_previews(self):
        messages = list(self.messages("99999")) + list(self.messages())
        messages[-1] = (Message.Url, "https://pbs.twimg.com/media/video?format=jpg&name=orig", {"type": "preview"})
        result = list(x_downloader.messages_to_posts(messages, x_downloader.normalize_url("x.com/Example/status/12345"), 0, RunControl()))
        self.assertEqual([p.post_id for p in result], ["12345"])
        self.assertEqual(len(result[0].images), 1)
        with self.assertRaises(DownloadError):
            x_downloader.image_candidates("https://pbs.twimg.com/profile_images/avatar.jpg")

    def test_cookies_are_explicit_filtered_and_not_written_to_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cookie_path = root / "cookies.txt"
            secret = "TEST_ONLY_SESSION_VALUE"
            cookie_path.write_text("# Netscape HTTP Cookie File\n.twitter.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\t" + secret +
                                   "\n.example.org\tTRUE\t/\tTRUE\t2147483647\tother\tprivate\n", encoding="utf-8")
            jar = x_downloader.load_cookies(cookie_path)
            self.assertEqual([(c.domain, c.name) for c in jar], [(".x.com", "auth_token")])

            class ImageClient(x_downloader.XImageClient):
                def request(self, *args, **kwargs):
                    return GIF

            logs = []
            report = x_downloader.download_links(["x.com/Example/status/12345"], root / "X", cookies_path=cookie_path,
                log=logs.append, client=ImageClient(), extractor_factory=lambda *args: iter(self.messages()))
            self.assertEqual(report.images_saved, 2)
            self.assertNotIn(secret, "\n".join(logs) + Path(report.manifest).read_text("utf-8"))
            self.assertEqual(read_json(Path(report.folders[0]) / "post.json")["platform"], "x")

    def test_extractor_uses_only_selected_cookies_and_no_netrc(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tTEST_ONLY\n", encoding="utf-8")
            worker = x_downloader.create_extractor(x_downloader.normalize_url("x.com/Example/status/12345"), RunControl(), x_downloader.load_cookies(path))
            try:
                self.assertEqual(worker.api.headers["x-twitter-auth-type"], "OAuth2Session")
                self.assertFalse(worker.config("netrc"))
                self.assertFalse(worker.session.trust_env)
                self.assertFalse(worker.config("videos"))
            finally:
                worker.session.close()

    def test_platform_roots_and_wrong_platform_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            yt = pipelines.platform_folder("youtube", base)
            x = pipelines.platform_folder("x", base)
            self.assertEqual((yt.name, x.name), ("YouTube", "X"))
            self.assertNotEqual(yt, x)
            with self.assertRaises(DownloadError):
                pipelines.normalize_urls("youtube", ["x.com/Example/status/12345"])
            with self.assertRaises(DownloadError):
                pipelines.normalize_urls("x", [BASE + "/post/UgABCDEFGHIJK"])
            with patch("pipelines.downloader.download_links") as youtube, patch("pipelines.x_downloader.download_links") as twitter:
                pipelines.run("youtube", "download", ["youtube.com/@Example"], base)
                self.assertEqual(youtube.call_args.args[1], yt)
                twitter.assert_not_called()
                pipelines.run("x", "download", ["x.com/Example"], base)
                self.assertEqual(twitter.call_args.args[1], x)
            self.assertFalse(list(base.iterdir()))


if __name__ == "__main__":
    unittest.main()
