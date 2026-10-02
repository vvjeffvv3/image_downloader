"""Use the dependencies shipped beside the app without modifying system Python."""
import sys
from pathlib import Path


def enable_bundled_dependencies():
    folder = str(Path(__file__).resolve().parent / ".deps")
    if folder not in sys.path and Path(folder).is_dir():
        sys.path.append(folder)
