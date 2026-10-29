#!/usr/bin/env python3
"""Build the shim and wrap it in an Android boot v2 image, without bash.

shim/build.sh needs a POSIX shell, gawk, gtruncate, cpio and aosp-mkbootimg.
This does the same steps in Python, so the image can be built anywhere a
compiler exists: LLVM's clang + ld.lld (Windows or Linux) or aarch64-elf-gcc:

    python tools/make-boot.py --payload shim/out/payload-0086.bin shim/out/shim-boot-0086.img
    python tools/make-boot.py --payload payload.bin --dtb backup/davinci.dtb out.img
    python tools/make-boot.py --image-only shim/out/shim-boot-test.img

The payload is the ramdisk: tools/mkpayload.py output (kernel + ADT). Without
one, an empty cpio is packed and the shim runs its UFS diagnostics instead of
booting XNU, exactly like build.sh.

Image layout mirrors what build.sh asks mkbootimg for, so the ABL sees the
same header it has always booted: 4 KiB pages, kernel at 0x8000, ramdisk at
0x1000000, tags at 0x100, dtb at 0x1f00000, cmdline "console=null".
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "shim"

SOURCES = ("start.S", "shim.c", "console.c", "mmu.c", "fdt.c", "loader.c", "ufs.c")
CFLAGS = ["-O2", "-ffreestanding", "-nostdlib", "-fno-pic", "-mgeneral-regs-only",
          "-mstrict-align", "-fno-stack-protector", "-fno-jump-tables", "-Wall", "-Wextra"]
LDFLAGS = ["-static", "-Wl,--build-id=none", "-Wl,-N"]

PAGE = 4096
KERNEL_ADDR, RAMDISK_ADDR, SECOND_ADDR = 0x8000, 0x1000000, 0xf00000
TAGS_ADDR, DTB_ADDR = 0x100, 0x1f00000
OS_VERSION = ((17 << 14) | (0 << 7) | 0) << 11 | ((2035 - 2000) << 4 | 6)


def find_tool(explicit: str | None, names: tuple[str, ...]) -> str:
    for cand in (explicit, *(shutil.which(n) for n in names)):
        if cand and Path(cand).exists():
            return str(cand)
    sys.exit(f"none of {', '.join(names)} found (use the explicit option)")


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        sys.stderr.write(f"{' '.join(cmd)}\n{done.stdout}{done.stderr}")
        sys.exit(f"failed: {cmd[0]}")
    return done


def compile_shim(work: Path, cc: str, objcopy: str, nm: str, readelf: str) -> Path:
    clang = "clang" in Path(cc).stem.lower()
    flags = ([f"--target=aarch64-none-elf"] if clang else []) + CFLAGS
    objects = []
    for src in SOURCES:
        obj = work / f"{Path(src).stem}.o"
        run([cc, *flags, "-c", str(SHIM / src), "-o", str(obj)], work)
        objects.append(str(obj))
    # start.S keeps image_size absolute by design; a C object may not. A jump
    # table is an array of absolute pointers in .rodata, hence -fno-jump-tables.
    for obj in objects[1:]:
        listing = run([readelf, "-r", obj], work).stdout
        n = listing.count("ABS64")
        if n:
            sys.exit(f"{Path(obj).name}: {n} absolute relocations (no relocation pass)")
    elf = work / "shim.elf"
    link = [cc, *flags, *LDFLAGS, "-T", str(SHIM / "shim.ld"), *objects, "-o", str(elf)]
    if clang:
        link[1:1] = ["-fuse-ld=lld"]
    run(link, work)
    image = work / "Image"
    run([objcopy, "-O", "binary", str(elf), str(image)], work)
    # ABL copies the file: pad to _end so .bss and the stacks exist as zeroes.
    end = [ln.split() for ln in run([nm, str(elf)], work).stdout.splitlines()]
    size = next(int(cols[0], 16) for cols in end if len(cols) == 3 and cols[2] == "_end")
    with image.open("r+b") as f:
        f.truncate(size)
    print(f"shim: {image} ({size} bytes)")
    return image


def empty_cpio() -> bytes:
    name = b"TRAILER!!!\0"
    fields = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(name), 0]
    header = b"070701" + b"".join(b"%08x" % f for f in fields)
    blob = header + name
    return gzip.compress(blob + b"\0" * (-len(blob) % 4), 9)


def pack(kernel: Path, ramdisk: Path, out: Path, dtb: Path | None) -> None:
    for p in (kernel, ramdisk, dtb):
        if p is not None and not p.is_file():
            sys.exit(f"{p} is not a file")
    k, r = kernel.read_bytes(), ramdisk.read_bytes()
    d = dtb.read_bytes() if dtb else b""
    out.parent.mkdir(parents=True, exist_ok=True)
    hdr = bytearray(1660)
    struct.pack_into("<8s10I", hdr, 0, b"ANDROID!", len(k), KERNEL_ADDR, len(r), RAMDISK_ADDR,
                     0, SECOND_ADDR, TAGS_ADDR, PAGE, 2, OS_VERSION)
    hdr[64:64 + 11] = b"console=null"
    struct.pack_into("<I", hdr, 1632, 0)            # recovery_dtbo_size
    struct.pack_into("<I", hdr, 1644, 1660)         # header_size
    struct.pack_into("<IQ", hdr, 1648, len(d), DTB_ADDR)
    pad = lambda b: b + b"\0" * (-len(b) % PAGE)
    out.write_bytes(pad(bytes(hdr)) + pad(k) + pad(r) + (pad(d) if d else b""))
    print(f"image: {out} ({out.stat().st_size} bytes, dtb {'yes' if d else 'no'})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # --payload is a flag, not a positional: with two positionals a call like
    # `--image-only out.img` would silently make out.img the payload.
    ap.add_argument("out", nargs="?", default="shim/out/shim-boot.img",
                    help="boot image to write (fastboot boot <out>)")
    ap.add_argument("--payload", help="mkpayload.py output (kernel + ADT) as the ramdisk")
    ap.add_argument("--dtb", help="device tree to embed (davinci.dtb)")
    ap.add_argument("--image-only", action="store_true",
                    help="reuse shim/out/Image instead of compiling the shim")
    ap.add_argument("--cc"), ap.add_argument("--objcopy"), ap.add_argument("--nm")
    ap.add_argument("--readelf")
    a = ap.parse_args()

    work = SHIM / "out"
    work.mkdir(exist_ok=True)
    if a.image_only:
        image = work / "Image"
        if not image.exists():
            sys.exit(f"{image} missing: build the shim first")
    else:
        cc = find_tool(a.cc, ("aarch64-elf-gcc", "clang"))
        objcopy = find_tool(a.objcopy, ("aarch64-elf-objcopy", "llvm-objcopy"))
        nm = find_tool(a.nm, ("aarch64-elf-nm", "llvm-nm"))
        readelf = find_tool(a.readelf, ("aarch64-elf-readelf", "llvm-readelf",
                                        "llvm-objdump"))
        image = compile_shim(work, cc, objcopy, nm, readelf)

    ramdisk = Path(a.payload) if a.payload else work / "ramdisk-empty.bin"
    if not a.payload:
        ramdisk.write_bytes(empty_cpio())
        print("ramdisk: empty cpio (shim diagnostics, no XNU)")
    dtb = Path(a.dtb) if a.dtb else next((p for p in (ROOT / "dtb" / "davinci.dtb",
                                                      ROOT / "backup" / "davinci.dtb")
                                          if p.exists()), None)
    pack(image, ramdisk, Path(a.out), dtb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
