# Adreno 618 GPU on davinci (Mi 9T)

Goal: real GPU acceleration for unmodified iOS 16.7 userland on
`davinci` (SDM730 / SM7150-class SoC, Adreno 618). Same GPU block as
`sweet` (732G). Display path (`AppleCLCD` / IOSurface) stays separate;
this work only owns the accelerator.

Today CoreAnimation runs `CA::OGL::SW` (CPU). backboardd already polls
`IOProviderClass IOAcceleratorES`, `AGXAccelerator` and
`IOAcceleratorESLegacy` and finds nothing. Filling that gap with a driver
that speaks enough of Apple's IOAccelerator / Metal ABI, and drives the
Adreno through a freedreno-style backend, is the path to "iOS runs well".

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

Firmware (later phases, keep out of git until needed): GMU fw + ZAP
shader (`a615_zap`-class for a618). Source them from the device's
Android vendor partition / linux-firmware, never commit blobs.

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
firmware path exists (forcing GX without GMU is a hang risk). Sources of
truth for the patch live in `tools/gpu-src/` (`gen_0085_patch.py`).

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

Shaders, compute, apps. Long tail; only after Phase 4 is stable.

## Non-goals (for now)

- MDP/DPU-only composition as a substitute for Metal (useful later as a
  present helper, not the main plan).
- Shipping GPU firmware in this repo.
- Touching display bring-up or replacing `AppleCLCD`.

## References

- Linux: `arch/arm64/boot/dts/qcom/sm7150.dtsi` (`gpu@5000000`, `gmu@506a000`, `gpucc@5090000`)
- Clocks: `drivers/clk/qcom/gcc-sm7150.c`, `gpucc-sc7180.c`
- Userspace/kernel GPU: Mesa freedreno / msm `a6xx_*`, `adreno_gmu*`
- Project notes: IOSurface / IOMFB reverse already done in `NOTES.md`
  (patches 0046+); copy that RE workflow for IOAccelerator.
