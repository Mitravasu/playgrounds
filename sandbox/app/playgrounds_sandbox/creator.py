"""Build and inspect a supplied Storybook project without external network access."""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import zipfile
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from socket import socket
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Route, ViewportSize, sync_playwright

from playgrounds_sandbox.artifacts import write_manifest

INPUT_DIRECTORY = Path("/work/input")
OUTPUT_DIRECTORY = Path("/work/output")
TEMPLATE_DIRECTORY = Path("/opt/storybook-template")
PROJECT_DIRECTORY = Path("/tmp/storybook-project")
STATIC_DIRECTORY = Path("/tmp/storybook-static")
PROJECT_FILE = INPUT_DIRECTORY / "project.json"
MAX_FILES = 100
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000
ALLOWED_PATH = re.compile(
    r"^src/(?:tokens/[A-Za-z][A-Za-z0-9_-]*\.(?:css|json)|"
    r"components/[A-Z][A-Za-z0-9]*/[A-Z][A-Za-z0-9]*\.(?:tsx|css)|"
    r"components/[A-Z][A-Za-z0-9]*/[A-Z][A-Za-z0-9]*\.stories\.tsx)$"
)
PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class QuietHandler(SimpleHTTPRequestHandler):
    """Serve local build files without adding request noise to sandbox logs."""

    def log_message(self, format: str, *args: Any) -> None:
        return


def available_loopback_port() -> int:
    """Reserve an ephemeral port number on the container's loopback interface."""

    with closing(socket()) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _block_external_requests(route: Route, local_origin: str, blocked: list[str]) -> None:
    """Allow only the local static Storybook origin."""

    if route.request.url.startswith(local_origin):
        route.continue_()
        return
    blocked.append(route.request.url.split("?", 1)[0])
    route.abort()


def _load_project() -> dict[str, Any]:
    """Load and independently validate the bounded generated-file envelope."""

    try:
        project = json.loads(PROJECT_FILE.read_bytes())
    except FileNotFoundError as error:
        raise FileNotFoundError("creator job is missing project.json") from error
    except json.JSONDecodeError as error:
        raise ValueError("project.json is not valid JSON") from error
    if not isinstance(project, dict) or set(project) != {
        "schema_version",
        "plan",
        "files",
        "inferred_choices",
    }:
        raise ValueError("project.json has an invalid root shape")
    if project["schema_version"] != 1:
        raise ValueError("project.json uses an unsupported schema version")
    plan = project["plan"]
    files = project["files"]
    if not isinstance(plan, dict) or not isinstance(plan.get("stories"), list):
        raise TypeError("project.json plan must declare stories")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("project.json must contain a bounded non-empty file list")
    seen: set[str] = set()
    total_bytes = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "content"}:
            raise ValueError("generated files must contain exactly path and content")
        path = item["path"]
        content = item["content"]
        if not isinstance(path, str) or not isinstance(content, str):
            raise TypeError("generated file paths and contents must be strings")
        normalized = PurePosixPath(path)
        if normalized.as_posix() != path or normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError(f"generated file path is unsafe: {path}")
        if not ALLOWED_PATH.fullmatch(path):
            raise ValueError(f"generated file path is not allowed: {path}")
        if path in seen:
            raise ValueError(f"generated file path is duplicated: {path}")
        seen.add(path)
        content_bytes = len(content.encode())
        if content_bytes > MAX_FILE_BYTES:
            raise ValueError(f"generated file is too large: {path}")
        total_bytes += content_bytes
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError("generated project exceeds the total source-size limit")
    return project


def _assemble_project(project: dict[str, Any]) -> None:
    """Create a writable source tree linked to the immutable dependency tree."""

    PROJECT_DIRECTORY.mkdir(parents=True)
    shutil.copytree(TEMPLATE_DIRECTORY / ".storybook", PROJECT_DIRECTORY / ".storybook")
    shutil.copytree(TEMPLATE_DIRECTORY / "public", PROJECT_DIRECTORY / "public")
    shutil.copy2(TEMPLATE_DIRECTORY / "package.json", PROJECT_DIRECTORY / "package.json")
    shutil.copy2(TEMPLATE_DIRECTORY / "tsconfig.json", PROJECT_DIRECTORY / "tsconfig.json")
    (PROJECT_DIRECTORY / "node_modules").symlink_to(TEMPLATE_DIRECTORY / "node_modules")
    global_css = PROJECT_DIRECTORY / "src" / "global.css"
    global_css.parent.mkdir(parents=True)
    shutil.copy2(TEMPLATE_DIRECTORY / "src" / "global.css", global_css)
    for item in project["files"]:
        destination = PROJECT_DIRECTORY / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(item["content"], encoding="utf-8")


def _run_command(command: list[str], timeout: int) -> tuple[bool, str]:
    """Run one fixed build command and return bounded combined diagnostics."""

    result = subprocess.run(
        command,
        cwd=PROJECT_DIRECTORY,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    log = f"{result.stdout}\n{result.stderr}".strip()
    return result.returncode == 0, log[-32_000:]


def _story_entries() -> list[dict[str, str]]:
    """Read the static Storybook index and retain only renderable stories."""

    try:
        index = json.loads((STATIC_DIRECTORY / "index.json").read_bytes())
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("static Storybook has no valid story index") from error
    entries = index.get("entries") if isinstance(index, dict) else None
    if not isinstance(entries, dict):
        raise TypeError("static Storybook index has no entries object")
    stories: list[dict[str, str]] = []
    for story_id, value in sorted(entries.items()):
        if not isinstance(value, dict) or value.get("type") != "story":
            continue
        title = value.get("title")
        name = value.get("name")
        if isinstance(story_id, str) and isinstance(title, str) and isinstance(name, str):
            stories.append({"id": story_id, "title": title, "name": name})
    return stories


def _viewport_for(story: dict[str, str], plan: dict[str, Any]) -> ViewportSize:
    """Resolve the declared viewport for a built story, defaulting to desktop."""

    for declared in plan["stories"]:
        if not isinstance(declared, dict):
            continue
        if (
            str(declared.get("component", "")).casefold() in story["title"].casefold()
            and str(declared.get("name", "")).casefold() == story["name"].casefold()
        ):
            if declared.get("viewport") == "mobile":
                return {"width": 390, "height": 844}
            break
    return {"width": 1280, "height": 720}


def _inspect_page(page: Page) -> dict[str, object]:
    """Collect deterministic render and baseline-accessibility facts."""

    return page.evaluate(
        """() => {
            const root = document.querySelector("#storybook-root");
            const interactiveSelector = [
              "button", "a[href]", "input", "select", "textarea",
              "[role=button]", "[role=menuitem]", "[role=checkbox]",
              "[role=radio]", "[role=switch]", "[tabindex]"
            ].join(",");
            const interactive = root ? [...root.querySelectorAll(interactiveSelector)] : [];
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
            const bounds = root?.firstElementChild?.getBoundingClientRect();
            return {
              root_found: Boolean(root?.firstElementChild),
              root_bounds: bounds
                ? {width: bounds.width, height: bounds.height}
                : {width: 0, height: 0},
              interactive_count: interactive.length,
              unnamed_interactive_count: interactive.filter(
                (element) => !accessibleName(element)
              ).length,
              document_size: {
                width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight
              },
              viewport_size: {
                width: window.innerWidth,
                height: window.innerHeight
              }
            };
        }"""
    )


def _render_stories(
    stories: list[dict[str, str]], plan: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Render every story and capture one representative screenshot."""

    port = available_loopback_port()
    handler = lambda *args, **kwargs: QuietHandler(*args, directory=STATIC_DIRECTORY, **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    local_origin = f"http://127.0.0.1:{port}/"
    results: list[dict[str, Any]] = []
    all_errors: list[str] = []
    all_blocked: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for index, story in enumerate(stories):
                errors: list[str] = []
                blocked: list[str] = []
                viewport = _viewport_for(story, plan)
                page = browser.new_page(viewport=viewport)
                page.on("pageerror", lambda error, target=errors: target.append(str(error)))
                page.on(
                    "console",
                    lambda message, target=errors: (
                        target.append(message.text) if message.type == "error" else None
                    ),
                )
                page.route(
                    "**/*",
                    lambda route, target=blocked: _block_external_requests(
                        route, local_origin, target
                    ),
                )
                try:
                    page.goto(
                        f"{local_origin}iframe.html?id={story['id']}&viewMode=story",
                        wait_until="networkidle",
                    )
                    page.wait_for_selector("#storybook-root > *", timeout=5_000)
                    page.wait_for_timeout(200)
                    inspection = _inspect_page(page)
                    if index == 0:
                        page.screenshot(path=OUTPUT_DIRECTORY / "screenshot.png", full_page=True)
                except PlaywrightError as error:
                    errors.append(str(error))
                    inspection = {
                        "root_found": False,
                        "root_bounds": {"width": 0, "height": 0},
                        "interactive_count": 0,
                        "unnamed_interactive_count": 0,
                    }
                results.append(
                    {
                        **story,
                        "viewport": viewport,
                        "errors": errors,
                        "blocked_requests": blocked,
                        "inspection": inspection,
                    }
                )
                all_errors.extend(f"{story['id']}: {error}" for error in errors)
                all_blocked.extend(blocked)
                page.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return results, all_errors, sorted(set(all_blocked))


def _write_storybook_archive() -> None:
    """Create one bounded portable static-build artifact."""

    with zipfile.ZipFile(
        OUTPUT_DIRECTORY / "storybook.zip", mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for path in sorted(STATIC_DIRECTORY.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(STATIC_DIRECTORY).as_posix())


def _write_failure_outputs(
    *,
    typecheck_succeeded: bool,
    expected_story_count: int,
    typecheck_log: str,
    build_log: str,
) -> None:
    """Return inspectable hard-gate evidence even when compilation fails."""

    (OUTPUT_DIRECTORY / "screenshot.png").write_bytes(PLACEHOLDER_PNG)
    with zipfile.ZipFile(OUTPUT_DIRECTORY / "storybook.zip", mode="w"):
        pass
    (OUTPUT_DIRECTORY / "render.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build": {
                    "typecheck_succeeded": typecheck_succeeded,
                    "storybook_succeeded": False,
                    "expected_story_count": expected_story_count,
                    "story_count": 0,
                },
                "errors": [build_log or typecheck_log or "Storybook build failed"],
                "blocked_requests": [],
                "stories": [],
                "typecheck_log": typecheck_log,
                "build_log": build_log,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_manifest(OUTPUT_DIRECTORY)


def main() -> None:
    """Materialize, build, render, and package one generated Storybook."""

    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    project = _load_project()
    expected_story_count = len(project["plan"]["stories"])
    _assemble_project(project)
    typecheck_succeeded, typecheck_log = _run_command(
        ["npm", "run", "typecheck", "--prefix", str(PROJECT_DIRECTORY)], timeout=30
    )
    if not typecheck_succeeded:
        _write_failure_outputs(
            typecheck_succeeded=False,
            expected_story_count=expected_story_count,
            typecheck_log=typecheck_log,
            build_log="",
        )
        return
    build_succeeded, build_log = _run_command(
        [
            "npm",
            "run",
            "build-storybook",
            "--prefix",
            str(PROJECT_DIRECTORY),
            "--",
            "--output-dir",
            str(STATIC_DIRECTORY),
        ],
        timeout=60,
    )
    if not build_succeeded:
        _write_failure_outputs(
            typecheck_succeeded=True,
            expected_story_count=expected_story_count,
            typecheck_log=typecheck_log,
            build_log=build_log,
        )
        return
    stories = _story_entries()
    rendered, errors, blocked = _render_stories(stories, project["plan"])
    if not (OUTPUT_DIRECTORY / "screenshot.png").exists():
        (OUTPUT_DIRECTORY / "screenshot.png").write_bytes(PLACEHOLDER_PNG)
    _write_storybook_archive()
    (OUTPUT_DIRECTORY / "render.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build": {
                    "typecheck_succeeded": True,
                    "storybook_succeeded": True,
                    "expected_story_count": expected_story_count,
                    "story_count": len(stories),
                },
                "errors": errors,
                "blocked_requests": blocked,
                "stories": rendered,
                "typecheck_log": typecheck_log,
                "build_log": build_log,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    write_manifest(OUTPUT_DIRECTORY)


if __name__ == "__main__":
    main()
