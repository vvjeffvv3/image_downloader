"""Cancellation, temporary pause, and interruptible request spacing."""
import math
import threading
import time


class RunControl:
    def __init__(self, stop=None, interval=1.0):
        if not math.isfinite(interval) or not 0 <= interval <= 300:
            raise ValueError("요청 간격은 0~300초 사이의 숫자로 입력해 주세요.")
        self.stop = stop if stop is not None else threading.Event()
        self.paused = threading.Event()
        self.interval = interval
        self.last_request = 0.0

    def check(self):
        from downloader import Cancelled
        while self.paused.is_set():
            if self.stop.wait(0.1):
                raise Cancelled("사용자가 중지했습니다.")
        if self.stop.is_set():
            raise Cancelled("사용자가 중지했습니다.")

    def wait(self, seconds):
        remaining = max(0.0, seconds)
        while remaining > 0:
            self.check()
            duration = min(remaining, 0.1)
            started = time.monotonic()
            self.stop.wait(duration)
            remaining -= time.monotonic() - started
        self.check()

    def before_request(self):
        self.check()
        if self.last_request:
            self.wait(self.interval - (time.monotonic() - self.last_request))

    def after_request(self):
        self.last_request = time.monotonic()
