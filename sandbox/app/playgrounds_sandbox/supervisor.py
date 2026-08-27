"""Run one fixed sandbox job and hold its ephemeral artifacts for collection."""

import json
import os
import subprocess
import time
from pathlib import Path

STATUS_PATH = Path("/tmp/playgrounds-job-status.json")


def main() -> None:
    """Run the configured fixed entrypoint, then await trusted collection."""

    entrypoint = os.environ["PLAYGROUNDS_JOB_ENTRYPOINT"].split()
    result = subprocess.run(entrypoint, check=False)
    STATUS_PATH.write_text(json.dumps({"exit_code": result.returncode}), encoding="utf-8")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
