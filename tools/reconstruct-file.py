#!/usr/bin/env python3
"""Rebuild the current content of one XNU file from xnu/patches/.

The repo only carries patches; the tree they apply to lives in CI. This
replays the new-file hunk that creates the path plus every later patch that
touches it, using `git apply --include`, so a single file can be read and
edited locally.

    tools/reconstruct-file.py iokit/Kernel/arm/SweetFramebuffer.cpp OUT
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCHES = ROOT / "xnu" / "patches"


def read(path: Path) -> str:
    # Patches are checked out with CRLF on Windows; git apply is byte exact.
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")


def created_by(path: str) -> Path:
    for patch in sorted(PATCHES.glob("*.patch")):
        text = read(patch)
        d = text.find(f"+++ b/{path}\n")
        if d >= 0 and text.rfind("--- ", 0, d) >= 0 and \
                text[text.rfind("--- ", 0, d):d].startswith("--- /dev/null"):
            return patch
    raise SystemExit(f"{path} is not created by any patch")


def extract_new_file(patch: Path, path: str) -> str:
    text = read(patch)
    d = text.find(f"+++ b/{path}\n")
    body = text[d:].split("\n")[1:]
    if body and body[0].startswith("@@"):
        body = body[1:]
    lines = []
    for line in body:
        if line.startswith(("diff ", "--- ", "+++ ")):
            break
        if line.startswith("+"):
            lines.append(line[1:])
        elif line.startswith("\\"):
            continue
        else:
            break
    return "\n".join(lines) + "\n"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    path, out = sys.argv[1], Path(sys.argv[2])
    base = created_by(path)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(extract_new_file(base, path), encoding="utf-8", newline="\n")
        git = ["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf"]
        subprocess.run(git + ["init", "-q"], cwd=work, check=True)
        subprocess.run(git + ["add", "-A"], cwd=work, check=True)
        subprocess.run(git + ["-c", "user.email=a@b", "-c", "user.name=a",
                              "commit", "-qm", "base"], cwd=work, check=True)
        applied = []
        for patch in sorted(PATCHES.glob("*.patch")):
            if patch == base or f"b/{path}\n" not in read(patch):
                continue
            local = work / patch.name
            local.write_text(read(patch), encoding="utf-8", newline="\n")
            r = subprocess.run(
                git + ["apply", "-p1", f"--include={path}", str(local)],
                cwd=work, capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"{patch.name} failed: {r.stderr.strip()}")
            applied.append(patch.name)
        out.write_text(target.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    print(f"{path}: base {base.name} + {len(applied)} patches -> {out}")
    for name in applied:
        print(f"  {name}")


if __name__ == "__main__":
    main()
