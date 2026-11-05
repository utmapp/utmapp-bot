/* Shared helpers for the D3D probes: pick the VirtIO GPU adapter explicitly.
 *
 * D3D11CreateDevice(NULL, D3D_DRIVER_TYPE_HARDWARE) can succeed on the
 * Microsoft Basic Render Driver (WARP, vendor 0x1414): S_OK, a working device,
 * and not one call reaching the driver under test.  Every probe therefore
 * enumerates DXGI adapters and uses the VirtIO one (vendor 0x1af4) with
 * D3D_DRIVER_TYPE_UNKNOWN, or fails loudly.
 *
 * Under Wine (a Linux guest) there is no KMD: Neptune's own DXGI reports one
 * adapter named after the host GPU, with vendor 0.  There the first hardware
 * adapter that is not Microsoft's is used.
 */
#pragma once
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_4.h>
#include <stdio.h>

#define VIRTIO_VENDOR 0x1af4

static IDXGIAdapter1 *probe_find_adapter(int verbose)
{
    IDXGIFactory1 *f = NULL;
    IDXGIAdapter1 *a = NULL, *found = NULL, *wine_pick = NULL;
    /* CrossOver's ntdll hides wine_get_version; every Wine has __wine_dbg_output */
    HMODULE nt = GetModuleHandleA("ntdll.dll");
    int wine = GetProcAddress(nt, "wine_get_version") != NULL ||
               GetProcAddress(nt, "__wine_dbg_output") != NULL;
    if (FAILED(CreateDXGIFactory1(&IID_IDXGIFactory1, (void **)&f))) {
        printf("CreateDXGIFactory1 failed\n");
        return NULL;
    }
    for (UINT i = 0; IDXGIFactory1_EnumAdapters1(f, i, &a) != DXGI_ERROR_NOT_FOUND; i++) {
        DXGI_ADAPTER_DESC1 d;
        IDXGIAdapter1_GetDesc1(a, &d);
        if (verbose)
            printf("adapter %u: %ls vendor=0x%04x device=0x%04x dedicated=%zu MB shared=%zu MB%s\n",
                   i, d.Description, d.VendorId, d.DeviceId, d.DedicatedVideoMemory >> 20,
                   d.SharedSystemMemory >> 20,
                   (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? " (software)" : "");
        if (d.VendorId == VIRTIO_VENDOR && !found) {
            found = a;
        } else if (wine && !wine_pick && d.VendorId != 0x1414 &&
                   !(d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) {
            wine_pick = a;
        } else {
            IDXGIAdapter1_Release(a);
        }
    }
    IDXGIFactory1_Release(f);
    if (!found && wine_pick) {
        DXGI_ADAPTER_DESC1 d;
        IDXGIAdapter1_GetDesc1(wine_pick, &d);
        printf("wine: no VirtIO adapter (no KMD under Wine); using the first hardware "
               "adapter: %ls\n", d.Description);
        found = wine_pick;
    } else if (wine_pick) {
        IDXGIAdapter1_Release(wine_pick);
    }
    if (!found)
        printf("no VirtIO GPU adapter (vendor 0x%04x): the driver is not loaded, or this is "
               "running outside the console session\n", VIRTIO_VENDOR);
    return found;
}

/* An x64 or x86 build runs emulated on Windows on Arm; it cannot load a
 * native arm64 driver DLL, so its failures say nothing about the driver. */
static const char *probe_build_arch(void)
{
#if defined(_M_ARM64EC)
    return "arm64ec";
#elif defined(_M_ARM64)
    return "arm64 (native)";
#elif defined(_M_X64)
    return "x64 (emulated on an Arm host)";
#else
    return "x86 (emulated on an Arm host)";
#endif
}
