"""Render a supplied local component without external network access."""

import json
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket

from playwright.sync_api import Page, Route, sync_playwright

from playgrounds_sandbox.artifacts import write_manifest

INPUT_DIRECTORY = Path("/work/input")
OUTPUT_DIRECTORY = Path("/work/output")


def available_loopback_port() -> int:
    """Reserve an ephemeral port number on the container's loopback interface."""

    with closing(socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _block_external_requests(route: Route, local_origin: str, blocked: list[str]) -> None:
    """Allow the local preview only and record attempted external requests."""

    if route.request.url.startswith(local_origin):
        route.continue_()
        return
    blocked.append(route.request.url.split("?", 1)[0])
    route.abort()


def _inspect_page(page: Page) -> dict[str, object]:
    """Collect small deterministic render and baseline-accessibility facts."""

    return page.evaluate(
        """() => {
            const root = document.querySelector("[data-pg-component]");
            const interactiveSelector = [
              "button", "a[href]", "input", "select", "textarea",
              "[role=button]", "[role=menuitem]", "[role=checkbox]",
              "[role=radio]", "[role=switch]", "[tabindex]"
            ].join(",");
            const interactive = root ? [
              ...(root.matches(interactiveSelector) ? [root] : []),
              ...root.querySelectorAll(interactiveSelector)
            ] : [];
            const accessibleName = (element) => {
              const labelledBy = element.getAttribute("aria-labelledby");
              const labelledText = labelledBy
                ? labelledBy.split(/\\s+/).map((id) => document.getElementById(id)?.textContent || "")
                    .join(" ")
                : "";
              return (
                element.getAttribute("aria-label") ||
                labelledText ||
                element.getAttribute("alt") ||
                element.textContent ||
                ""
              ).trim();
            };
            return {
              root_found: Boolean(root),
              root_bounds: root ? {
                width: root.getBoundingClientRect().width,
                height: root.getBoundingClientRect().height
              } : {width: 0, height: 0},
              interactive_count: interactive.length,
              unnamed_interactive_count: interactive.filter(
                (element) => !accessibleName(element)
              ).length,
              document_size: {
                width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight
              }
            };
        }"""
    )


def main() -> None:
    """Render a component package and write its screenshot plus diagnostics."""

    required = ("component.html", "component.css", "component.js")
    missing = [name for name in required if not (INPUT_DIRECTORY / name).is_file()]
    if missing:
        raise FileNotFoundError(f"creator jobs are missing required inputs: {', '.join(missing)}")
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    port = available_loopback_port()
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(
        *args, directory=INPUT_DIRECTORY, **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    errors: list[str] = []
    blocked_requests: list[str] = []
    inspection: dict[str, object] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: errors.append(message.text) if message.type == "error" else None,
            )
            local_origin = f"http://127.0.0.1:{port}/"
            page.route(
                "**/*",
                lambda route: _block_external_requests(route, local_origin, blocked_requests),
            )
            page.goto(f"{local_origin}component.html", wait_until="networkidle")
            inspection = _inspect_page(page)
            page.screenshot(path=OUTPUT_DIRECTORY / "screenshot.png", full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    (OUTPUT_DIRECTORY / "render.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "errors": errors,
                "blocked_requests": blocked_requests,
                "inspection": inspection,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_manifest(OUTPUT_DIRECTORY)


if __name__ == "__main__":
    main()
