"""Render a supplied local component without external network access."""

import json
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket

from playwright.sync_api import sync_playwright

from playgrounds_sandbox.artifacts import write_manifest

INPUT_DIRECTORY = Path("/work/input")
OUTPUT_DIRECTORY = Path("/work/output")


def available_loopback_port() -> int:
    """Reserve an ephemeral port number on the container's loopback interface."""

    with closing(socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def main() -> None:
    """Render component.html and write the screenshot plus browser diagnostics."""

    component = INPUT_DIRECTORY / "component.html"
    if not component.is_file():
        message = "creator jobs require /work/input/component.html"
        raise FileNotFoundError(message)
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    port = available_loopback_port()
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(
        *args, directory=INPUT_DIRECTORY, **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: errors.append(message.text) if message.type == "error" else None,
            )
            page.goto(f"http://127.0.0.1:{port}/component.html", wait_until="networkidle")
            page.screenshot(path=OUTPUT_DIRECTORY / "screenshot.png", full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (OUTPUT_DIRECTORY / "render.json").write_text(
        json.dumps({"errors": errors}, sort_keys=True), encoding="utf-8"
    )
    write_manifest(OUTPUT_DIRECTORY)


if __name__ == "__main__":
    main()
