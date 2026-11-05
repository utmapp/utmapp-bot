/* adaptercheck: does D3D reach the VirtIO GPU driver?
 *
 * Lists every DXGI adapter, then creates a D3D11 device on the VirtIO adapter
 * at each feature level, and a D3D12 device when d3d12.dll is present.
 * Exit 0 when D3D11 FL 11_0 or higher creates on the VirtIO adapter.
 * Run it in the console session (utmbot test), never over SSH.
 */
#include "common.h"
#include <d3d11.h>
#include <d3d12.h>

typedef HRESULT (WINAPI *PFN_D3D12CREATE)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

int main(void)
{
    static const D3D_FEATURE_LEVEL levels[] = {
        D3D_FEATURE_LEVEL_12_1, D3D_FEATURE_LEVEL_12_0, D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0,
    };
    static const char *names[] = { "12_1", "12_0", "11_1", "11_0", "10_1", "10_0" };
    int ok11 = 0;
    IDXGIAdapter1 *a;

    printf("build: %s\n", probe_build_arch());
    a = probe_find_adapter(1);
    if (!a) {
        printf("ADAPTERCHECK FAIL\n");
        return 1;
    }
    for (int i = 0; i < 6; i++) {
        ID3D11Device *dev = NULL;
        D3D_FEATURE_LEVEL got = 0;
        HRESULT hr = D3D11CreateDevice((IDXGIAdapter *)a, D3D_DRIVER_TYPE_UNKNOWN, NULL, 0,
                                       &levels[i], 1, D3D11_SDK_VERSION, &dev, &got, NULL);
        printf("D3D11 FL %s hr=0x%08lx\n", names[i], (unsigned long)hr);
        if (SUCCEEDED(hr)) {
            if (levels[i] >= D3D_FEATURE_LEVEL_11_0)
                ok11 = 1;
            ID3D11Device_Release(dev);
        }
    }
    {
        HMODULE m = LoadLibraryA("d3d12.dll");
        PFN_D3D12CREATE create = m ? (PFN_D3D12CREATE)GetProcAddress(m, "D3D12CreateDevice") : NULL;
        if (create) {
            for (int i = 0; i < 4; i++) {
                ID3D12Device *dev = NULL;
                HRESULT hr = create((IUnknown *)a, levels[i], &IID_ID3D12Device, (void **)&dev);
                printf("D3D12 FL %s hr=0x%08lx\n", names[i], (unsigned long)hr);
                if (SUCCEEDED(hr))
                    ID3D12Device_Release(dev);
            }
        }
    }
    IDXGIAdapter1_Release(a);
    printf("ADAPTERCHECK %s\n", ok11 ? "PASS" : "FAIL");
    return ok11 ? 0 : 1;
}
