# Adreno 618 GPU on davinci (Mi 9T)

Goal: real GPU acceleration for unmodified iOS 16.7 userland on
`davinci` (SDM730 / SM7150-class SoC, Adreno 618). Same GPU block as
`sweet` (732G). Display path (`AppleCLCD` / IOSurface) stays separate;
this work only owns the accelerator.

Today CoreAnimation runs `CA::OGL::SW` (CPU). backboardd already polls
`IOProviderClass IOAcceleratorES`, `AGXAccelerator` and
`IOAcceleratorESLegacy` and finds nothing.

## Where this stands (2026-10-29)

Two independent walls. Only one of them can be moved with kernel code:

1. **Shaders are already machine code.** iOS ships precompiled AGX ISA
   (`default.metallib` in QuartzCore / Metal / every framework, plus the
   per-app `metallib`s). There is no shader source and no compiler we can
   point at a6xx, so choosing `CA::OGL::MetalContext` would require
   *translating* AGX machine code into a6xx machine code. A complete
   IOAccelerator / Metal ABI implementation alone does not get there:
   whatever we submit would still have to run AGX binaries. Treat this as
   the long-term research track, not the next milestone.
2. **The present path is the CPU.** Everything CoreAnimation draws is
   copied into the panel by the CPU: `AppleCLCD::present()` does a
   ~10 MB `memcpy` per frame into a write-combined mapping of the ABL
   framebuffer (patch 0046), throttled to `sweetfb_hz` ticks. This is
   where frames are lost today, and it needs no firmware, no TZ and no
   shader translation.

So the work splits:

- **Track A, present path** (patch 0086). Raise the panel copy out of the
  vsync timer, measure it, and offer a cached mapping. See below; this is
  the active work.
- **Track B, Adreno driver** (patches 0084/0085 and the phases below).
  Power, GMU, ring, submit: the groundwork for Track A's successor (GPU
  composition of the same IOSurfaces) and for compute offload, not a way
  to make existing apps render on the GPU.

## Track A — present path (patch 0086)

Before 0086 the panel copy ran *inside* `vsyncFired()` on the same work
loop that delivers vsync notifications, every `60/hz` ticks (default
`sweetfb_hz=10`). Two consequences: at 10 Hz the GUI visibly stepped, and
raising the rate moved the copy onto the CA pacing path.

0086 changes:

- second `IOWorkLoop` + `IOTimerEventSource` (`fPresentLoop` /
  `fPresentTimer`): vsync notifications keep their own timer and stay on
  time whatever a copy costs.
- `sweetfb_hz` now means the present period directly (`1000000/hz` us,
  default **60**); the period adapts up to 3x the measured copy time,
  capped at 50 ms, so a slow mapping costs update rate, not CPU.
- every 120 presents: `SWEET FB: present N <us> <KB> <MB/s> every <us>` -
  the copy cost is now measured, not guessed.
- `sweetfb_cache=1` maps the panel through `kIOMapCopybackCache` and
  flushes the written lines with `dc cvac` + `dsb sy` (the display
  controller is not cache coherent). Default stays write-combined; the
  log above decides which one wins on real hardware.
- one big `memcpy` when the layer row and the panel stride match, and the
  copied byte count is reported instead of being implied.
- `SwapWait` logs its first 8 calls (caller name, scalar inputs, presented
  count) so CA pacing can be understood from the kernel log alone.

Read the result like this: copy <= 5 ms means `sweetfb_hz=60` is honest;
copy of 10-20 ms means write-combined stores are the limit and
`sweetfb_cache=1` is the fix; a copy that never shrinks means the layer
rect (the `w`,`h` in the swap record) is full-screen every time and dirty
rects have to come from CA, not from us.

## Hardware map (SM7150 / Linux sm7150.dtsi)

| Block | PA | Size | Notes |
| --- | --- | --- | --- |
| GPU (kgsl_3d0) | `0x05000000` | `0x40000` | a6xx RBBM / CP / …; `REG_A6XX_RBBM_CHIP_ID` @ `0x0` |
| CX_DBGC | `0x05061000` | `0x800` | debug |
| CX_MEM | `0x0509e000` | `0x1000` | |
| Adreno SMMU | `0x05040000` | `0x10000` | SMMUv2; stream 0 = GPU, 5 = GMU |
| GMU | `0x0506a000` | `0x31000` | a618 always has a GMU |
| GMU PDC | `0x0b290000` / `0x0b490000` | `0x10000` each | power sequencer |
| GPUCC | `0x05090000` | `0x9000` | sc7180-compatible; CX_GDSC `0x106c`, GX_GDSC `0x100c` |
| IRQ | GIC SPI 300 | | GPU; GMU HFI/GMU = 304 / 305 |

GCC clocks used by the GPU path (offsets from `gcc-sm7150.c`):

- `GCC_GPU_CFG_AHB` CBCR `0x71004`
- `GCC_GPU_MEMNOC_GFX` CBCR `0x7100c`
- `GCC_GPU_SNOC_DVM_GFX` CBCR `0x71014`
- `GCC_DDRSS_GPU_AXI` CBCR `0x4452c`

GPUCC clocks (offsets from `gpucc-sc7180.c`): CXO_AON `0x1004`, CRC_AHB
`0x107c`, CX_SNOC_DVM `0x108c`, CX_GMU `0x1098`, CXO `0x109c`, GMU RCG
`0x1120`.

Power: CX_GDSC + GX_GDSC. On a618 the GMU owns GX bring-up in the
normal Linux path; a "force legacy / no GMU" path is a research option
for early bring-up only.

Firmware: GMU firmware + SQE microcode + ZAP shader. The Mi 9T still has
its Android vendor partition, which is the only licensed copy we may use;
pull it with `python tools/gpu-fw.py` (adb, or `--local` for files already
pulled). It lands in `fw/` (gitignored) with `fw/manifest.txt` holding
size + sha256, and it prints which file is what. Blobs never enter git.
The GMU firmware is loaded by the CPU through the GMU's own AHB window,
so it is reachable; the ZAP shader is TZ-signed and is what
`RBBM_SECVID_TRUST_CNTL` (`0x05000000 + 0xf000`) has to work around.

## Stack (target)

```text
  QuartzCore / Metal / IOGPUFamily   (unmodified iOS)
            |
            v
  IOKit: AGXAccelerator : IOAcceleratorES   (our stub -> real UC)
            |
            v
  sweet_gpu  (kernel): power/clocks, GMU or legacy, ring, submit, fences
            |
            v
  Adreno 618 MMIO + (later) SMMU stream mapping of IOSurfaces
            |
            v
  AppleCLCD still presents the composed IOSurface to the panel
```

Do not patch Metal.framework or replace the dyld cache. All work is
kernel + ADT + (maybe) root tweaks for shader-cache paths already used
by backboardd.

## Phases

Status 2026-10-29: 0084, 0085 and 0086 are written but **have never been
compiled** - the GitHub Actions account is locked for billing, so every
run fails before the job starts (see `NOTES.md`). Nothing below has run on
the phone yet; treat the log formats as intent, not as observed output.

### Boot-arg `sweetgpu`

| Value | Effect |
| --- | --- |
| `0` | Off |
| `1` | Read-only probe (default). Never enables rails |
| `2` | Power CX GDSC + GCC/GPUCC clocks, read `RBBM_CHIP_ID` |
| `3` | Also register IOKit `AGXAccelerator` stub (may break SW GUI) |

### Phase 0 — probe (patch 0084)

Read-only, same style as USB 0072. Default `sweetgpu=1`.

1. Map GCC, print GPU-related CBCR on/off.
2. Map GPUCC, print CX/GX GDSC.
3. If CX looks powered and the cfg/memnoc clocks are on, map the GPU and
   print `RBBM_CHIP_ID`. Never touch a gated block (hang risk).

Success: log lines `SWEET GPU: ...` on a davinci boot. Expect "off"
after fastboot; that still validates the probe.

### Phase 1 — power on + chip id (patch 0085, `sweetgpu=2`)

Enable CX GDSC + CBCRs, read a stable chip id. GX stays off until a GMU
firmware path exists. Sources of truth for the patch live in
`tools/gpu-src/` (`gen_0085_patch.py`).

Two corrections from the sc7180 driver (already in the patch text, worth
knowing when debugging it):

- GX GDSC is **not** CPU-controllable on a6xx: `gpucc-sc7180.c` registers
  it with `gdsc_gx_do_nothing_enable` because only the GMU may power the
  core domain. Forcing GX from the CPU is a hang, not a shortcut.
- The `CX_GMU` branch (`0x1098`) is a CBCR with sleep/wake fields: bits
  4-11 must carry `0xF` (both sides) before the enable bit sticks. Writing
  the enable alone reads back as off.

### Phase 2 — IOKit stub + selector trace (patch 0085, `sweetgpu=3`)

ADT `/arm-io/gpu` (`compatible = sweet,gpu`). `AGXAccelerator` :
`IOAcceleratorES` logs every user-client selector once. **Do not use
`sweetgpu=3` on a daily image** until Metal ABI answers are real — a
stub can make CoreAnimation prefer Metal and break the working SW path.

### Phase 3 — submit path (freedreno-shaped)

Ringbuffer, IB, WAIT_FOR_IDLE, simple clear or blit into an IOSurface
backing store. CPU still owns composition; this proves the GPU executes
our packets and memory is coherent enough for CA.

### Phase 4 — compositor Metal

Enough of the IOAccelerator / Metal user-client ABI that
`CA::OGL::MetalContext` (or the GLES path if smaller) is selected and
produces frames. Measure fps vs today's ~20–30 CPU path.

### Phase 5 — broaden

Shaders, compute, apps. Long tail; only after Phase 4 is stable, and it
inherits the AGX-ISA problem described at the top of this file.

## Non-goals (for now)

- MDP/DPU-only composition as a substitute for Metal (useful later as a
  present helper, not the main plan).
- Shipping GPU firmware in this repo (`fw/` is gitignored).
- Changing display bring-up. Track A edits the present path inside
  `AppleCLCD`, but the panel/ABL framebuffer setup and the 1125x2436
  presentation stay as they are (patches 0046-0079).

## References

- Linux: `arch/arm64/boot/dts/qcom/sm7150.dtsi` (`gpu@5000000`, `gmu@506a000`, `gpucc@5090000`)
- Clocks: `drivers/clk/qcom/gcc-sm7150.c`, `gpucc-sc7180.c`
- Userspace/kernel GPU: Mesa freedreno / msm `a6xx_*`, `adreno_gmu*`
- Project notes: IOSurface / IOMFB reverse already done in `NOTES.md`
  (patches 0046+); copy that RE workflow for IOAccelerator.
