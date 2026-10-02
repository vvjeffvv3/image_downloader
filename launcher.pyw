"""Window-only entry point; keep startup failures visible without a terminal."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path


def show_error(message: str) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "유튜브 · X 사진 다운로더", 0x10)
    else:
        print(message, file=sys.stderr)


def run() -> int:
    original_stdout, original_stderr = sys.stdout, sys.stderr
    log_file = None
    log_path = Path(__file__).resolve().parent / "logs" / "startup.log"
    if sys.stdout is None or sys.stderr is None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("a", encoding="utf-8", buffering=1)
        except OSError as error:
            show_error(f"실행 기록을 저장할 수 없습니다. 프로그램 폴더의 쓰기 권한을 확인해 주세요.\n\n{error}")
            return 1
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file
    try:
        from app import main
        code = main()
        if code:
            show_error(f"프로그램을 실행하지 못했습니다.\n실행 기록: {log_path}")
        return code
    except Exception:
        traceback.print_exc()
        show_error(f"프로그램 실행 중 오류가 발생했습니다.\n실행 기록: {log_path}")
        return 1
    finally:
        if log_file is not None:
            sys.stdout, sys.stderr = original_stdout, original_stderr
            log_file.close()


if __name__ == "__main__":
    raise SystemExit(run())
