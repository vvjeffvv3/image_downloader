import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import DownloaderApp
from downloader import DownloadError
from settings import SettingsStore


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def cookie_file(root, name):
    path = root / name
    path.write_text("# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tTEST_ONLY_" + name + "\n", encoding="utf-8")
    return path


def app_with_settings(path, initial=""):
    app = DownloaderApp.__new__(DownloaderApp)
    app.root = None
    app.settings = SettingsStore(path)
    app.cookies_var = Value(initial)
    app.status_var = Value()
    app.settings_notice = ""
    return app


class CookieSettingsTests(unittest.TestCase):
    def test_selection_survives_restart_and_stores_only_file_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings_path = root / "settings.json"
            self.assertEqual(SettingsStore(settings_path).cookie_path(), "")
            original = cookie_file(root, "first.txt")
            contents = original.read_bytes()
            app = app_with_settings(settings_path)
            app.use_cookie_file(original)
            self.assertEqual(SettingsStore(settings_path).cookie_path(), str(original.resolve()))
            data = json.loads(settings_path.read_text("utf-8"))
            self.assertEqual(data, {"version": 1, "x_cookies_path": str(original.resolve())})
            self.assertNotIn("TEST_ONLY_", settings_path.read_text("utf-8"))
            self.assertEqual(original.read_bytes(), contents)

    def test_replacement_and_clear_are_retained_without_deleting_cookie_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings.json"
            first = cookie_file(root, "first.txt")
            second = cookie_file(root, "second.txt")
            app = app_with_settings(path)
            app.use_cookie_file(first)
            app.use_cookie_file(second)
            self.assertEqual(SettingsStore(path).cookie_path(), str(second.resolve()))
            app.clear_cookies()
            self.assertEqual((app.cookies_var.get(), SettingsStore(path).cookie_path()), ("", ""))
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_invalid_or_cancelled_new_selection_keeps_previous_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings.json"
            first = cookie_file(root, "first.txt")
            invalid = root / "not-cookies.txt"
            invalid.write_text("test username and password, not a cookie file", encoding="utf-8")
            app = app_with_settings(path)
            app.use_cookie_file(first)
            previous = path.read_bytes()
            with patch("tkinter.filedialog.askopenfilename", return_value=str(invalid)), patch("tkinter.messagebox.showerror") as alert:
                app.choose_cookies()
                alert.assert_called_once()
            self.assertEqual(app.cookies_var.get(), str(first.resolve()))
            self.assertEqual(path.read_bytes(), previous)
            with patch("tkinter.filedialog.askopenfilename", return_value=""):
                app.choose_cookies()
            self.assertEqual(path.read_bytes(), previous)

    def test_absent_cookie_does_not_automatically_forget_the_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings.json"
            first = cookie_file(root, "first.txt")
            app = app_with_settings(path)
            app.use_cookie_file(first)
            first.unlink()
            self.assertEqual(SettingsStore(path).cookie_path(), str(first.resolve()))
            with self.assertRaises(DownloadError):
                app.use_cookie_file(first)
            self.assertEqual(SettingsStore(path).cookie_path(), str(first.resolve()))

    def test_valid_selection_can_repair_a_broken_settings_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings.json"
            path.write_text("{invalid", encoding="utf-8")
            with self.assertRaises(DownloadError):
                SettingsStore(path).cookie_path()
            app = app_with_settings(path)
            first = cookie_file(root, "first.txt")
            app.use_cookie_file(first)
            self.assertEqual(SettingsStore(path).cookie_path(), str(first.resolve()))

    def test_storage_failure_does_not_replace_the_previous_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings.json"
            first = cookie_file(root, "first.txt")
            second = cookie_file(root, "second.txt")
            app = app_with_settings(path)
            app.use_cookie_file(first)
            previous = path.read_bytes()
            with patch("settings.write_json", side_effect=PermissionError("cannot save")), self.assertRaises(OSError):
                app.use_cookie_file(second)
            self.assertEqual((path.read_bytes(), app.cookies_var.get()), (previous, str(first.resolve())))


if __name__ == "__main__":
    unittest.main()
