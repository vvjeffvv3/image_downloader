import json
import tempfile
import threading
import unittest
from pathlib import Path

from downloader import (
    BASE, Cancelled, DownloadError, YouTubeClient, allowed_image_url,
    continuation_token, download_links, extract_posts, image_extension,
    normalize_url, parse_page, safe_name,
)

# A complete 1x1 GIF, so storage tests exercise real image data.
GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


def photo(identity):
    return {"backstageImageRenderer": {"image": {"thumbnails": [
        {"url": f"https://yt3.ggpht.com/{identity}=s288-c", "width": 288, "height": 288},
        {"url": f"https://yt3.ggpht.com/{identity}=s1080-c", "width": 1080, "height": 1080},
    ]}}}


def post(post_id="UgABCDEFGHIJK", identities=("photo-a", "photo-b")):
    attachment = {"postMultiImageRenderer": {"images": [photo(identity) for identity in identities]}}
    return {"backstagePostThreadRenderer": {"post": {"backstagePostRenderer": {
        "postId": post_id,
        "authorText": {"runs": [{"text": "테스트 채널"}]},
        "contentText": {"runs": [{"text": "첫째 줄\n"}, {"text": "둘째 줄 🌸"}]},
        "publishedTimeText": {"simpleText": "2 days ago"},
        "authorThumbnail": {"thumbnails": [{"url": "https://yt3.ggpht.com/avatar=s88"}]},
        "backstageAttachment": attachment,
    }}}}


def next_item(token):
    return {"continuationItemRenderer": {"continuationEndpoint": {"continuationCommand": {"token": token}}}}


def page_html(items, prefix="var ytInitialData = "):
    data = {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [
        {"tabRenderer": {"selected": False, "content": post("UgUNSELECTED1")}},
        {"tabRenderer": {"selected": True, "content": {"sectionListRenderer": {"contents": items}}}},
    ]}}}
    return ("<script>ytcfg.set('unrelated');ytcfg.set(" + json.dumps({
        "INNERTUBE_CLIENT_VERSION": "2.20261001.01.00", "INNERTUBE_CONTEXT": {"client": {"clientName": "WEB"}}
    }) + ");</script><script>" + prefix + json.dumps(data) + ";</script>").encode()


class FakeClient(YouTubeClient):
    def __init__(self, page, pages=None, fail_photos=False, cancel_photos=False, original_fails=False):
        super().__init__()
        self.page = page
        self.pages = pages or {}
        self.calls = []
        self.fail_photos = fail_photos
        self.cancel_photos = cancel_photos
        self.original_fails = original_fails

    def pause(self, seconds):
        self.check_cancelled()

    def request(self, url, payload=None, max_bytes=None):
        self.check_cancelled()
        self.calls.append((url, payload))
        if payload is not None:
            return json.dumps(self.pages[payload["continuation"]]).encode()
        if "ggpht.com" in url:
            if self.cancel_photos:
                raise Cancelled("stopped")
            if self.fail_photos or (self.original_fails and url.endswith("=s0")):
                raise DownloadError("photo failed")
            return GIF
        return self.page


class DownloaderTests(unittest.TestCase):
    def test_accepted_url_forms(self):
        post_id = "UgABCDEFGHIJK"
        for source in (f"https://www.youtube.com/post/{post_id}?si=tracking",
                       f"https://m.youtube.com/channel/UCabc/community?lb={post_id}"):
            self.assertEqual(normalize_url(source).post_id, post_id)
        for source in ("youtube.com/@Example", "https://youtube.com/@Example/community"):
            self.assertEqual(normalize_url(source).url, BASE + "/@Example/posts")
        self.assertIn("%ED%95%9C%EA%B8%80", normalize_url("https://youtube.com/@한글/posts").url)

    def test_wrong_hosts_video_links_and_path_injection_rejected(self):
        for source in ("https://youtube.com.attacker.invalid/@a", "https://youtube.com@attacker.invalid/@a",
                       "file:///tmp/example", "https://youtu.be/abc", "https://youtube.com/watch?v=abc",
                       "https://youtube.com/post/../outside", "https://youtube.com:8080/@a"):
            with self.subTest(source=source), self.assertRaises(DownloadError):
                normalize_url(source)

    def test_script_json_with_braces_and_window_assignment(self):
        raw = 'window["ytInitialData"] = ' + json.dumps({"text": "한글 } ; { \"quoted\""}) + ";"
        data, config = parse_page(raw + 'ytcfg.set("other");ytcfg.set({"INNERTUBE_CLIENT_VERSION":"v1"});')
        self.assertIn("한글", data["text"])
        self.assertEqual(config["INNERTUBE_CLIENT_VERSION"], "v1")
        with self.assertRaises(DownloadError):
            parse_page("<html>sign in</html>")

    def test_album_preserves_order_and_ignores_avatar(self):
        item = extract_posts([post()])[0]
        self.assertEqual(item.text, "첫째 줄\n둘째 줄 🌸")
        self.assertEqual([urls[0] for urls in item.images], [
            "https://yt3.ggpht.com/photo-a=s0", "https://yt3.ggpht.com/photo-b=s0"])
        self.assertTrue(item.images[0][1].endswith("=s1080-c"))

    def test_video_thumbnail_is_not_a_community_photo(self):
        item = post()
        renderer = item["backstagePostThreadRenderer"]["post"]["backstagePostRenderer"]
        renderer["backstageAttachment"] = {"videoRenderer": {"thumbnail": {"thumbnails": [
            {"url": "https://i.ytimg.com/vi/video/hq.jpg", "width": 720, "height": 400}]}}}
        self.assertEqual(extract_posts([item])[0].images, [])

    def test_post_comment_continuation_is_ignored(self):
        item = post()
        item["backstagePostThreadRenderer"]["comments"] = next_item("comments")
        self.assertEqual(continuation_token([item, next_item("feed")]), "feed")

    def test_pagination_limit_and_duplicate_posts(self):
        client = FakeClient(page_html([post(), next_item("next")]), pages={"next": {
            "onResponseReceivedActions": [{"appendContinuationItemsAction": {"continuationItems": [
                post(), post("UgSECONDPOST1"), next_item("unrequested")
            ]}}]
        }})
        actual = list(client.iter_posts(normalize_url("youtube.com/@Example"), 2, lambda x: None))
        self.assertEqual([p.post_id for p in actual], ["UgABCDEFGHIJK", "UgSECONDPOST1"])
        requests = [payload for _, payload in client.calls if payload is not None]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["context"]["client"]["clientVersion"], "2.20261001.01.00")

    def test_repeated_continuation_reports_incomplete_download(self):
        client = FakeClient(page_html([post(), next_item("loop")]), pages={"loop": {
            "onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": [post(), next_item("loop")]}}]
        }})
        with self.assertRaises(DownloadError):
            list(client.iter_posts(normalize_url("youtube.com/@Example"), 0, lambda x: None))

    def test_single_post_never_downloads_unrelated_posts(self):
        client = FakeClient(page_html([post(), post("UgUNRELATED12")]))
        result = list(client.iter_posts(normalize_url(BASE + "/post/UgABCDEFGHIJK"), 100, lambda x: None))
        self.assertEqual([p.post_id for p in result], ["UgABCDEFGHIJK"])

    def test_storage_unicode_images_and_repeat_skip(self):
        client = FakeClient(page_html([post()]))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = download_links([BASE + "/post/UgABCDEFGHIJK"], root, client=client, log=lambda x: None)
            self.assertEqual((first.posts, first.images_saved), (1, 2))
            folder = Path(first.folders[0])
            self.assertIn("둘째 줄 🌸", (folder / "post.txt").read_text("utf-8-sig"))
            self.assertEqual(len(list(folder.glob("*.gif"))), 2)
            second = download_links([BASE + "/post/UgABCDEFGHIJK"], root, client=client, log=lambda x: None)
            self.assertEqual((second.images_saved, second.images_skipped), (0, 2))
            self.assertFalse(list(root.rglob("*.part")))
            self.assertEqual(json.loads(Path(second.manifest).read_text("utf-8"))["posts"], 1)

    def test_original_failure_falls_back_and_records_quality(self):
        client = FakeClient(page_html([post(identities=("photo-a",))]), original_fails=True)
        with tempfile.TemporaryDirectory() as temp:
            report = download_links([BASE + "/post/UgABCDEFGHIJK"], Path(temp), client=client, log=lambda x: None)
            data = json.loads((Path(report.folders[0]) / "post.json").read_text("utf-8"))
            self.assertEqual(data["images"][0]["quality"], "available")
            self.assertTrue(data["images"][0]["download_url"].endswith("=s1080-c"))

    def test_failed_photos_keep_text_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as temp:
            failed = download_links([BASE + "/post/UgABCDEFGHIJK"], Path(temp),
                                    client=FakeClient(page_html([post()]), fail_photos=True), log=lambda x: None)
            self.assertEqual(failed.images_failed, 2)
            folder = Path(failed.folders[0])
            self.assertTrue((folder / "post.txt").is_file())
            self.assertEqual(json.loads((folder / "post.json").read_text("utf-8"))["images"][0]["status"], "failed")
            retried = download_links([BASE + "/post/UgABCDEFGHIJK"], Path(temp),
                                     client=FakeClient(page_html([post()])), log=lambda x: None)
            self.assertEqual(retried.images_saved, 2)

    def test_cancellation_preserves_metadata_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            report = download_links([BASE + "/post/UgABCDEFGHIJK"], Path(temp),
                                    client=FakeClient(page_html([post()]), cancel_photos=True), log=lambda x: None)
            self.assertTrue(report.cancelled)
            data = json.loads((Path(report.folders[0]) / "post.json").read_text("utf-8"))
            self.assertEqual([image["status"] for image in data["images"]], ["cancelled", "pending"])
            self.assertTrue(json.loads(Path(report.manifest).read_text("utf-8"))["cancelled"])

    def test_image_host_validation_and_binary_type(self):
        self.assertEqual(allowed_image_url("//yt3.ggpht.com/image=s0"), "https://yt3.ggpht.com/image=s0")
        for url in ("http://yt3.ggpht.com/a", "https://ggpht.com.attacker.invalid/a", "https://127.0.0.1/a"):
            with self.assertRaises(DownloadError):
                allowed_image_url(url)
        self.assertEqual(image_extension(GIF), ".gif")
        self.assertEqual(image_extension(b"RIFF0000WEBP"), ".webp")
        with self.assertRaises(DownloadError):
            image_extension(b"<html>blocked</html>")

    def test_windows_filenames(self):
        self.assertEqual(safe_name("CON"), "channel_CON")
        self.assertEqual(safe_name('한글:/채널?'), "한글__채널_")
        self.assertLessEqual(len(safe_name("a" * 100)), 32)


if __name__ == "__main__":
    unittest.main()
