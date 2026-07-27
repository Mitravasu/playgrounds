"""Typed contracts for disposable, fixed-purpose sandbox jobs."""

import hashlib
import io
import ipaddress
import json
import tarfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from docker.errors import DockerException
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from requests.exceptions import RequestException


class SandboxJobKind(StrEnum):
    """The only job types the trusted orchestrator may start."""

    ANALYZER = "analyzer"
    CREATOR = "creator"


class SandboxArtifact(BaseModel):
    """A file that may cross the trusted application/sandbox boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    @field_validator("path")
    @classmethod
    def validate_relative_posix_path(cls, value: str) -> str:
        """Allow only a non-traversing, relative POSIX path."""

        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in {".", ""}:
            message = "artifact paths must be relative and must not contain '..'"
            raise ValueError(message)
        return path.as_posix()


class SandboxJobRequest(BaseModel):
    """A fixed-purpose job request without arbitrary runtime controls."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SandboxJobKind
    inputs: tuple[SandboxArtifact, ...] = ()
    outputs: tuple[SandboxArtifact, ...] = Field(min_length=1)


class AnalyzerJobRequest(SandboxJobRequest):
    """Offline analyzer request with one local page and fixed evidence artifacts."""

    kind: Literal[SandboxJobKind.ANALYZER] = SandboxJobKind.ANALYZER

    @model_validator(mode="after")
    def validate_analyzer_contract(self) -> "AnalyzerJobRequest":
        """Keep the offline analyzer's input and output surface fixed."""

        expected_inputs = {"page.html": "text/html"}
        expected_outputs = {
            "screenshot.png": "image/png",
            "page.json": "application/json",
            "observations.json": "application/json",
        }
        actual_inputs = {artifact.path: artifact.media_type for artifact in self.inputs}
        actual_outputs = {artifact.path: artifact.media_type for artifact in self.outputs}
        if len(self.inputs) != 1 or actual_inputs != expected_inputs:
            raise ValueError("analyzer jobs require exactly one text/html input: page.html")
        if len(self.outputs) != len(expected_outputs) or actual_outputs != expected_outputs:
            raise ValueError("analyzer jobs require the declared evidence artifacts")
        return self


def validate_public_analyzer_url(value: str) -> str:
    """Accept a manual public HTTPS URL before proxy-side address validation."""

    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("analyzer URLs must be credential-free HTTPS URLs on port 443")
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("analyzer URLs must not use literal IP addresses")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("analyzer URLs must not target localhost")
    return value


class PublicAnalyzerJobRequest(SandboxJobRequest):
    """Controlled public analyzer request for a manual HTTPS URL."""

    kind: Literal[SandboxJobKind.ANALYZER] = SandboxJobKind.ANALYZER
    url: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def normalize_public_url(cls, value: str) -> str:
        """Validate once and retain one canonical URL across the workflow."""

        validate_public_analyzer_url(value)
        return str(HttpUrl(value))

    @model_validator(mode="after")
    def validate_public_analyzer_contract(self) -> "PublicAnalyzerJobRequest":
        """Keep public analysis artifact and input surfaces fixed."""

        expected_outputs = {
            "screenshot.png": "image/png",
            "page.json": "application/json",
            "observations.json": "application/json",
        }
        actual_outputs = {artifact.path: artifact.media_type for artifact in self.outputs}
        if self.inputs:
            raise ValueError("public analyzer jobs do not accept workspace inputs")
        validate_public_analyzer_url(self.url)
        if len(self.outputs) != len(expected_outputs) or actual_outputs != expected_outputs:
            raise ValueError("analyzer jobs require the declared evidence artifacts")
        return self


class CreatorJobRequest(SandboxJobRequest):
    """Offline creator request with one validated generated Storybook project."""

    kind: Literal[SandboxJobKind.CREATOR] = SandboxJobKind.CREATOR

    @model_validator(mode="after")
    def validate_creator_contract(self) -> "CreatorJobRequest":
        """Keep creator inputs and Storybook outputs fixed."""

        expected_inputs = {
            "project.json": "application/json",
        }
        expected_outputs = {
            "screenshot.png": "image/png",
            "render.json": "application/json",
            "storybook.zip": "application/zip",
        }
        actual_inputs = {artifact.path: artifact.media_type for artifact in self.inputs}
        actual_outputs = {artifact.path: artifact.media_type for artifact in self.outputs}
        if len(self.inputs) != len(expected_inputs) or actual_inputs != expected_inputs:
            raise ValueError("creator jobs require exactly one generated Storybook project")
        if len(self.outputs) != len(expected_outputs) or actual_outputs != expected_outputs:
            raise ValueError(
                "creator jobs require Storybook, screenshot, and render diagnostics outputs"
            )
        return self


class SandboxRuntimeProfile(BaseModel):
    """Trusted runtime settings selected solely from a job kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entrypoint: tuple[str, ...]
    network_mode: str
    timeout_seconds: int = Field(gt=0)
    memory_limit: str
    temporary_storage_limit: str
    cpu_count: int = Field(gt=0)
    pid_limit: int = Field(gt=0)


SANDBOX_RUNTIME_PROFILES: dict[SandboxJobKind, SandboxRuntimeProfile] = {
    SandboxJobKind.CREATOR: SandboxRuntimeProfile(
        entrypoint=("python", "-m", "playgrounds_sandbox.creator"),
        network_mode="none",
        timeout_seconds=120,
        memory_limit="2g",
        temporary_storage_limit="512m",
        cpu_count=2,
        pid_limit=128,
    ),
    SandboxJobKind.ANALYZER: SandboxRuntimeProfile(
        entrypoint=("python", "-m", "playgrounds_sandbox.analyzer"),
        network_mode="none",
        timeout_seconds=30,
        memory_limit="1g",
        temporary_storage_limit="64m",
        cpu_count=1,
        pid_limit=64,
    ),
}

PUBLIC_ANALYZER_RUNTIME_PROFILE = SandboxRuntimeProfile(
    entrypoint=("python", "-m", "playgrounds_sandbox.analyzer"),
    network_mode="playgrounds-analyzer-egress",
    timeout_seconds=90,
    memory_limit="2g",
    temporary_storage_limit="512m",
    cpu_count=1,
    pid_limit=64,
)
ANALYZER_PROXY_HOST = "playgrounds-egress-proxy"
ANALYZER_PROXY_IP = "172.30.0.2"


def runtime_profile_for(kind: SandboxJobKind) -> SandboxRuntimeProfile:
    """Return the application-owned runtime profile for a fixed job kind."""

    return SANDBOX_RUNTIME_PROFILES[kind]


@dataclass(frozen=True)
class SandboxJobResult:
    """The bounded result returned after a sandbox container is removed."""

    succeeded: bool
    exit_code: int | None
    outputs: dict[str, bytes]
    logs: str
    error: str | None = None


class SandboxRunner:
    """Run fixed-purpose jobs with application-owned Docker restrictions."""

    _runtime_name = "playgrounds-runsc"
    _input_directory = "/work/input"
    _output_directory = "/work/output"
    _status_path = "/tmp/playgrounds-job-status.json"
    _max_log_bytes = 64 * 1024
    _max_output_bytes = 100 * 1024 * 1024

    def __init__(self, client: Any, *, image: str) -> None:
        self._client = client
        self._image = image

    def run(
        self,
        request: SandboxJobRequest,
        input_files: Mapping[str, bytes],
    ) -> SandboxJobResult:
        """Run a job, validating its declared artifacts and always cleaning up."""

        self._validate_inputs(request, input_files)
        profile = (
            PUBLIC_ANALYZER_RUNTIME_PROFILE
            if isinstance(request, PublicAnalyzerJobRequest)
            else runtime_profile_for(request.kind)
        )
        container: Any | None = None
        staging_container: Any | None = None
        input_volume: Any | None = None
        try:
            input_volume = self._create_workspace_volume()
            staging_container = self._client.containers.create(
                self._image,
                command=["sleep", "30"],
                detach=True,
                user="root",
                runtime=self._runtime_name,
                network_mode="none",
                volumes={
                    input_volume.name: {"bind": self._input_directory, "mode": "rw"},
                },
            )
            staging_container.start()
            staging_container.put_archive(self._input_directory, self._make_archive(input_files))
            self._prepare_workspace_permissions(staging_container)
            staging_container.remove(force=True)
            staging_container = None
            container_options: dict[str, Any] = {}
            if isinstance(request, PublicAnalyzerJobRequest):
                # gVisor's network stack does not resolve Docker service aliases
                # on this host. This maps only the internal proxy endpoint.
                container_options["extra_hosts"] = {ANALYZER_PROXY_HOST: ANALYZER_PROXY_IP}
            container = self._client.containers.create(
                self._image,
                command=["python", "-m", "playgrounds_sandbox.supervisor"],
                detach=True,
                user="sandbox",
                runtime=self._runtime_name,
                network_mode=profile.network_mode,
                environment=self._job_environment(request, profile),
                read_only=True,
                tmpfs={
                    "/tmp": (
                        "rw,noexec,nosuid,"
                        f"size={profile.temporary_storage_limit},uid=10001,gid=10001"
                    ),
                    "/work/output": "rw,noexec,nosuid,size=100m,uid=10001,gid=10001",
                },
                volumes={
                    input_volume.name: {"bind": self._input_directory, "mode": "ro"},
                },
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=profile.memory_limit,
                nano_cpus=profile.cpu_count * 1_000_000_000,
                pids_limit=profile.pid_limit,
                **container_options,
            )
            container.start()
            exit_code = self._wait_for_job_result(container, profile.timeout_seconds)
            logs = self._limited_logs(container)
            if exit_code != 0:
                return SandboxJobResult(
                    succeeded=False,
                    exit_code=exit_code,
                    outputs={},
                    logs=logs,
                    error="sandbox job exited unsuccessfully",
                )
            return SandboxJobResult(
                succeeded=True,
                exit_code=exit_code,
                outputs=self._collect_outputs(
                    self._collect_output_archive(container), request.outputs
                ),
                logs=logs,
            )
        except (
            DockerException,
            RequestException,
            RuntimeError,
            TimeoutError,
            TypeError,
            ValueError,
            tarfile.TarError,
        ) as error:
            return SandboxJobResult(
                succeeded=False,
                exit_code=None,
                outputs={},
                logs=self._limited_logs(container) if container is not None else "",
                error=str(error),
            )
        finally:
            if container is not None:
                container.remove(force=True)
            if staging_container is not None:
                staging_container.remove(force=True)
            if input_volume is not None:
                input_volume.remove(force=True)

    @staticmethod
    def _job_environment(
        request: SandboxJobRequest, profile: SandboxRuntimeProfile
    ) -> dict[str, str]:
        environment = {"PLAYGROUNDS_JOB_ENTRYPOINT": " ".join(profile.entrypoint)}
        if isinstance(request, PublicAnalyzerJobRequest):
            environment["PLAYGROUNDS_ANALYZER_URL"] = request.url
            environment["PLAYGROUNDS_ANALYZER_PROXY"] = f"http://{ANALYZER_PROXY_HOST}:8080"
        return environment

    def _create_workspace_volume(self) -> Any:
        return self._client.volumes.create(labels={"playgrounds.sandbox": "true"})

    def _prepare_workspace_permissions(self, container: Any) -> None:
        result = container.exec_run(
            [
                "sh",
                "-c",
                f"chmod -R a+rX {self._input_directory}",
            ]
        )
        exit_code = result.exit_code if hasattr(result, "exit_code") else result[0]
        if exit_code != 0:
            raise RuntimeError("sandbox workspace permission setup failed")

    def _wait_for_job_result(self, container: Any, timeout_seconds: int) -> int:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = container.exec_run(["cat", self._status_path], user="sandbox")
            exit_code = result.exit_code if hasattr(result, "exit_code") else result[0]
            output = result.output if hasattr(result, "output") else result[1]
            if exit_code == 0:
                status = json.loads(bytes(output))
                if isinstance(status, dict) and isinstance(status.get("exit_code"), int):
                    return status["exit_code"]
                raise ValueError("sandbox supervisor returned an invalid status document")
            time.sleep(0.05)
        raise TimeoutError("sandbox job timed out")

    def _collect_output_archive(self, container: Any) -> bytes:
        result = container.exec_run(
            ["tar", "--create", "--file=-", "--directory=/work/output", "."], user="sandbox"
        )
        exit_code = result.exit_code if hasattr(result, "exit_code") else result[0]
        output = result.output if hasattr(result, "output") else result[1]
        archive = bytes(output)
        if exit_code != 0:
            raise RuntimeError("sandbox output collection failed")
        if len(archive) > self._max_output_bytes:
            raise ValueError("sandbox output archive exceeds the configured size limit")
        return archive

    @staticmethod
    def _validate_inputs(request: SandboxJobRequest, input_files: Mapping[str, bytes]) -> None:
        declared = {artifact.path: artifact for artifact in request.inputs}
        supplied = set(input_files)
        if supplied != set(declared):
            message = "input files must exactly match the declared input artifacts"
            raise ValueError(message)
        for path, content in input_files.items():
            if len(content) > declared[path].max_bytes:
                raise ValueError(f"sandbox input exceeds the declared size limit: {path}")

    @staticmethod
    def _make_archive(files: Mapping[str, bytes]) -> bytes:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for path, content in files.items():
                info = tarfile.TarInfo(name=path)
                info.size = len(content)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(content))
        return archive.getvalue()

    def _limited_logs(self, container: Any) -> str:
        logs = container.logs(stdout=True, stderr=True)[-self._max_log_bytes :]
        return logs.decode("utf-8", errors="replace")

    def _collect_outputs(
        self, archive: bytes, expected_outputs: tuple[SandboxArtifact, ...]
    ) -> dict[str, bytes]:
        files = self._read_output_archive(archive)
        manifest = self._read_manifest(files.pop("manifest.json", None))
        expected = {artifact.path: artifact.media_type for artifact in expected_outputs}
        size_limits = {artifact.path: artifact.max_bytes for artifact in expected_outputs}
        actual = {item["path"]: item["media_type"] for item in manifest["artifacts"]}
        if actual != expected or set(files) != set(expected):
            message = "sandbox output does not match the declared artifact manifest"
            raise ValueError(message)
        for item in manifest["artifacts"]:
            path = item["path"]
            content = files[path]
            if len(content) > size_limits[path]:
                raise ValueError(f"sandbox output exceeds the declared size limit: {path}")
            digest = hashlib.sha256(content).hexdigest()
            if item["sha256"] != digest:
                message = f"sandbox output digest mismatch: {path}"
                raise ValueError(message)
        return files

    @staticmethod
    def _read_output_archive(archive: bytes) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                path = PurePosixPath(member.name)
                parts = path.parts[1:] if path.parts and path.parts[0] == "output" else path.parts
                relative_path = PurePosixPath(*parts).as_posix()
                SandboxArtifact(path=relative_path, media_type="application/octet-stream")
                file_object = tar.extractfile(member)
                if file_object is None:
                    raise ValueError(f"unable to read sandbox output: {relative_path}")
                files[relative_path] = file_object.read()
        return files

    @staticmethod
    def _read_manifest(raw_manifest: bytes | None) -> dict[str, Any]:
        if raw_manifest is None:
            raise ValueError("sandbox output manifest is missing")
        try:
            manifest = json.loads(raw_manifest)
        except json.JSONDecodeError as error:
            raise ValueError("sandbox output manifest is invalid JSON") from error
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if not isinstance(artifacts, list):
            raise TypeError("sandbox output manifest has no artifact list")
        for item in artifacts:
            if not (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("media_type"), str)
                and isinstance(item.get("sha256"), str)
            ):
                raise TypeError("sandbox output manifest contains an invalid artifact")
        return {"artifacts": artifacts}
