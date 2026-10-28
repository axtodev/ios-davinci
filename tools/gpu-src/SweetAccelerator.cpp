/*
 * DAVINCI / SWEET: IOKit GPU stub for Metal / CoreAnimation discovery.
 *
 * Publishes AGXAccelerator : IOAcceleratorES on /arm-io/gpu (compatible
 * sweet,gpu) when boot-arg sweetgpu>=3. Every user-client method is logged
 * once and returns success with zeroed outputs so we can reverse the ABI
 * the same way IOSurface/IOMFB were reversed. This is NOT a working Metal
 * driver yet: real submits need docs/GPU.md phases 3-4.
 */
#if defined(ARM64_BOARD_CONFIG_SWEET)
#include <IOKit/IOService.h>
#include <IOKit/IOUserClient.h>
#include <IOKit/IOKitKeys.h>
#include <IOKit/IOLib.h>
#include <IOKit/IOMemoryDescriptor.h>
#include <sys/proc.h>
#include <pexpert/pexpert.h>

extern "C" {
int sweet_gpu_mode(void);
bool sweet_gpu_is_on(void);
uint32_t sweet_gpu_chip_id(void);
}

class IOAcceleratorES : public IOService {
	OSDeclareDefaultStructors(IOAcceleratorES);
public:
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(IOAcceleratorES, IOService);

bool
IOAcceleratorES::start(IOService *provider)
{
	return IOService::start(provider);
}

class AGXAccelerator : public IOAcceleratorES {
	OSDeclareDefaultStructors(AGXAccelerator);
public:
	bool start(IOService *provider) APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(AGXAccelerator, IOAcceleratorES);

class AGXAcceleratorUserClient : public IOUserClient {
	OSDeclareDefaultStructors(AGXAcceleratorUserClient);
public:
	IOReturn clientClose(void) APPLE_KEXT_OVERRIDE;
	IOReturn externalMethod(uint32_t selector, IOExternalMethodArguments *args,
	    IOExternalMethodDispatch *dispatch, OSObject *target,
	    void *reference) APPLE_KEXT_OVERRIDE;
};

OSDefineMetaClassAndStructors(AGXAcceleratorUserClient, IOUserClient);

bool
AGXAccelerator::start(IOService *provider)
{
	if (sweet_gpu_mode() < 3) {
		return false;
	}
	if (!IOAcceleratorES::start(provider)) {
		return false;
	}
	setProperty(kIOUserClientClassKey, "AGXAcceleratorUserClient");
	setProperty("AGXAcceleratorFamily", "sweet-adreno618");
	setProperty("SweetGPUChipID", (uint64_t)sweet_gpu_chip_id(), 64);
	setProperty("SweetGPUPowered", sweet_gpu_is_on());
	/* Match names backboardd already polls. */
	setName("AGXAccelerator");
	registerService();
	IOLog("SWEET GPU: AGXAccelerator registered (stub; chip=0x%x on=%d) - "
	    "Metal ABI incomplete, software compositor may break\n",
	    sweet_gpu_chip_id(), sweet_gpu_is_on() ? 1 : 0);
	return true;
}

IOReturn
AGXAcceleratorUserClient::clientClose(void)
{
	terminate();
	return kIOReturnSuccess;
}

static uint64_t sweet_gpu_sel_logged[4]; /* 256 selectors */

IOReturn
AGXAcceleratorUserClient::externalMethod(uint32_t selector,
    IOExternalMethodArguments *args,
    __unused IOExternalMethodDispatch *dispatch,
    __unused OSObject *target, __unused void *reference)
{
	for (uint32_t i = 0; i < args->scalarOutputCount; i++) {
		args->scalarOutput[i] = 0;
	}
	if (args->structureOutputDescriptor != NULL) {
		IOByteCount len = args->structureOutputDescriptor->getLength();
		for (IOByteCount off = 0; off < len; ) {
			static const uint64_t zero = 0;
			IOByteCount n = len - off < 8 ? len - off : 8;
			args->structureOutputDescriptor->writeBytes(off, &zero, n);
			off += n;
		}
	} else if (args->structureOutput != NULL) {
		bzero(args->structureOutput, args->structureOutputSize);
	}

	if (selector < 256) {
		uint64_t bit = 1ULL << (selector & 63);
		uint32_t word = selector >> 6;
		if (!(sweet_gpu_sel_logged[word] & bit)) {
			sweet_gpu_sel_logged[word] |= bit;
			char name[32];
			proc_selfname(name, sizeof(name));
			IOLog("SWEET GPU: %s sel %u in %u/%u out %u/%u\n", name, selector,
			    args->scalarInputCount, args->structureInputSize,
			    args->scalarOutputCount, args->structureOutputSize);
		}
	}
	return kIOReturnSuccess;
}
#endif /* ARM64_BOARD_CONFIG_SWEET */
