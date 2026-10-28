/*
 * DAVINCI / SWEET: Adreno 618 bring-up.
 *
 * Boot-arg sweetgpu:
 *   0 = off
 *   1 = read-only probe (default) - never enables rails
 *   2 = enable CX GDSC + GCC/GPUCC clocks, read RBBM_CHIP_ID
 *   3 = also allow IOKit AGXAccelerator stub (SweetAccelerator.cpp)
 *
 * sweetgpu=3 can make Metal prefer a stub that is not yet a real driver;
 * keep the working software compositor unless you are tracing selectors.
 * See docs/GPU.md.
 */
#if defined(ARM64_BOARD_CONFIG_SWEET)
#include <sys/param.h>
#include <sys/systm.h>
#include <pexpert/pexpert.h>
#include <kern/clock.h>

extern vm_offset_t ml_io_map(vm_offset_t, vm_size_t);
void sweet_gpu_probe(void);
int sweet_gpu_mode(void);
bool sweet_gpu_is_on(void);
uint32_t sweet_gpu_chip_id(void);

#define GCC_PA                  0x00100000u
#define GCC_SIZE                0x00090000u
#define CBCR_CLK_OFF            (1u << 31)
#define CBCR_ENABLE             (1u << 0)

#define GCC_DDRSS_GPU_AXI       0x4452cu
#define GCC_GPU_CFG_AHB         0x71004u
#define GCC_GPU_MEMNOC_GFX      0x7100cu
#define GCC_GPU_SNOC_DVM_GFX    0x71014u

#define GPUCC_PA                0x05090000u
#define GPUCC_SIZE              0x00009000u
#define GPUCC_GX_GDSCR          0x100cu
#define GPUCC_CX_GDSCR          0x106cu
#define GPUCC_CXO_AON_CBCR      0x1004u
#define GPUCC_CRC_AHB_CBCR      0x107cu
#define GPUCC_CX_SNOC_DVM_CBCR  0x108cu
#define GPUCC_CX_GMU_CBCR       0x1098u
#define GPUCC_CXO_CBCR          0x109cu
#define GDSC_SW_COLLAPSE        (1u << 0)
#define GDSC_PWR_ON             (1u << 31)

#define GPU_PA                  0x05000000u
#define GPU_SIZE                0x00040000u
#define REG_A6XX_RBBM_CHIP_ID   0x0000u
#define REG_A6XX_RBBM_STATUS    0x0210u

#define GMU_PA                  0x0506a000u
#define GMU_SIZE                0x00001000u

static const struct {
	const char *name;
	uint32_t    off;
} gpu_gcc_cbcr[] = {
	{ "cfg-ahb",     GCC_GPU_CFG_AHB },
	{ "memnoc-gfx",  GCC_GPU_MEMNOC_GFX },
	{ "snoc-dvm",    GCC_GPU_SNOC_DVM_GFX },
	{ "ddrss-axi",   GCC_DDRSS_GPU_AXI },
};

static const struct {
	const char *name;
	uint32_t    off;
} gpu_cc_cbcr[] = {
	{ "cxo-aon",    GPUCC_CXO_AON_CBCR },
	{ "crc-ahb",    GPUCC_CRC_AHB_CBCR },
	{ "cx-snoc",    GPUCC_CX_SNOC_DVM_CBCR },
	{ "cx-gmu",     GPUCC_CX_GMU_CBCR },
	{ "cxo",        GPUCC_CXO_CBCR },
};

static int g_mode = 1;
static bool g_on = false;
static uint32_t g_chip_id = 0;
static vm_offset_t g_gcc, g_gpucc, g_gpu;

static uint32_t
gpu_rd(vm_offset_t base, uint32_t off)
{
	uint32_t v = *(volatile uint32_t *)(base + off);
	__asm__ volatile ("dmb oshld" ::: "memory");
	return v;
}

static void
gpu_wr(vm_offset_t base, uint32_t off, uint32_t v)
{
	__asm__ volatile ("dmb oshst" ::: "memory");
	*(volatile uint32_t *)(base + off) = v;
	__asm__ volatile ("dmb osh" ::: "memory");
}

int
sweet_gpu_mode(void)
{
	return g_mode;
}

bool
sweet_gpu_is_on(void)
{
	return g_on;
}

uint32_t
sweet_gpu_chip_id(void)
{
	return g_chip_id;
}

static void
gpu_dump_state(const char *tag)
{
	uint32_t cx = gpu_rd(g_gpucc, GPUCC_CX_GDSCR);
	uint32_t gx = gpu_rd(g_gpucc, GPUCC_GX_GDSCR);
	printf("SWEET GPU: %s GDSC cx 0x%08x (%s) gx 0x%08x (%s)\n", tag,
	    cx, (cx & GDSC_PWR_ON) ? "on" : "off",
	    gx, (gx & GDSC_PWR_ON) ? "on" : "off");
	for (unsigned i = 0; i < sizeof(gpu_gcc_cbcr) / sizeof(gpu_gcc_cbcr[0]); i++) {
		uint32_t v = gpu_rd(g_gcc, gpu_gcc_cbcr[i].off);
		printf("SWEET GPU: %s gcc %-10s CBCR 0x%08x (%s)\n", tag,
		    gpu_gcc_cbcr[i].name, v,
		    (v & CBCR_CLK_OFF) ? "off" : "on");
	}
	for (unsigned i = 0; i < sizeof(gpu_cc_cbcr) / sizeof(gpu_cc_cbcr[0]); i++) {
		uint32_t v = gpu_rd(g_gpucc, gpu_cc_cbcr[i].off);
		printf("SWEET GPU: %s gpucc %-8s CBCR 0x%08x (%s)\n", tag,
		    gpu_cc_cbcr[i].name, v,
		    (v & CBCR_CLK_OFF) ? "off" : "on");
	}
}

static bool
gpu_cbcr_enable(vm_offset_t base, uint32_t off, const char *name)
{
	uint32_t v = gpu_rd(base, off);
	gpu_wr(base, off, v | CBCR_ENABLE);
	for (int i = 0; i < 1000; i++) {
		v = gpu_rd(base, off);
		if (!(v & CBCR_CLK_OFF)) {
			return true;
		}
		delay(10); /* 10 us */
	}
	printf("SWEET GPU: CBCR %s stuck off (0x%08x)\n", name, v);
	return false;
}

static bool
gpu_cx_enable(void)
{
	uint32_t v = gpu_rd(g_gpucc, GPUCC_CX_GDSCR);
	if (v & GDSC_PWR_ON) {
		return true;
	}
	gpu_wr(g_gpucc, GPUCC_CX_GDSCR, v & ~GDSC_SW_COLLAPSE);
	for (int i = 0; i < 1000; i++) {
		v = gpu_rd(g_gpucc, GPUCC_CX_GDSCR);
		if (v & GDSC_PWR_ON) {
			return true;
		}
		delay(10);
	}
	printf("SWEET GPU: CX_GDSC did not power on (0x%08x)\n", v);
	return false;
}

static bool
gpu_power_on(void)
{
	printf("SWEET GPU: power-on CX + clocks (sweetgpu=%d)\n", g_mode);
	/*
	 * Order follows Linux msm/a6xx + gpucc: iface clocks, then CX GDSC,
	 * then GMU/CXO branches. GX stays off until a GMU path exists.
	 */
	if (!gpu_cbcr_enable(g_gcc, GCC_GPU_CFG_AHB, "cfg-ahb") ||
	    !gpu_cbcr_enable(g_gcc, GCC_GPU_MEMNOC_GFX, "memnoc-gfx") ||
	    !gpu_cbcr_enable(g_gcc, GCC_GPU_SNOC_DVM_GFX, "snoc-dvm") ||
	    !gpu_cbcr_enable(g_gcc, GCC_DDRSS_GPU_AXI, "ddrss-axi")) {
		return false;
	}
	if (!gpu_cbcr_enable(g_gpucc, GPUCC_CXO_AON_CBCR, "cxo-aon") ||
	    !gpu_cbcr_enable(g_gpucc, GPUCC_CRC_AHB_CBCR, "crc-ahb") ||
	    !gpu_cbcr_enable(g_gpucc, GPUCC_CX_SNOC_DVM_CBCR, "cx-snoc")) {
		return false;
	}
	if (!gpu_cx_enable()) {
		return false;
	}
	if (!gpu_cbcr_enable(g_gpucc, GPUCC_CX_GMU_CBCR, "cx-gmu") ||
	    !gpu_cbcr_enable(g_gpucc, GPUCC_CXO_CBCR, "cxo")) {
		return false;
	}
	return true;
}

static void
gpu_read_chip(void)
{
	g_gpu = ml_io_map(GPU_PA, GPU_SIZE);
	if (g_gpu == 0) {
		printf("SWEET GPU: GPU map failed\n");
		return;
	}
	g_chip_id = gpu_rd(g_gpu, REG_A6XX_RBBM_CHIP_ID);
	printf("SWEET GPU: RBBM_CHIP_ID 0x%08x RBBM_STATUS 0x%08x\n",
	    g_chip_id, gpu_rd(g_gpu, REG_A6XX_RBBM_STATUS));
	if (ml_io_map(GMU_PA, GMU_SIZE) == 0) {
		printf("SWEET GPU: GMU map failed\n");
	} else {
		printf("SWEET GPU: GMU aperture mapped (fw bring-up still TODO)\n");
	}
	g_on = (g_chip_id != 0 && g_chip_id != 0xffffffffu);
}

void
sweet_gpu_probe(void)
{
	g_mode = 1;
	PE_parse_boot_argn("sweetgpu", &g_mode, sizeof(g_mode));
	if (g_mode <= 0) {
		return;
	}

	g_gcc = ml_io_map(GCC_PA, GCC_SIZE);
	if (g_gcc == 0) {
		printf("SWEET GPU: GCC map failed\n");
		return;
	}
	g_gpucc = ml_io_map(GPUCC_PA, GPUCC_SIZE);
	if (g_gpucc == 0) {
		printf("SWEET GPU: GPUCC map failed\n");
		return;
	}

	printf("SWEET GPU: Adreno 618 (davinci/sweet) mode %d\n", g_mode);
	gpu_dump_state("pre");

	if (g_mode >= 2) {
		if (!gpu_power_on()) {
			printf("SWEET GPU: power-on FAILED; leaving rails as-is\n");
			gpu_dump_state("fail");
			return;
		}
		gpu_dump_state("post");
		gpu_read_chip();
		if (g_on) {
			printf("SWEET GPU: CX path UP chip_id=0x%08x "
			    "(GX/GMU/submit still TODO; Metal needs sweetgpu=3 stub + ABI)\n",
			    g_chip_id);
		}
		return;
	}

	/* mode 1: read-only, same hang-safety as USB probe */
	{
		uint32_t cx = gpu_rd(g_gpucc, GPUCC_CX_GDSCR);
		uint32_t cfg = gpu_rd(g_gcc, GCC_GPU_CFG_AHB);
		uint32_t mem = gpu_rd(g_gcc, GCC_GPU_MEMNOC_GFX);
		bool cx_on = (cx & GDSC_PWR_ON) != 0;
		bool cfg_on = !(cfg & CBCR_CLK_OFF);
		bool memnoc_on = !(mem & CBCR_CLK_OFF);
		if (!cx_on || !cfg_on || !memnoc_on) {
			printf("SWEET GPU: CX/clks gated; GPU MMIO not read "
			    "(use sweetgpu=2 to power on)\n");
			return;
		}
		gpu_read_chip();
	}
}
#endif /* ARM64_BOARD_CONFIG_SWEET */
