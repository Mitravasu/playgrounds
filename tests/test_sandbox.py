import hashlib
import io
import json
import tarfile
from typing import Any

import pytest
from pydantic import ValidationError

from playgrounds.sandbox import (
    PUBLIC_ANALYZER_RUNTIME_PROFILE,
    AnalyzerJobRequest,
    CreatorJobRequest,
    PublicAnalyzerJobRequest,
    SandboxArtifact,
    SandboxJobKind,
    SandboxJobRequest,
    SandboxRunner,
    runtime_profile_for,
)


def test_creator_profile_is_fixed_and_offline() -> None:
    profile = runtime_profile_for(SandboxJobKind.CREATOR)

    assert profile.network_mode == "none"
    assert profile.entrypoint == ("python", "-m", "playgrounds_sandbox.creator")


def test_analyzer_profile_is_fixed_and_offline() -> None:
    profile = runtime_profile_for(SandboxJobKind.ANALYZER)

    assert profile.network_mode == "none"
    assert profile.entrypoint == ("python", "-m", "playgrounds_sandbox.analyzer")


def test_analyzer_request_accepts_only_its_fixed_fixture_and_artifacts() -> None:
    request = AnalyzerJobRequest(
        inputs=(SandboxArtifact(path="page.html", media_type="text/html"),),
        outputs=(
            SandboxArtifact(path="screenshot.png", media_type="image/png"),
            SandboxArtifact(path="page.json", media_type="application/json"),
            SandboxArtifact(path="observations.json", media_type="application/json"),
        ),
    )

    assert request.kind is SandboxJobKind.ANALYZER


def test_analyzer_request_rejects_an_undeclared_artifact() -> None:
    with pytest.raises(ValidationError, match="declared evidence artifacts"):
        AnalyzerJobRequest(
            inputs=(SandboxArtifact(path="page.html", media_type="text/html"),),
            outputs=(SandboxArtifact(path="screenshot.png", media_type="image/png"),),
        )


def test_creator_request_accepts_only_component_files_and_render_diagnostics() -> None:
    request = CreatorJobRequest(
        inputs=(
            SandboxArtifact(path="component.html", media_type="text/html"),
            SandboxArtifact(path="component.css", media_type="text/css"),
            SandboxArtifact(path="component.js", media_type="text/javascript"),
        ),
        outputs=(
            SandboxArtifact(path="screenshot.png", media_type="image/png"),
            SandboxArtifact(path="render.json", media_type="application/json"),
        ),
    )

    assert request.kind is SandboxJobKind.CREATOR


@pytest.mark.parametrize(
    "url",
    (
        "http://www.mitravasu.com/",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://www.mitravasu.com:444/",
    ),
)
def test_public_analyzer_request_rejects_unsafe_or_untrusted_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        PublicAnalyzerJobRequest(
            url=url,
            outputs=(
                SandboxArtifact(path="screenshot.png", media_type="image/png"),
                SandboxArtifact(path="page.json", media_type="application/json"),
                SandboxArtifact(path="observations.json", media_type="application/json"),
            ),
        )


def test_public_analyzer_request_accepts_a_public_https_hostname() -> None:
    request = PublicAnalyzerJobRequest(
        url="https://www.mitravasu.com/",
        outputs=(
            SandboxArtifact(path="screenshot.png", media_type="image/png"),
            SandboxArtifact(path="page.json", media_type="application/json"),
            SandboxArtifact(path="observations.json", media_type="application/json"),
        ),
    )

    assert request.url == "https://www.mitravasu.com/"
    assert PUBLIC_ANALYZER_RUNTIME_PROFILE.memory_limit == "2g"
    assert PUBLIC_ANALYZER_RUNTIME_PROFILE.temporary_storage_limit == "512m"


def test_public_analyzer_request_canonicalizes_a_hostname_without_a_path() -> None:
    request = PublicAnalyzerJobRequest(
        url="https://www.mitravasu.com",
        outputs=(
            SandboxArtifact(path="screenshot.png", media_type="image/png"),
            SandboxArtifact(path="page.json", media_type="application/json"),
            SandboxArtifact(path="observations.json", media_type="application/json"),
        ),
    )

    assert request.url == "https://www.mitravasu.com/"


def test_public_analyzer_request_accepts_a_second_public_https_hostname() -> None:
    request = PublicAnalyzerJobRequest(
        url="https://example.com/",
        outputs=(
            SandboxArtifact(path="screenshot.png", media_type="image/png"),
            SandboxArtifact(path="page.json", media_type="application/json"),
            SandboxArtifact(path="observations.json", media_type="application/json"),
        ),
    )

    assert request.url == "https://example.com/"


@pytest.mark.parametrize("path", ("/etc/passwd", "../secret", "output/../../secret", "."))
def test_artifact_path_rejects_paths_outside_the_job_workspace(path: str) -> None:
    with pytest.raises(ValidationError, match="relative"):
        SandboxArtifact(path=path, media_type="text/plain")


def test_job_request_rejects_runtime_controls() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SandboxJobRequest.model_validate(
            {
                "kind": "creator",
                "outputs": ({"path": "screenshot.png", "media_type": "image/png"},),
                "network_mode": "bridge",
            }
        )


class FakeContainer:
    def __init__(self, output_archive: bytes) -> None:
        self.output_archive = output_archive
        self.put_archives: list[tuple[str, bytes]] = []
        self.operations: list[str] = []
        self.started = False
        self.removed = False

    def put_archive(self, path: str, archive: bytes) -> None:
        self.operations.append("put_archive")
        self.put_archives.append((path, archive))

    def start(self) -> None:
        self.operations.append("start")
        self.started = True

    def wait(self, *, timeout: int) -> dict[str, int]:
        assert timeout == 30
        return {"StatusCode": 0}

    def exec_run(self, command: list[str], **kwargs: object) -> tuple[int, bytes]:
        assert kwargs == {"user": "sandbox"} or kwargs == {}
        if command == ["sh", "-c", "chmod -R a+rX /work/input"]:
            self.operations.append("prepare_input")
            return 0, b""
        if command == ["cat", "/tmp/playgrounds-job-status.json"]:
            self.operations.append("read_status")
            return 0, b'{"exit_code": 0}'
        if command == ["tar", "--create", "--file=-", "--directory=/work/output", "."]:
            self.operations.append("collect_output")
            return 0, self.output_archive
        raise AssertionError(f"unexpected command: {command}")

    def logs(self, **_: object) -> bytes:
        return b"creator completed"

    def get_archive(self, path: str) -> tuple[list[bytes], dict[str, object]]:
        assert path == "/work/output"
        return [self.output_archive], {}

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.removed = True


class FakeContainers:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = containers
        self.create_kwargs: list[dict[str, Any]] = []

    def create(self, _image: str, **kwargs: Any) -> FakeContainer:
        self.create_kwargs.append(kwargs)
        return self._containers.pop(0)


class FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name
        self.removed = False

    def remove(self, *, force: bool) -> None:
        assert force is True
        self.removed = True


class FakeVolumes:
    def __init__(self) -> None:
        self.created: list[FakeVolume] = []

    def create(self, *, labels: dict[str, str]) -> FakeVolume:
        assert labels == {"playgrounds.sandbox": "true"}
        volume = FakeVolume(f"volume-{len(self.created)}")
        self.created.append(volume)
        return volume


class FakeClient:
    def __init__(self, staging_container: FakeContainer, job_container: FakeContainer) -> None:
        self.containers = FakeContainers([staging_container, job_container])
        self.volumes = FakeVolumes()


def output_archive(files: dict[str, bytes]) -> bytes:
    archive = io.BytesIO()
    manifest = {
        "artifacts": [
            {
                "path": path,
                "media_type": "text/html",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in files.items()
        ]
    }
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for path, content in {"manifest.json": json.dumps(manifest).encode(), **files}.items():
            info = tarfile.TarInfo(name=f"output/{path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return archive.getvalue()


def test_runner_uses_creator_security_profile_and_cleans_up() -> None:
    artifact = SandboxArtifact(path="component.html", media_type="text/html")
    staging_container = FakeContainer(b"")
    job_container = FakeContainer(output_archive({"component.html": b"<button>Menu</button>"}))
    client = FakeClient(staging_container, job_container)
    request = SandboxJobRequest(kind="creator", inputs=(artifact,), outputs=(artifact,))

    result = SandboxRunner(client, image="playgrounds-browser@sha256:example").run(
        request, {"component.html": b"<button>Menu</button>"}
    )

    assert result.succeeded is True
    assert result.outputs == {"component.html": b"<button>Menu</button>"}
    assert staging_container.operations == ["start", "put_archive", "prepare_input"]
    assert staging_container.removed is True
    assert job_container.started is True
    assert job_container.removed is True
    assert job_container.operations == ["start", "read_status", "collect_output"]
    assert [volume.removed for volume in client.volumes.created] == [True]
    assert client.containers.create_kwargs == [
        {
            "command": ["sleep", "30"],
            "detach": True,
            "user": "root",
            "runtime": "playgrounds-runsc",
            "network_mode": "none",
            "volumes": {
                "volume-0": {"bind": "/work/input", "mode": "rw"},
            },
        },
        {
            "command": ["python", "-m", "playgrounds_sandbox.supervisor"],
            "detach": True,
            "user": "sandbox",
            "runtime": "playgrounds-runsc",
            "network_mode": "none",
            "environment": {"PLAYGROUNDS_JOB_ENTRYPOINT": "python -m playgrounds_sandbox.creator"},
            "read_only": True,
            "tmpfs": {
                "/tmp": "rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
                "/work/output": "rw,noexec,nosuid,size=100m,uid=10001,gid=10001",
            },
            "volumes": {
                "volume-0": {"bind": "/work/input", "mode": "ro"},
            },
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": "1g",
            "nano_cpus": 1_000_000_000,
            "pids_limit": 64,
        },
    ]


def test_runner_rejects_an_output_that_exceeds_its_declared_limit() -> None:
    artifact = SandboxArtifact(path="component.html", media_type="text/html", max_bytes=3)
    staging_container = FakeContainer(b"")
    job_container = FakeContainer(output_archive({"component.html": b"too large"}))
    client = FakeClient(staging_container, job_container)
    request = SandboxJobRequest(kind="creator", inputs=(), outputs=(artifact,))

    result = SandboxRunner(client, image="playgrounds-browser@sha256:example").run(request, {})

    assert result.succeeded is False
    assert result.error == "sandbox output exceeds the declared size limit: component.html"
    assert job_container.removed is True
