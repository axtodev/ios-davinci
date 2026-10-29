#!/usr/bin/env python3
"""Keep dispatching the xnu workflow until a macOS runner is actually assigned.

The free macOS arm64 pool frequently answers with

    The job was not acquired by Runner of type hosted even after multiple
    attempts

after ~15 minutes in queue, and sometimes with "your account is locked due
to a billing issue". Neither is a build failure: the job never ran. So:
dispatch, wait, and on a non-build outcome try again after a pause. When a
run finally succeeds its `xnu-kernel` artifact is downloaded to
`xnu/out-<stamp>/`.

    python tools/xnu-ci-retry.py            # 6 attempts, 10 min apart
    python tools/xnu-ci-retry.py 12 900     # 12 attempts, 15 min apart
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = "xnu.yml"
REF = "gpu-bringup"
ARTIFACT = "xnu-kernel"


def gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def dispatch() -> int:
    gh("workflow", "run", WORKFLOW, "--ref", REF)
    time.sleep(20)                      # let the run show up in the list
    runs = json.loads(gh("run", "list", "--workflow", WORKFLOW, "--limit", "1",
                         "--json", "databaseId"))
    return int(runs[0]["databaseId"])


def wait_run(run_id: int, timeout: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = json.loads(gh("run", "view", str(run_id),
                             "--json", "status,conclusion"))
        if info["status"] == "completed":
            return str(info["conclusion"])
        time.sleep(60)
    return "timeout"


def main() -> int:
    attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    pause = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    for n in range(1, attempts + 1):
        run_id = dispatch()
        print(f"[{n}/{attempts}] dispatched {run_id}", flush=True)
        conclusion = wait_run(run_id, timeout=6 * 3600)
        print(f"[{n}/{attempts}] {run_id}: {conclusion}", flush=True)
        if conclusion == "success":
            out = ROOT / "xnu" / ("out-" + time.strftime("%Y%m%d-%H%M"))
            gh("run", "download", str(run_id), "-n", ARTIFACT, "-D", str(out))
            print(f"kernel artifact -> {out}", flush=True)
            return 0
        if n < attempts:
            time.sleep(pause)
    print("no runner in this batch; dispatch again later", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
