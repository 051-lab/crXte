from __future__ import annotations

import argparse
import socket
import subprocess
import threading
import webbrowser

import uvicorn

from .instance import InstanceLock


def available_port(start: int = 8765, stop: int = 8775) -> int:
    for port in range(start, stop + 1):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found between {start} and {stop}.")


def open_browser(url: str) -> None:
    def launch() -> None:
        try:
            subprocess.Popen(
                ["cmd.exe", "/d", "/c", "start", "", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            webbrowser.open(url)

    threading.Timer(0.9, launch).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="crXte: export public X posts locally.")
    parser.add_argument("--port", type=int, help="Local port (default: first free 8765-8775)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    args = parser.parse_args()
    instance = InstanceLock()
    if not instance.acquire():
        url = instance.current_url()
        print(f"crXte is already running{f' → {url}' if url else '.'}")
        if url and not args.no_browser:
            open_browser(url)
        return
    try:
        port = args.port or available_port()
        url = f"http://127.0.0.1:{port}"
        instance.publish(url)
        if not args.no_browser:
            open_browser(url)
        print(f"crXte → {url}")
        print("Press Ctrl+C to stop the local server. Partial downloads are kept.")
        uvicorn.run("x_media_downloader.app:app", host="127.0.0.1", port=port, log_level="info")
    finally:
        instance.close()


if __name__ == "__main__":
    main()
