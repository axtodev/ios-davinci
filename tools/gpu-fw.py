#!/usr/bin/env python3
"""Copy the Adreno 618 firmware off the phone, hash it, keep it out of git.

davinci still carries its Android vendor partition, and that is the only
copy of the GMU firmware, the SQE microcode and the ZAP shader we are
allowed to use for the a6xx submit path (docs/GPU.md, phase 3 and up).
The files are pulled into fw/ (gitignored) together with fw/manifest.txt,
which records size + sha256 so the kernel loader can check what it is
about to copy into GPU memory.

    python tools/gpu-fw.py                  # phone booted in Android
    python tools/gpu-fw.py --local C:/dl    # already pulled by hand
    python tools/gpu-fw.py --list           # only report what matches

Nothing outside fw/ is written and no partition is touched: this is
adb pull only.  In OrangeFox recovery mount /vendor first (it is not
mounted by default) or use --local after a manual dd.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fw"

# Vendor firmware lives in one of these, depending on the Android build.
FW_DIRS = (
    "/vendor/firmware_mnt/image",
    "/vendor/firmware",
    "/firmware/image",
    "/system/vendor/firmware",
)

# a6xx: GMU firmware, SQE ring microcode, ZAP shader, legacy shader blobs.
PATTERNS = ("a6xx*", "*gmu*", "*sqe*", "*zap*", "*.fw", "*.fw2")


def role(name: str) -> str:
    low = name.lower()
    if "gmu" in low:
        return "GMU firmware, CPU loaded (phase 3)"
    if "sqe" in low:
        return "SQE microcode, builds the ring (phase 3)"
    if "zap" in low:
        return "ZAP shader, TZ signed (needs the SECVID bypass)"
    if low.endswith((".fw", ".fw2")):
        return "shader microcode"
    return "?"


def find_adb(explicit: str | None) -> str | None:
    for cand in (explicit, os.environ.get("ADB"), shutil.which("adb")):
        if cand and Path(cand).exists():
            return str(cand)
    roots = [ROOT.parent, Path.home() / "Android" / "Sdk"]
    for env in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]))
    for root in roots:
        for name in ("adb.exe", "adb"):
            path = root / "platform-tools" / name
            if path.exists():
                return str(path)
    return None


def shell(adb: str, cmd: str) -> str:
    try:
        done = subprocess.run([adb, "shell", cmd], capture_output=True,
                              text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"adb shell failed: {exc}")
    return done.stdout.replace("\r", "")


def scan_device(adb: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in FW_DIRS:
        for name in shell(adb, f"ls -1 {directory} 2>/dev/null").split():
            low = name.lower()
            if name in seen or "/" in name:
                continue
            if any(fnmatch.fnmatch(low, pat) for pat in PATTERNS):
                seen.add(name)
                found.append((directory, name))
    return found


def scan_local(source: Path) -> list[tuple[str, str]]:
    found = []
    for entry in sorted(source.iterdir()):
        low = entry.name.lower()
        if entry.is_file() and any(fnmatch.fnmatch(low, pat) for pat in PATTERNS):
            found.append((str(source), entry.name))
    return found


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(names: list[str]) -> Path:
    manifest = OUT / "manifest.txt"
    lines = [
        "# Adreno 618 firmware pulled off davinci. Not redistributable:",
        "# never commit this directory (see .gitignore), keep it local.",
        "# sha256  size  name",
    ]
    for name in sorted(names):
        path = OUT / name
        lines.append(f"{sha256(path)}  {path.stat().st_size}  {name}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adb", help="path to adb (default: PATH, then platform-tools)")
    ap.add_argument("--local", type=Path, help="directory that already holds the files")
    ap.add_argument("--list", action="store_true", help="report matches, copy nothing")
    ap.add_argument("--force", action="store_true", help="overwrite fw/ files")
    args = ap.parse_args()

    if args.local:
        if not args.local.is_dir():
            sys.exit(f"not a directory: {args.local}")
        found = scan_local(args.local)
        source = args.local
    else:
        adb = find_adb(args.adb)
        if adb is None:
            sys.exit("adb not found: pass --adb, set ADB, or use --local")
        found = scan_device(adb)
        source = None

    if not found:
        print("no a6xx firmware found.")
        if source is None:
            print("booted in Android? else mount /vendor in recovery, or")
            print("pull the vendor partition and use --local <dir>.")
        return 1

    print(f"{len(found)} candidate file(s):")
    for directory, name in found:
        print(f"  {directory}/{name:24s} {role(name)}")
    if args.list:
        return 0

    OUT.mkdir(exist_ok=True)
    pulled: list[str] = []
    for directory, name in found:
        dest = OUT / name
        if dest.exists() and not args.force:
            print(f"  keep {name} (use --force to refresh)")
            pulled.append(name)
            continue
        if source is not None:
            shutil.copyfile(source / name, dest)
        else:
            done = subprocess.run([adb, "pull", f"{directory}/{name}", str(dest)])
            if done.returncode != 0:
                print(f"  FAILED {name}")
                continue
        pulled.append(name)

    manifest = write_manifest(pulled)
    total = sum((OUT / n).stat().st_size for n in pulled)
    print(f"\n{len(pulled)} file(s), {total} bytes -> {OUT}")
    print(f"manifest: {manifest}")
    print("fw/ is gitignored: keep the blobs on this machine only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
