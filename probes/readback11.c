/* readback11: pixel-exact D3D11 checks, one per GPU access path.
 *
 * Each path renders or writes a known value into a small texture, copies it
 * into a STAGING texture that was first filled with a sentinel, maps it and
 * compares.  No window, swap chain or display is involved, so a FAIL is the
 * driver/host pipeline and not presentation.  The sentinel tells "the GPU
 * wrote the wrong value" apart from "the copy or the write never happened".
 *
 * The paths map to different mechanisms on the host and can fail
 * independently:
 *   clear        ClearRenderTargetView
 *   upload       UpdateSubresource (CPU -> GPU texture)
 *   draw         vertex buffer + constant buffer + pixel shader
 *   dynamic      Map(WRITE_DISCARD) a dynamic constant buffer twice per frame
 *   compute      compute shader writing a UAV
 *
 * To make a repro: copy this file, keep main()/check(), and replace one
 * path's body with the smallest sequence that shows the bug.  Exit 0 = all
 * paths PASS.  Run in the console session: utmbot test readback11.exe
 */
#include "common.h"
#include <d3d11.h>
#include <d3dcompiler.h>
#include <string.h>

#define W 64
#define H 64
#define SENTINEL 0xDEADBEEFu

typedef HRESULT (WINAPI *PFN_COMPILE)(const void *, SIZE_T, const char *, const void *, void *,
                                      const char *, const char *, UINT, UINT, ID3DBlob **,
                                      ID3DBlob **);

static ID3D11Device *dev;
static ID3D11DeviceContext *ctx;
static PFN_COMPILE compile;
static int failures;

static const char *hlsl =
    "cbuffer C : register(b0) { float4 color; };\n"
    "float4 vs(float2 p : POSITION) : SV_Position { return float4(p, 0, 1); }\n"
    "float4 ps() : SV_Target { return color; }\n"
    "RWTexture2D<float4> uav : register(u0);\n"
    "[numthreads(8, 8, 1)] void cs(uint3 id : SV_DispatchThreadID) { uav[id.xy] = color; }\n";

static ID3DBlob *shader(const char *entry, const char *target)
{
    ID3DBlob *code = NULL, *err = NULL;
    HRESULT hr = compile(hlsl, strlen(hlsl), "probe", NULL, NULL, entry, target, 0, 0, &code, &err);
    if (FAILED(hr)) {
        printf("compile %s failed: %s\n", entry, err ? (char *)ID3D10Blob_GetBufferPointer(err) : "?");
        exit(2);
    }
    return code;
}

static ID3D11Texture2D *texture(UINT bind, D3D11_USAGE usage, UINT cpu)
{
    D3D11_TEXTURE2D_DESC d = { W, H, 1, 1, DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, usage, bind, cpu, 0 };
    ID3D11Texture2D *t = NULL;
    HRESULT hr = ID3D11Device_CreateTexture2D(dev, &d, NULL, &t);
    if (FAILED(hr)) {
        printf("CreateTexture2D(bind=0x%x) hr=0x%08lx\n", bind, (unsigned long)hr);
        exit(2);
    }
    return t;
}

static ID3D11Buffer *cbuffer(D3D11_USAGE usage, const float *rgba)
{
    D3D11_BUFFER_DESC d = { 16, usage, D3D11_BIND_CONSTANT_BUFFER,
                            usage == D3D11_USAGE_DYNAMIC ? D3D11_CPU_ACCESS_WRITE : 0, 0, 0 };
    D3D11_SUBRESOURCE_DATA init = { rgba, 0, 0 };
    ID3D11Buffer *b = NULL;
    ID3D11Device_CreateBuffer(dev, &d, rgba ? &init : NULL, &b);
    return b;
}

/* Copy src into a sentinel-filled staging texture and compare every texel. */
static void check(const char *path, ID3D11Texture2D *src, UINT expect)
{
    ID3D11Texture2D *st = texture(0, D3D11_USAGE_STAGING, D3D11_CPU_ACCESS_READ | D3D11_CPU_ACCESS_WRITE);
    D3D11_MAPPED_SUBRESOURCE m;
    UINT bad = 0, first = 0, sentinel = 0;

    ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)st, 0, D3D11_MAP_WRITE, 0, &m);
    for (UINT y = 0; y < H; y++)
        for (UINT x = 0; x < W; x++)
            ((UINT *)((BYTE *)m.pData + y * m.RowPitch))[x] = SENTINEL;
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)st, 0);

    ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource *)st, (ID3D11Resource *)src);
    if (FAILED(ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)st, 0, D3D11_MAP_READ, 0, &m))) {
        printf("PATH %-8s FAIL (Map READ failed)\n", path);
        failures++;
        return;
    }
    for (UINT y = 0; y < H; y++)
        for (UINT x = 0; x < W; x++) {
            UINT v = ((UINT *)((BYTE *)m.pData + y * m.RowPitch))[x];
            if (v != expect) {
                if (!bad)
                    first = v;
                bad++;
                sentinel += v == SENTINEL;
            }
        }
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)st, 0);
    ID3D11Texture2D_Release(st);
    if (bad) {
        printf("PATH %-8s FAIL %u/%u texels wrong, first=0x%08x expected=0x%08x%s\n", path, bad,
               W * H, first, expect, sentinel ? " (sentinel: never written)" : "");
        failures++;
    } else {
        printf("PATH %-8s PASS 0x%08x\n", path, expect);
    }
}

static void path_clear(void)
{
    ID3D11Texture2D *t = texture(D3D11_BIND_RENDER_TARGET, D3D11_USAGE_DEFAULT, 0);
    ID3D11RenderTargetView *rtv;
    const float c[4] = { 1.0f, 0.0f, 0.0f, 1.0f };
    ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)t, NULL, &rtv);
    ID3D11DeviceContext_ClearRenderTargetView(ctx, rtv, c);
    check("clear", t, 0xff0000ff);
    ID3D11RenderTargetView_Release(rtv);
    ID3D11Texture2D_Release(t);
}

static void path_upload(void)
{
    ID3D11Texture2D *t = texture(D3D11_BIND_SHADER_RESOURCE, D3D11_USAGE_DEFAULT, 0);
    static UINT px[W * H];
    for (int i = 0; i < W * H; i++)
        px[i] = 0x8040c020;
    ID3D11DeviceContext_UpdateSubresource(ctx, (ID3D11Resource *)t, 0, NULL, px, W * 4, 0);
    check("upload", t, 0x8040c020);
    ID3D11Texture2D_Release(t);
}

static void draw(ID3D11Texture2D *t, ID3D11Buffer *cb)
{
    static const float tri[] = { -1, -1, -1, 3, 3, -1 };   /* covers the target */
    D3D11_BUFFER_DESC vd = { sizeof(tri), D3D11_USAGE_DEFAULT, D3D11_BIND_VERTEX_BUFFER, 0, 0, 0 };
    D3D11_SUBRESOURCE_DATA vi = { tri, 0, 0 };
    D3D11_INPUT_ELEMENT_DESC el = { "POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0 };
    D3D11_VIEWPORT vp = { 0, 0, W, H, 0, 1 };
    ID3DBlob *vsb = shader("vs", "vs_5_0"), *psb = shader("ps", "ps_5_0");
    ID3D11VertexShader *vs;
    ID3D11PixelShader *ps;
    ID3D11InputLayout *il;
    ID3D11Buffer *vb;
    ID3D11RenderTargetView *rtv;
    UINT stride = 8, off = 0;

    ID3D11Device_CreateVertexShader(dev, ID3D10Blob_GetBufferPointer(vsb), ID3D10Blob_GetBufferSize(vsb), NULL, &vs);
    ID3D11Device_CreatePixelShader(dev, ID3D10Blob_GetBufferPointer(psb), ID3D10Blob_GetBufferSize(psb), NULL, &ps);
    ID3D11Device_CreateInputLayout(dev, &el, 1, ID3D10Blob_GetBufferPointer(vsb), ID3D10Blob_GetBufferSize(vsb), &il);
    ID3D11Device_CreateBuffer(dev, &vd, &vi, &vb);
    ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource *)t, NULL, &rtv);
    ID3D11DeviceContext_IASetInputLayout(ctx, il);
    ID3D11DeviceContext_IASetVertexBuffers(ctx, 0, 1, &vb, &stride, &off);
    ID3D11DeviceContext_IASetPrimitiveTopology(ctx, D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ID3D11DeviceContext_VSSetShader(ctx, vs, NULL, 0);
    ID3D11DeviceContext_PSSetShader(ctx, ps, NULL, 0);
    ID3D11DeviceContext_PSSetConstantBuffers(ctx, 0, 1, &cb);
    ID3D11DeviceContext_RSSetViewports(ctx, 1, &vp);
    ID3D11DeviceContext_OMSetRenderTargets(ctx, 1, &rtv, NULL);
    ID3D11DeviceContext_Draw(ctx, 3, 0);
    ID3D11DeviceContext_OMSetRenderTargets(ctx, 0, NULL, NULL);
    ID3D11RenderTargetView_Release(rtv);
    ID3D11Buffer_Release(vb);
    ID3D11InputLayout_Release(il);
    ID3D11PixelShader_Release(ps);
    ID3D11VertexShader_Release(vs);
}

static void path_draw(void)
{
    const float c[4] = { 0.0f, 1.0f, 0.0f, 1.0f };
    ID3D11Texture2D *t = texture(D3D11_BIND_RENDER_TARGET, D3D11_USAGE_DEFAULT, 0);
    ID3D11Buffer *cb = cbuffer(D3D11_USAGE_DEFAULT, c);
    draw(t, cb);
    check("draw", t, 0xff00ff00);
    ID3D11Buffer_Release(cb);
    ID3D11Texture2D_Release(t);
}

/* Two WRITE_DISCARD updates of one buffer before a single draw: the draw
 * must see the second contents (buffer renaming). */
static void path_dynamic(void)
{
    const float c1[4] = { 1.0f, 1.0f, 1.0f, 1.0f }, c2[4] = { 0.0f, 0.0f, 1.0f, 1.0f };
    ID3D11Texture2D *t = texture(D3D11_BIND_RENDER_TARGET, D3D11_USAGE_DEFAULT, 0);
    ID3D11Buffer *cb = cbuffer(D3D11_USAGE_DYNAMIC, NULL);
    D3D11_MAPPED_SUBRESOURCE m;
    ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m);
    memcpy(m.pData, c1, 16);
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)cb, 0);
    ID3D11DeviceContext_Map(ctx, (ID3D11Resource *)cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m);
    memcpy(m.pData, c2, 16);
    ID3D11DeviceContext_Unmap(ctx, (ID3D11Resource *)cb, 0);
    draw(t, cb);
    check("dynamic", t, 0xffff0000);
    ID3D11Buffer_Release(cb);
    ID3D11Texture2D_Release(t);
}

static void path_compute(void)
{
    const float c[4] = { 0.6f, 0.2f, 1.0f, 1.0f };   /* exact in UNORM8 */
    ID3D11Texture2D *t = texture(D3D11_BIND_UNORDERED_ACCESS, D3D11_USAGE_DEFAULT, 0);
    ID3D11Buffer *cb = cbuffer(D3D11_USAGE_DEFAULT, c);
    ID3DBlob *csb = shader("cs", "cs_5_0");
    ID3D11ComputeShader *cs;
    ID3D11UnorderedAccessView *uav;
    ID3D11Device_CreateComputeShader(dev, ID3D10Blob_GetBufferPointer(csb), ID3D10Blob_GetBufferSize(csb), NULL, &cs);
    ID3D11Device_CreateUnorderedAccessView(dev, (ID3D11Resource *)t, NULL, &uav);
    ID3D11DeviceContext_CSSetShader(ctx, cs, NULL, 0);
    ID3D11DeviceContext_CSSetConstantBuffers(ctx, 0, 1, &cb);
    ID3D11DeviceContext_CSSetUnorderedAccessViews(ctx, 0, 1, &uav, NULL);
    ID3D11DeviceContext_Dispatch(ctx, W / 8, H / 8, 1);
    ID3D11DeviceContext_CSSetUnorderedAccessViews(ctx, 0, 1, &(ID3D11UnorderedAccessView *){ NULL }, NULL);
    check("compute", t, 0xffff3399);
    ID3D11UnorderedAccessView_Release(uav);
    ID3D11ComputeShader_Release(cs);
    ID3D11Buffer_Release(cb);
    ID3D11Texture2D_Release(t);
}

int main(void)
{
    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HMODULE dc = LoadLibraryA("d3dcompiler_47.dll");
    IDXGIAdapter1 *a;
    HRESULT hr;

    printf("build: %s\n", probe_build_arch());
    compile = dc ? (PFN_COMPILE)GetProcAddress(dc, "D3DCompile") : NULL;
    if (!compile) {
        printf("d3dcompiler_47.dll not available\n");
        return 2;
    }
    a = probe_find_adapter(0);
    if (!a)
        return 2;
    hr = D3D11CreateDevice((IDXGIAdapter *)a, D3D_DRIVER_TYPE_UNKNOWN, NULL, 0, &fl, 1,
                           D3D11_SDK_VERSION, &dev, NULL, &ctx);
    if (FAILED(hr)) {
        printf("D3D11CreateDevice on the VirtIO adapter hr=0x%08lx\n", (unsigned long)hr);
        return 2;
    }
    path_clear();
    path_upload();
    path_draw();
    path_dynamic();
    path_compute();
    hr = ID3D11Device_GetDeviceRemovedReason(dev);
    if (hr != S_OK) {
        printf("device removed: 0x%08lx\n", (unsigned long)hr);
        failures++;
    }
    printf("READBACK11 %s (%d failing path(s))\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
