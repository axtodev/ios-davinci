#!/usr/bin/env python3
"""Build 0085 patch from tools/gpu-src sources + extract 0084 baseline."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = Path(__file__).resolve().parent
PATCH84 = ROOT / "xnu" / "patches" / "0084-davinci-gpu-probe.patch"
OUT = ROOT / "xnu" / "patches" / "0085-davinci-gpu-power-accel.patch"


def extract_new_file(patch_text: str, path: str) -> str:
    marker = f"+++ b/{path}\n"
    i = patch_text.find(marker)
    if i < 0:
        raise SystemExit(f"missing {path} in 0084")
    i = patch_text.find("\n", i) + 1
    if patch_text[i:].startswith("@@"):
        i = patch_text.find("\n", i) + 1
    lines = []
    for line in patch_text[i:].splitlines():
        if line.startswith("diff ") or (line.startswith("--- ") and lines):
            break
        if line.startswith("+"):
            lines.append(line[1:])
        elif line.startswith("\\"):
            continue
        elif line.startswith("--- ") or line.startswith("+++ "):
            break
    return "\n".join(lines) + ("\n" if lines else "")


def file_hunk_replace(path: str, old: str, new: str) -> str:
    old_l = old.splitlines()
    new_l = new.splitlines()
    # drop trailing empty from split if present
    if old_l and old_l[-1] == "":
        old_l = old_l[:-1]
    if new_l and new_l[-1] == "":
        new_l = new_l[:-1]
    out = [f"--- a/{path}\n", f"+++ b/{path}\n",
           f"@@ -1,{len(old_l)} +1,{len(new_l)} @@\n"]
    for line in old_l:
        out.append("-" + line + "\n")
    for line in new_l:
        out.append("+" + line + "\n")
    return "".join(out)


def file_hunk_new(path: str, content: str) -> str:
    lines = content.splitlines()
    if lines and lines[-1] == "":
        lines = lines[:-1]
    out = [f"--- /dev/null\n", f"+++ b/{path}\n",
           f"@@ -0,0 +1,{len(lines)} @@\n"]
    for line in lines:
        out.append("+" + line + "\n")
    return "".join(out)


old_gpu = extract_new_file(PATCH84.read_text(encoding="utf-8"), "bsd/dev/sweet_gpu.c")
new_gpu = (SRC / "sweet_gpu.c").read_text(encoding="utf-8")
accel = (SRC / "SweetAccelerator.cpp").read_text(encoding="utf-8")

header = """--- a/iokit/conf/files.arm64
+++ b/iokit/conf/files.arm64
@@ -16,6 +16,7 @@
 iokit/Kernel/arm/SweetSurface.cpp                                optional iokitcpp
 iokit/Kernel/arm/SweetFramebuffer.cpp                            optional iokitcpp
 iokit/Kernel/arm/SweetBattery.cpp                                optional iokitcpp
+iokit/Kernel/arm/SweetAccelerator.cpp                            optional iokitcpp
 iokit/Kernel/arm/hid/SweetHID.cpp                                optional iokitcpp
 iokit/Kernel/arm/hid/IOHIDEvent.cpp                              optional iokitcpp
 iokit/Kernel/arm/hid/IOHIDEventServiceQueue.cpp                  optional iokitcpp
--- a/iokit/KernelConfigTables.cpp
+++ b/iokit/KernelConfigTables.cpp
@@ -82,6 +82,11 @@
     "     'IOProviderClass' = IOResources;"
     "     'IOMatchCategory' = SweetBattery;"
     "   },"
+    "   {"
+    "     'IOClass'         = AGXAccelerator;"
+    "     'IOProviderClass' = IOPlatformDevice;"
+    "     'IONameMatch'     = 'sweet,gpu';"
+    "   },"
 #endif
     "   {"
     "     'IOClass'         = IOPanicPlatform;"
"""

OUT.write_text(
    header
    + file_hunk_replace("bsd/dev/sweet_gpu.c", old_gpu, new_gpu)
    + file_hunk_new("iokit/Kernel/arm/SweetAccelerator.cpp", accel),
    encoding="utf-8",
    newline="\n",
)
print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
