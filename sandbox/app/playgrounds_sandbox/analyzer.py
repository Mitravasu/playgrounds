"""Extract deterministic visual evidence from one offline HTML page."""

import json
import os
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Page, Request, Response, Route, sync_playwright

from playgrounds_sandbox.artifacts import write_manifest

INPUT_DIRECTORY = Path("/work/input")
OUTPUT_DIRECTORY = Path("/work/output")
PAGE_FILE = "page.html"
STYLE_ALLOWLIST_VERSION = "1"
STYLE_ALLOWLIST = (
    "backgroundColor",
    "borderBottomColor",
    "borderBottomLeftRadius",
    "borderBottomRightRadius",
    "borderBottomStyle",
    "borderBottomWidth",
    "borderLeftColor",
    "borderLeftStyle",
    "borderLeftWidth",
    "borderRightColor",
    "borderRightStyle",
    "borderRightWidth",
    "borderTopColor",
    "borderTopLeftRadius",
    "borderTopRightRadius",
    "borderTopStyle",
    "borderTopWidth",
    "boxShadow",
    "color",
    "cursor",
    "display",
    "fontFamily",
    "fontSize",
    "fontStyle",
    "fontWeight",
    "lineHeight",
    "marginBottom",
    "marginLeft",
    "marginRight",
    "marginTop",
    "maxWidth",
    "minHeight",
    "minWidth",
    "opacity",
    "outlineColor",
    "outlineStyle",
    "outlineWidth",
    "paddingBottom",
    "paddingLeft",
    "paddingRight",
    "paddingTop",
    "textAlign",
    "textDecorationLine",
)
MAX_NETWORK_LOG_EVENTS = 100


def available_loopback_port() -> int:
    """Reserve an ephemeral port number on the container's loopback interface."""

    with closing(socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _block_non_fixture_requests(route: Route, fixture_url: str) -> None:
    """Allow only the local fixture server, even before network isolation applies."""

    if route.request.url.startswith(fixture_url):
        route.continue_()
    else:
        route.abort()


def _safe_url(value: str) -> str:
    """Log a request destination without query parameters or fragments."""

    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _attach_network_logging(page: Page) -> None:
    """Emit a bounded browser trace for sandbox navigation diagnostics."""

    events = 0

    def log_request(request: Request) -> None:
        nonlocal events
        if events < MAX_NETWORK_LOG_EVENTS:
            print(f"browser request {request.method} {_safe_url(request.url)}", flush=True)
            events += 1

    def log_response(response: Response) -> None:
        nonlocal events
        if events < MAX_NETWORK_LOG_EVENTS:
            print(f"browser response {response.status} {_safe_url(response.url)}", flush=True)
            events += 1

    def log_failure(request: Request) -> None:
        nonlocal events
        if events < MAX_NETWORK_LOG_EVENTS:
            print(
                f"browser request failed {request.failure} {_safe_url(request.url)}",
                flush=True,
            )
            events += 1

    page.on("request", log_request)
    page.on("response", log_response)
    page.on("requestfailed", log_failure)
    page.on("pageerror", lambda error: print(f"browser page error {error}", flush=True))


def _extract_observations(page: Page) -> list[dict[str, Any]]:
    """Return visible semantics and selected computed styles, never raw DOM."""

    return page.evaluate(
        """(styleNames) => {
            const implicitRoles = {
              a: "link", article: "article", aside: "complementary", button: "button",
              footer: "contentinfo", form: "form", h1: "heading", h2: "heading",
              h3: "heading", h4: "heading", h5: "heading", h6: "heading",
              header: "banner", img: "img", input: "textbox", main: "main",
              nav: "navigation", ol: "list", select: "combobox", textarea: "textbox", ul: "list"
            };
            const buttonLikeLink = (tag, styles, bounds) => {
              if (tag !== "a") return null;
              const hasBackground = styles.backgroundColor !== "transparent" &&
                styles.backgroundColor !== "rgba(0, 0, 0, 0)";
              const hasBorder = ["borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth"]
                .some((name) => Number.parseFloat(styles[name]) > 0);
              const hasShadow = styles.boxShadow !== "none";
              const padding = ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft"]
                .reduce((total, name) => total + Number.parseFloat(styles[name]), 0);
              const hasActionTarget = bounds.height >= 32 && padding >= 8;
              return hasBackground || hasBorder || hasShadow || hasActionTarget
                ? "button_like_link"
                : null;
            };
            return [...document.querySelectorAll("*")].flatMap((element, index) => {
              const styles = getComputedStyle(element);
              const bounds = element.getBoundingClientRect();
              if (styles.display === "none" || styles.visibility === "hidden" ||
                  Number(styles.opacity) === 0 || bounds.width === 0 || bounds.height === 0) {
                return [];
              }
              const tag = element.tagName.toLowerCase();
              const text = (element.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 500);
              const computedStyles = Object.fromEntries(
                styleNames.map((name) => [name, styles[name]])
              );
              return [{
                id: `element-${index}`,
                tag,
                role: element.getAttribute("role") || implicitRoles[tag] || null,
                visual_role: buttonLikeLink(tag, computedStyles, bounds),
                text,
                bounds: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height },
                styles: computedStyles
              }];
            });
        }""",
        list(STYLE_ALLOWLIST),
    )


def main() -> None:
    """Render the local fixture and write the analyzer's four declared artifacts."""

    target_url = os.environ.get("PLAYGROUNDS_ANALYZER_URL")
    proxy_url = os.environ.get("PLAYGROUNDS_ANALYZER_PROXY")
    if bool(target_url) != bool(proxy_url):
        raise RuntimeError("public analyzer jobs require both URL and egress proxy settings")
    print(f"analyzer start mode={'public' if target_url else 'offline'}", flush=True)
    fixture = INPUT_DIRECTORY / PAGE_FILE
    if target_url is None and not fixture.is_file():
        raise FileNotFoundError("analyzer jobs require /work/input/page.html")
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    server: ThreadingHTTPServer | None = None
    fixture_url: str | None = None
    if target_url is None:
        port = available_loopback_port()
        handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(
            *args, directory=INPUT_DIRECTORY, **kwargs
        )
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        fixture_url = f"http://127.0.0.1:{port}/{PAGE_FILE}"
    navigation_url = target_url or fixture_url
    if navigation_url is None:
        raise RuntimeError("analyzer navigation URL is missing")
    try:
        with sync_playwright() as playwright:
            print("analyzer launching Chromium", flush=True)
            browser = playwright.chromium.launch(
                proxy={"server": proxy_url} if proxy_url is not None else None
            )
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            _attach_network_logging(page)
            if fixture_url is not None:
                page.route("**/*", lambda route: _block_non_fixture_requests(route, fixture_url))
            # Public pages may retain analytics connections or delay document events.
            # Commit plus a short render window keeps this POC bounded.
            print(f"analyzer navigating {_safe_url(navigation_url)}", flush=True)
            page.goto(navigation_url, wait_until="commit", timeout=20_000)
            print("analyzer navigation committed", flush=True)
            page.wait_for_timeout(1_000)
            print("analyzer capturing screenshot", flush=True)
            page.screenshot(path=OUTPUT_DIRECTORY / "screenshot.png", full_page=True)
            observations = _extract_observations(page)
            print(f"analyzer extracted {len(observations)} observations", flush=True)
            (OUTPUT_DIRECTORY / "page.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "title": page.title(),
                        "viewport": {"width": 1280, "height": 720},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (OUTPUT_DIRECTORY / "observations.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "style_allowlist_version": STYLE_ALLOWLIST_VERSION,
                        "style_allowlist": STYLE_ALLOWLIST,
                        "observations": observations,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            browser.close()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
    write_manifest(OUTPUT_DIRECTORY)


if __name__ == "__main__":
    main()
