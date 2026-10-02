"""Korean desktop launcher and command-line interface for separate photo pipelines."""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pipelines
from downloader import DownloadError, load_checkpoint
from run_control import RunControl
from settings import SettingsStore
from x_downloader import X_HOSTS, load_cookies

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "downloads"
OPERATION_LABELS = {"download": "사진을 받고 있습니다…",
                    "resume": "중지한 위치에서 이어받고 있습니다…",
                    "retry": "실패한 사진을 다시 받고 있습니다…",
                    "inspect": "저장 사진을 검사하고 있습니다…"}


class DownloaderApp:
    def __init__(self, root, initial_platform=None, settings_path=None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.control = None
        self.closing = False
        self.platform = None
        self.operation = "download"
        self.drafts = {name: {"urls": "", "limit": "100"} for name in pipelines.PLATFORMS}
        self.active_output = DEFAULT_OUTPUT
        self.settings = SettingsStore(settings_path)
        self.settings_notice = ""
        try:
            remembered_cookies = self.settings.cookie_path()
        except (DownloadError, OSError):
            remembered_cookies = ""
            self.settings_notice = "쿠키 파일 선택 정보를 불러오지 못했습니다. 사용할 파일을 다시 선택해 주세요."
        root.title("유튜브 · X 사진 다운로더")
        root.option_add("*Font", ("맑은 고딕", 10))
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("맑은 고딕", 20, "bold"))
        style.configure("Hint.TLabel", foreground="#536176")
        style.configure("Action.TButton", padding=(14, 8))
        style.configure("Choice.TButton", font=("맑은 고딕", 15, "bold"), padding=(24, 24))

        self.choice_frame = ttk.Frame(root, padding=36)
        ttk.Label(self.choice_frame, text="어디에서 사진을 받을까요?", style="Title.TLabel").pack(anchor="w", pady=(8, 10))
        ttk.Label(self.choice_frame, text="플랫폼을 선택하면 해당 링크를 입력하는 화면이 열립니다.", style="Hint.TLabel").pack(anchor="w")
        choices = ttk.Frame(self.choice_frame)
        choices.pack(fill="x", pady=(32, 20))
        for name, title, hint in (("youtube", "유튜브", "게시글 한 개 · 채널 커뮤니티"),
                                  ("x", "X", "게시글 한 개 · 계정 미디어 탭")):
            card = ttk.Frame(choices)
            card.pack(side="left", fill="both", expand=True, padx=6)
            ttk.Button(card, text=title, style="Choice.TButton", command=lambda value=name: self.select_platform(value)).pack(fill="x")
            ttk.Label(card, text=hint, style="Hint.TLabel", anchor="center").pack(fill="x", pady=10)
        ttk.Label(self.choice_frame, text="사진과 다운로드 기록은 YouTube / X 폴더에 각각 저장합니다.", style="Hint.TLabel").pack(anchor="w", pady=12)

        self.form = frame = ttk.Frame(root, padding=22)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(10, weight=1)
        top = ttk.Frame(frame)
        top.grid(row=0, column=0, sticky="ew")
        self.title_var = tk.StringVar()
        ttk.Label(top, textvariable=self.title_var, style="Title.TLabel").pack(side="left")
        self.change_button = ttk.Button(top, text="플랫폼 바꾸기", command=self.show_choice)
        self.change_button.pack(side="right")
        ttk.Label(frame, text="첨부 사진을 게시글별로 저장합니다. 사진이 없는 글은 건너뜁니다.", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(5, 14))
        self.links_label = tk.StringVar()
        ttk.Label(frame, textvariable=self.links_label).grid(row=2, column=0, sticky="w")
        self.urls = ScrolledText(frame, height=4, wrap="word", relief="solid", borderwidth=1)
        self.urls.grid(row=3, column=0, sticky="ew", pady=(6, 4))
        self.hint_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.hint_var, style="Hint.TLabel").grid(row=4, column=0, sticky="w", pady=(0, 12))

        options = ttk.Frame(frame)
        options.grid(row=5, column=0, sticky="ew")
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="기본 저장 폴더").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.output_entry = ttk.Entry(options, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(options, text="변경…", command=self.choose_folder)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        self.actual_output_var = tk.StringVar()
        ttk.Label(options, textvariable=self.actual_output_var, style="Hint.TLabel", wraplength=680).grid(row=1, column=1, columnspan=2, sticky="w", pady=(3, 8))
        self.count_label = tk.StringVar()
        ttk.Label(options, textvariable=self.count_label).grid(row=2, column=0, sticky="w", padx=(0, 12))
        count = ttk.Frame(options)
        count.grid(row=2, column=1, columnspan=2, sticky="w")
        self.limit_var = tk.StringVar(value="100")
        self.limit_entry = ttk.Spinbox(count, from_=0, to=100000, textvariable=self.limit_var, width=8)
        self.limit_entry.pack(side="left")
        ttk.Label(count, text="  0 = 조회 가능한 끝까지 · 게시글 한 개는 수 제한 없음", style="Hint.TLabel").pack(side="left")
        ttk.Label(options, text="요청 간격(초)").grid(row=3, column=0, sticky="w", pady=(8, 0))
        spacing = ttk.Frame(options)
        spacing.grid(row=3, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.interval_var = tk.StringVar(value="1.0")
        self.interval_entry = ttk.Spinbox(spacing, from_=0, to=300, increment=0.5, textvariable=self.interval_var, width=8)
        self.interval_entry.pack(side="left")
        ttk.Label(spacing, text="  요청이 끝난 뒤 다음 요청까지 기다리는 시간", style="Hint.TLabel").pack(side="left")

        self.cookie_frame = ttk.Frame(options)
        self.cookie_frame.columnconfigure(1, weight=1)
        ttk.Label(self.cookie_frame, text="X 로그인 쿠키").grid(row=0, column=0, padx=(0, 12))
        self.cookies_var = tk.StringVar(value=remembered_cookies)
        self.cookies_entry = ttk.Entry(self.cookie_frame, textvariable=self.cookies_var)
        self.cookies_entry.grid(row=0, column=1, sticky="ew")
        self.cookie_button = ttk.Button(self.cookie_frame, text="파일 선택…", command=self.choose_cookies)
        self.cookie_button.grid(row=0, column=2, padx=8)
        self.clear_cookie_button = ttk.Button(self.cookie_frame, text="해제", command=self.clear_cookies)
        self.clear_cookie_button.grid(row=0, column=3)
        ttk.Label(self.cookie_frame, text="게시글 한 개는 필요할 때 선택 · 계정 미디어 탭은 로그인 cookies.txt가 필요합니다.", style="Hint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=4)
        ttk.Label(self.cookie_frame, text="선택한 파일은 다음 실행에도 유지됩니다. 새 파일로 바꾸거나 ‘해제’를 누를 때까지 기억합니다.", style="Hint.TLabel").grid(row=2, column=0, columnspan=4, sticky="w")

        self.youtube_frame = ttk.Frame(frame)
        self.youtube_frame.grid(row=6, column=0, sticky="ew", pady=(15, 0))
        yt_actions = ttk.Frame(self.youtube_frame)
        yt_actions.pack(anchor="w")
        self.resume_button = ttk.Button(yt_actions, text="이어서 받기", command=lambda: self.start("resume"))
        self.resume_button.pack(side="left")
        self.retry_button = ttk.Button(yt_actions, text="실패한 사진 재시도", command=lambda: self.start("retry"))
        self.retry_button.pack(side="left", padx=8)
        self.inspect_button = ttk.Button(yt_actions, text="저장 사진 검사", command=lambda: self.start("inspect"))
        self.inspect_button.pack(side="left")
        checks = ttk.Frame(self.youtube_frame)
        checks.pack(anchor="w", pady=(8, 0))
        self.original_var = tk.BooleanVar(value=True)
        self.repair_var = tk.BooleanVar(value=False)
        self.original_check = ttk.Checkbutton(checks, text="검사 시 서버 최대 크기와 대조", variable=self.original_var)
        self.original_check.pack(side="left")
        self.repair_check = ttk.Checkbutton(checks, text="문제 사진 자동복구 (기존 파일 백업)", variable=self.repair_var)
        self.repair_check.pack(side="left", padx=12)
        ttk.Label(self.youtube_frame, text="이어받기는 마지막 작업의 주소·수 제한을 사용합니다. 재시도와 검사는 링크 입력 없이 사용할 수 있습니다.", style="Hint.TLabel", wraplength=790).pack(anchor="w", pady=(5, 0))

        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, sticky="ew", pady=(17, 10))
        self.start_button = ttk.Button(actions, text="사진 받기", style="Action.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(actions, text="잠시 멈춤", command=self.toggle_pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="중지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="저장 폴더 열기", command=self.open_folder).pack(side="right")
        self.status_var = tk.StringVar(value="링크를 입력한 후 ‘사진 받기’를 누르세요.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=790).grid(row=8, column=0, sticky="w")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.grid(row=9, column=0, sticky="ew", pady=(7, 10))
        self.log_box = ScrolledText(frame, height=6, state="disabled", wrap="word", relief="solid", borderwidth=1)
        self.log_box.grid(row=10, column=0, sticky="nsew")
        self.footer_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.footer_var, style="Hint.TLabel", wraplength=790).grid(row=11, column=0, sticky="w", pady=(10, 0))
        self.output_var.trace_add("write", lambda *_: self.update_output_hint())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(100, self.poll)
        if initial_platform:
            self.select_platform(initial_platform)
        else:
            self.show_choice()

    def show_choice(self):
        if self.worker and self.worker.is_alive():
            return
        if self.platform:
            self.drafts[self.platform] = {"urls": self.urls.get("1.0", "end-1c"), "limit": self.limit_var.get()}
        self.form.pack_forget()
        self.choice_frame.pack(fill="both", expand=True)
        self.root.minsize(660, 330)
        self.root.geometry("740x360")

    def select_platform(self, platform):
        self.platform = platform
        title = "유튜브" if platform == "youtube" else "X"
        self.title_var.set(f"{title} 사진 다운로더")
        self.links_label.set(f"{title} 링크 · 여러 개는 줄을 나누어 입력")
        self.hint_var.set("게시글: youtube.com/post/…   |   채널: youtube.com/@채널명/posts" if platform == "youtube"
                          else "게시글: x.com/계정/status/번호   |   계정: x.com/계정/media")
        self.count_label.set("채널당 조회 글 수" if platform == "youtube" else "계정당 사진 글 수")
        self.urls.delete("1.0", "end")
        self.urls.insert("1.0", self.drafts[platform]["urls"])
        self.limit_var.set(self.drafts[platform]["limit"])
        self.status_var.set("링크를 입력한 후 ‘사진 받기’를 누르세요.")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        if platform == "youtube":
            self.youtube_frame.grid()
            self.cookie_frame.grid_remove()
            self.footer_var.set("공개 게시글의 첨부 사진을 저장합니다. 최대 크기 대조는 서버가 제공하는 파일과 비교하며, 업로드 전 원본 파일을 보증하지는 않습니다.")
        else:
            self.youtube_frame.grid_remove()
            self.cookie_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
            self.footer_var.set("첨부 사진만 저장합니다. 동영상·동영상 미리보기·프로필 사진은 제외합니다. 선택한 로그인 쿠키는 다운로드 결과에 복사하지 않습니다.")
            if self.settings_notice:
                self.append_log(self.settings_notice)
        self.choice_frame.pack_forget()
        self.form.pack(fill="both", expand=True)
        self.root.minsize(780, 740)
        self.root.geometry("880x840")
        self.update_output_hint()
        self.urls.focus_set()

    def update_output_hint(self):
        if self.platform:
            try:
                self.actual_output_var.set("실제 저장: " + str(pipelines.platform_folder(self.platform, self.output_var.get())))
            except (ValueError, OSError):
                self.actual_output_var.set("저장 폴더를 확인해 주세요.")

    def choose_folder(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="기본 저장 폴더 선택", initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def choose_cookies(self):
        from tkinter import filedialog, messagebox
        filename = filedialog.askopenfilename(title="X 로그인 cookies.txt 선택", filetypes=[("쿠키 파일", "*.txt"), ("모든 파일", "*.*")])
        if filename:
            try:
                self.use_cookie_file(filename)
            except (DownloadError, OSError, ValueError) as error:
                messagebox.showerror("쿠키 파일 확인", str(error), parent=self.root)

    def use_cookie_file(self, filename):
        path = Path(filename).expanduser().resolve()
        load_cookies(path)
        value = self.settings.remember_cookie_path(path)
        self.cookies_var.set(value)
        self.settings_notice = ""
        self.status_var.set("쿠키 파일 선택을 기억했습니다. 다음 실행에도 같은 파일을 사용합니다.")

    def clear_cookies(self):
        from tkinter import messagebox
        try:
            self.settings.remember_cookie_path("")
        except (DownloadError, OSError, ValueError) as error:
            messagebox.showerror("선택 정보 저장 오류", str(error), parent=self.root)
            return
        self.cookies_var.set("")
        self.settings_notice = ""
        self.status_var.set("기억한 쿠키 파일 선택을 해제했습니다.")

    def append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in (self.start_button, self.output_entry, self.browse_button, self.limit_entry,
                       self.interval_entry, self.urls, self.change_button, self.cookies_entry,
                       self.cookie_button, self.clear_cookie_button, self.resume_button,
                       self.retry_button, self.inspect_button, self.original_check, self.repair_check):
            widget.configure(state=state)
        self.pause_button.configure(state="normal" if busy else "disabled", text="잠시 멈춤")
        self.stop_button.configure(state="normal" if busy else "disabled")
        self.progress.start(12) if busy else self.progress.stop()

    def start(self, operation="download"):
        from tkinter import messagebox
        if self.worker and self.worker.is_alive():
            return
        urls = [line.strip() for line in self.urls.get("1.0", "end").splitlines() if line.strip()]
        try:
            if not self.output_var.get().strip():
                raise DownloadError("기본 저장 폴더를 선택해 주세요.")
            output = Path(self.output_var.get().strip()).expanduser().resolve()
            root = pipelines.platform_folder(self.platform, output)
            limit = int(self.limit_var.get()) if operation == "download" else 100
            if limit < 0:
                raise DownloadError("게시글 수는 0 이상이어야 합니다.")
            self.control = RunControl(self.stop_event, float(self.interval_var.get()))
            cookies = self.cookies_var.get().strip() if self.platform == "x" else None
            if operation == "download":
                targets = pipelines.normalize_urls(self.platform, urls)
                if self.platform == "x" and not cookies and any(not target.post_id for target in targets):
                    raise DownloadError("X 계정 미디어 탭 조회에는 로그인 쿠키가 필요합니다. cookies.txt 파일을 선택해 주세요.")
                if cookies:
                    load_cookies(cookies)
                    cookies = self.settings.remember_cookie_path(cookies)
                    self.cookies_var.set(cookies)
            elif operation == "resume":
                checkpoint = load_checkpoint(root)
                urls, limit = checkpoint["source_urls"], checkpoint["limit"]
                self.urls.delete("1.0", "end")
                self.urls.insert("1.0", "\n".join(urls))
                self.limit_var.set(str(limit))
            elif not output.is_dir():
                raise DownloadError("저장된 사진이 있는 기본 폴더를 선택해 주세요.")
            self.active_output = root
        except ValueError as error:
            messagebox.showerror("입력 확인", str(error) if "간격" in str(error) else "게시글 수와 요청 간격을 숫자로 입력해 주세요.", parent=self.root)
            return
        except (DownloadError, OSError) as error:
            messagebox.showerror("입력 확인", str(error), parent=self.root)
            return
        self.operation = operation
        self.stop_event.clear()
        self.set_busy(True)
        self.status_var.set(OPERATION_LABELS[operation])
        self.append_log("─" * 45)
        self.append_log(f"{pipelines.PLATFORMS[self.platform]} 저장 폴더: {root}")
        options = dict(platform=self.platform, operation=operation, urls=urls, output=output,
                       limit=limit, cookies_path=cookies, interval=self.control.interval,
                       control=self.control, check_original=self.original_var.get(), repair=self.repair_var.get())
        self.worker = threading.Thread(target=self.run_worker, args=(options,), daemon=True)
        self.worker.start()

    def run_worker(self, options):
        try:
            report = pipelines.run(**options, log=lambda line: self.events.put(("log", line)))
            self.events.put(("done", report))
        except Exception as error:
            self.events.put(("error", str(error)))

    def toggle_pause(self):
        if not self.control:
            return
        if self.control.paused.is_set():
            self.control.paused.clear()
            self.pause_button.configure(text="잠시 멈춤")
            self.status_var.set(OPERATION_LABELS[self.operation])
            self.progress.start(12)
        else:
            self.control.paused.set()
            self.pause_button.configure(text="계속 진행")
            self.status_var.set("잠시 멈춤 요청됨 · 진행 중인 응답이 끝나면 대기합니다.")
            self.progress.stop()

    def stop(self):
        self.stop_event.set()
        if self.control:
            self.control.paused.clear()
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status_var.set("중지 요청 중… 진행 중인 인터넷 응답을 기다리는 동안 잠시 걸릴 수 있습니다.")

    def poll(self):
        from tkinter import messagebox
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(value)
            elif kind == "done":
                self.set_busy(False)
                self.status_var.set(value.summary())
            elif kind == "error":
                self.set_busy(False)
                self.status_var.set("작업을 완료하지 못했습니다.")
                self.append_log("오류: " + value)
                if not self.closing:
                    messagebox.showerror("작업 오류", value, parent=self.root)
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.root.destroy()
            return
        self.poll_id = self.root.after(100, self.poll)

    def open_folder(self):
        from tkinter import messagebox
        try:
            folder = self.active_output if self.worker and self.worker.is_alive() else pipelines.platform_folder(self.platform, self.output_var.get())
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except (DownloadError, OSError) as error:
            messagebox.showerror("폴더 열기 오류", str(error), parent=self.root)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.closing = True
            self.stop()
            self.root.withdraw()
        else:
            self.root.after_cancel(self.poll_id)
            self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description="유튜브와 X의 게시글 첨부 사진을 플랫폼별로 저장합니다.")
    parser.add_argument("urls", nargs="*", help="게시글 또는 채널·계정 미디어 링크")
    parser.add_argument("--platform", choices=list(pipelines.PLATFORMS))
    parser.add_argument("--action", choices=list(OPERATION_LABELS), default="download")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="기본 저장 폴더 (YouTube/X 하위 폴더 사용)")
    parser.add_argument("--limit", type=int, default=100, help="채널·계정당 게시글 수, 0은 끝까지")
    parser.add_argument("--interval", type=float, default=1.0, help="요청 간격, 초 단위 (기본 1)")
    parser.add_argument("--cookies", type=Path, help="X 로그인용 Netscape cookies.txt")
    parser.add_argument("--local-check", action="store_true", help="사진 검사에서 인터넷 최대 크기 대조 생략")
    parser.add_argument("--repair", action="store_true", help="검사 중 문제 사진 복구, 기존 파일 백업")
    args = parser.parse_args(argv)
    if args.urls or args.action != "download":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        platform = args.platform
        if not platform:
            url = args.urls[0] if args.urls else ""
            host = urlsplit(url if "://" in url else "https://" + url).hostname
            platform = "x" if host in X_HOSTS else "youtube"
        try:
            control = RunControl(interval=args.interval)
            report = pipelines.run(platform, args.action, args.urls, args.output, args.limit,
                                   cookies_path=args.cookies, control=control,
                                   check_original=not args.local_check, repair=args.repair)
            return 130 if report.cancelled else (1 if report.errors or report.images_failed else 0)
        except KeyboardInterrupt:
            print("중지했습니다. 지금까지 저장한 사진은 유지됩니다.", file=sys.stderr)
            return 130
        except (DownloadError, OSError, ValueError) as error:
            print(f"오류: {error}", file=sys.stderr)
            return 1
    try:
        import tkinter as tk
        root = tk.Tk()
        app = DownloaderApp(root, args.platform)
        app.output_var.set(str(args.output.resolve()))
        app.limit_var.set(str(args.limit))
        app.interval_var.set(str(args.interval))
        if args.cookies:
            app.cookies_var.set(str(args.cookies))
        root.mainloop()
    except Exception as error:
        print(f"화면을 열 수 없습니다: {error}\nTkinter를 포함한 Python을 확인해 주세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
