/* presentloop: present cadence and frame-rate decay over time.
 *
 * usage: presentloop [--seconds N] [--rtv-per-frame] [--draws N] [--fullscreen]
 *                    [--flip|--blt] [--interval 0|1]
 *
 * Clears and presents a window on the VirtIO adapter for N seconds and prints
 * the frame rate of every 5 s window plus the worst present-to-present gap.
 * --rtv-per-frame creates (and releases) a render target view every frame:
 * apps do this, and a driver that never frees released views decays.
 *
 * Verdict: DECAY when the last window is below 70% of the first, HITCH when a
 * single frame took longer than 250 ms.  A decay or a long outlier is a hang
 * in slow motion; treat it as one.  Run in the console session.
 */
#include "common.h"
#include <d3d11.h>
#include <stdlib.h>
#include <string.h>

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_DESTROY)
        PostQuitMessage(0);
    return DefWindowProcA(h, m, w, l);
}

int main(int argc, char **argv)
{
    int seconds = 60, rtv_per_frame = 0, draws = 0, fullscreen = 0, flip = 1, interval = 0;
    WNDCLASSA wc = { 0 };
    DXGI_SWAP_CHAIN_DESC sd = { 0 };
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    ID3D11Device *dev;
    ID3D11DeviceContext *ctx;
    IDXGISwapChain *sc;
    ID3D11Texture2D *bb;
    ID3D11RenderTargetView *rtv = NULL;
    IDXGIAdapter1 *a;
    LARGE_INTEGER f, t0, prev, now, win_start;
    double first = 0, last = 0, worst = 0;
    long frames = 0, win_frames = 0;
    HWND hwnd;
    HRESULT hr;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seconds") && i + 1 < argc) seconds = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--rtv-per-frame")) rtv_per_frame = 1;
        else if (!strcmp(argv[i], "--draws") && i + 1 < argc) draws = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--fullscreen")) fullscreen = 1;
        else if (!strcmp(argv[i], "--blt")) flip = 0;
        else if (!strcmp(argv[i], "--flip")) flip = 1;
        else if (!strcmp(argv[i], "--interval") && i + 1 < argc) interval = atoi(argv[++i]);
    }
    printf("build: %s, %d s, %s, %s%s, sync interval %d\n", probe_build_arch(), seconds,
           flip ? "flip" : "blt", fullscreen ? "fullscreen" : "windowed",
           rtv_per_frame ? ", new RTV per frame" : "", interval);

    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "presentloop";
    RegisterClassA(&wc);
    hwnd = CreateWindowA("presentloop", "presentloop", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                         100, 100, 1280, 720, NULL, NULL, wc.hInstance, NULL);

    a = probe_find_adapter(0);
    if (!a)
        return 2;
    sd.BufferCount = flip ? 2 : 1;
    sd.BufferDesc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.OutputWindow = hwnd;
    sd.SampleDesc.Count = 1;
    sd.Windowed = !fullscreen;
    sd.SwapEffect = flip ? DXGI_SWAP_EFFECT_FLIP_DISCARD : DXGI_SWAP_EFFECT_DISCARD;
    hr = D3D11CreateDeviceAndSwapChain((IDXGIAdapter *)a, D3D_DRIVER_TYPE_UNKNOWN, NULL, 0, &fl, 1,
                                       D3D11_SDK_VERSION, &sd, &sc, &dev, NULL, &ctx);
    if (FAILED(hr)) {
        printf("D3D11CreateDeviceAndSwapChain hr=0x%08lx\n", (unsigned long)hr);
        return 2;
    }
    IDXGISwapChain_GetBuffer(sc, 0, &IID_ID3D11Texture2D, (void **)&bb);
    if (!rtv_per_frame)
        ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)bb, NULL, &rtv);

    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    prev = win_start = t0;
    for (;;) {
        MSG msg;
        float c[4] = { (frames % 256) / 255.0f, 0.2f, 0.4f, 1.0f };
        double gap, el;

        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (rtv_per_frame)
            ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)bb, NULL, &rtv);
        for (int d = 0; d <= draws; d++)
            ID3D11DeviceContext_ClearRenderTargetView(ctx, rtv, c);
        if (rtv_per_frame) {
            ID3D11RenderTargetView_Release(rtv);
            rtv = NULL;
        }
        hr = IDXGISwapChain_Present(sc, interval, 0);
        if (FAILED(hr)) {
            printf("Present hr=0x%08lx (device removed reason 0x%08lx)\n", (unsigned long)hr,
                   (unsigned long)ID3D11Device_GetDeviceRemovedReason(dev));
            return 1;
        }
        frames++;
        win_frames++;
        QueryPerformanceCounter(&now);
        gap = (double)(now.QuadPart - prev.QuadPart) * 1000.0 / f.QuadPart;
        if (gap > worst)
            worst = gap;
        prev = now;
        el = (double)(now.QuadPart - win_start.QuadPart) / f.QuadPart;
        if (el >= 5.0) {
            double fps = win_frames / el;
            if (first == 0)
                first = fps;
            last = fps;
            printf("t=%4.0fs fps=%8.1f worst_gap=%.1fms\n",
                   (double)(now.QuadPart - t0.QuadPart) / f.QuadPart, fps, worst);
            fflush(stdout);
            win_frames = 0;
            win_start = now;
        }
        if ((double)(now.QuadPart - t0.QuadPart) / f.QuadPart >= seconds)
            break;
    }
    if (fullscreen)
        IDXGISwapChain_SetFullscreenState(sc, FALSE, NULL);
    printf("frames=%ld first=%.1f last=%.1f worst_gap=%.1fms\n", frames, first, last, worst);
    if (last < first * 0.7)
        printf("PRESENTLOOP DECAY (last window %.0f%% of the first)\n", 100.0 * last / first);
    else if (worst > 250.0)
        printf("PRESENTLOOP HITCH (a %.0f ms frame)\n", worst);
    else
        printf("PRESENTLOOP STEADY\n");
    return (last < first * 0.7 || worst > 250.0) ? 1 : 0;
}
