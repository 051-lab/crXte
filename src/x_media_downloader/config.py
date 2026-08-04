from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "x-media-downloader"
PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DATA_DIR = user_data_path(APP_NAME, ensure_exists=True)
DB_PATH = DATA_DIR / "state.db"


def detect_download_dir() -> Path:
    cmd = shutil.which("cmd.exe")
    wslpath = shutil.which("wslpath")
    if cmd and wslpath:
        try:
            result = subprocess.run(
                [cmd, "/d", "/c", "echo", "%USERPROFILE%"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            windows_home = result.stdout.strip().replace("\r", "")
            converted = subprocess.run(
                [wslpath, "-u", windows_home],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            if converted:
                return Path(converted) / "Downloads" / "X Media"
        except (OSError, subprocess.SubprocessError):
            pass
    return Path(os.path.expanduser("~/Downloads/X Media"))


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None
