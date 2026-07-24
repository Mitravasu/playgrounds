"""Write output manifests understood by the trusted sandbox runner."""

import hashlib
import json
import mimetypes
from pathlib import Path


def write_manifest(output_directory: Path) -> None:
    """Write the digest and media type of every output file except the manifest."""

    artifacts = []
    for path in sorted(output_directory.iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        artifacts.append(
            {
                "path": path.name,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (output_directory / "manifest.json").write_text(
        json.dumps({"artifacts": artifacts}, sort_keys=True), encoding="utf-8"
    )
