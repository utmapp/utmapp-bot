/* readback12: pixel-exact D3D12 checks without a window.
 *
 * Paths (each into its own texture, copied to a sentinel-filled readback
 * buffer, waited on with a fence, compared):
 *   clear        ClearRenderTargetView
 *   upload       upload-heap buffer -> CopyTextureRegion
 *
 * A D3D12 repro usually needs barriers, heaps or queues the D3D11 probe cannot
 * express: copy this file, keep main()/check(), and replace a path's body.
 * Exit 0 = all paths PASS.  Run in the console session.
 */
#include "common.h"
#include <d3d12.h>
#include <string.h>

#define W 64
#define H 64
#define SENTINEL 0xDEADBEEFu
#define ROW 256  /* W * 4 rounded up to D3D12_TEXTURE_DATA_PITCH_ALIGNMENT */

typedef HRESULT (WINAPI *PFN_CREATE)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

static ID3D12Device *dev;
static ID3D12CommandQueue *queue;
static ID3D12CommandAllocator *alloc;
static ID3D12GraphicsCommandList *list;
static ID3D12Fence *fence;
static UINT64 fence_value;
static int failures;

static ID3D12Resource *buffer(D3D12_HEAP_TYPE type, UINT64 size, D3D12_RESOURCE_STATES state)
{
    D3D12_HEAP_PROPERTIES hp = { type };
    D3D12_RESOURCE_DESC d = { D3D12_RESOURCE_DIMENSION_BUFFER, 0, size, 1, 1, 1,
                              DXGI_FORMAT_UNKNOWN, { 1, 0 }, D3D12_TEXTURE_LAYOUT_ROW_MAJOR };
    ID3D12Resource *r = NULL;
    HRESULT hr = ID3D12Device_CreateCommittedResource(dev, &hp, 0, &d, state, NULL,
                                                      &IID_ID3D12Resource, (void **)&r);
    if (FAILED(hr)) {
        printf("CreateCommittedResource(buffer) hr=0x%08lx\n", (unsigned long)hr);
        exit(2);
    }
    return r;
}

static ID3D12Resource *texture(D3D12_RESOURCE_FLAGS flags, D3D12_RESOURCE_STATES state)
{
    D3D12_HEAP_PROPERTIES hp = { D3D12_HEAP_TYPE_DEFAULT };
    D3D12_RESOURCE_DESC d = { D3D12_RESOURCE_DIMENSION_TEXTURE2D, 0, W, H, 1, 1,
                              DXGI_FORMAT_R8G8B8A8_UNORM, { 1, 0 }, D3D12_TEXTURE_LAYOUT_UNKNOWN, flags };
    ID3D12Resource *r = NULL;
    HRESULT hr = ID3D12Device_CreateCommittedResource(dev, &hp, 0, &d, state, NULL,
                                                      &IID_ID3D12Resource, (void **)&r);
    if (FAILED(hr)) {
        printf("CreateCommittedResource(texture) hr=0x%08lx\n", (unsigned long)hr);
        exit(2);
    }
    return r;
}

static void barrier(ID3D12Resource *r, D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to)
{
    D3D12_RESOURCE_BARRIER b = { D3D12_RESOURCE_BARRIER_TYPE_TRANSITION };
    b.Transition.pResource = r;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    b.Transition.StateBefore = from;
    b.Transition.StateAfter = to;
    ID3D12GraphicsCommandList_ResourceBarrier(list, 1, &b);
}

static void submit_and_wait(void)
{
    HANDLE ev = CreateEventA(NULL, FALSE, FALSE, NULL);
    ID3D12GraphicsCommandList_Close(list);
    ID3D12CommandQueue_ExecuteCommandLists(queue, 1, (ID3D12CommandList **)&list);
    ID3D12CommandQueue_Signal(queue, fence, ++fence_value);
    ID3D12Fence_SetEventOnCompletion(fence, fence_value, ev);
    if (WaitForSingleObject(ev, 10000) != WAIT_OBJECT_0)
        printf("fence %llu not reached in 10 s (GPU hang or lost completion)\n", fence_value);
    CloseHandle(ev);
    ID3D12CommandAllocator_Reset(alloc);
    ID3D12GraphicsCommandList_Reset(list, alloc, NULL);
}

/* tex must be in COPY_SOURCE; copy it to a sentinel-filled readback buffer. */
static void check(const char *path, ID3D12Resource *tex, UINT expect)
{
    ID3D12Resource *rb = buffer(D3D12_HEAP_TYPE_READBACK, ROW * H, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_TEXTURE_COPY_LOCATION dst = { rb, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
    D3D12_TEXTURE_COPY_LOCATION src = { tex, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
    D3D12_RANGE all = { 0, ROW * H }, none = { 0, 0 };
    BYTE *p;
    UINT bad = 0, first = 0, sentinel = 0;

    ID3D12Resource_Map(rb, 0, &none, (void **)&p);
    for (UINT i = 0; i < ROW * H / 4; i++)
        ((UINT *)p)[i] = SENTINEL;
    ID3D12Resource_Unmap(rb, 0, &all);
    dst.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    dst.PlacedFootprint.Footprint.Width = W;
    dst.PlacedFootprint.Footprint.Height = H;
    dst.PlacedFootprint.Footprint.Depth = 1;
    dst.PlacedFootprint.Footprint.RowPitch = ROW;
    ID3D12GraphicsCommandList_CopyTextureRegion(list, &dst, 0, 0, 0, &src, NULL);
    submit_and_wait();
    ID3D12Resource_Map(rb, 0, &all, (void **)&p);
    for (UINT y = 0; y < H; y++)
        for (UINT x = 0; x < W; x++) {
            UINT v = ((UINT *)(p + y * ROW))[x];
            if (v != expect) {
                if (!bad)
                    first = v;
                bad++;
                sentinel += v == SENTINEL;
            }
        }
    ID3D12Resource_Unmap(rb, 0, &none);
    ID3D12Resource_Release(rb);
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
    D3D12_DESCRIPTOR_HEAP_DESC hd = { D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1 };
    ID3D12DescriptorHeap *heap;
    D3D12_CPU_DESCRIPTOR_HANDLE rtv;
    const float c[4] = { 1.0f, 0.0f, 0.0f, 1.0f };
    ID3D12Resource *t = texture(D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET, D3D12_RESOURCE_STATE_RENDER_TARGET);

    ID3D12Device_CreateDescriptorHeap(dev, &hd, &IID_ID3D12DescriptorHeap, (void **)&heap);
    /* A struct-returning COM method returns through a pointer passed after
     * `this`; header C bindings disagree on this, so call the vtable directly. */
    ((void (STDMETHODCALLTYPE *)(ID3D12DescriptorHeap *, D3D12_CPU_DESCRIPTOR_HANDLE *))
         heap->lpVtbl->GetCPUDescriptorHandleForHeapStart)(heap, &rtv);
    ID3D12Device_CreateRenderTargetView(dev, t, NULL, rtv);
    ID3D12GraphicsCommandList_ClearRenderTargetView(list, rtv, c, 0, NULL);
    barrier(t, D3D12_RESOURCE_STATE_RENDER_TARGET, D3D12_RESOURCE_STATE_COPY_SOURCE);
    check("clear", t, 0xff0000ff);
    ID3D12Resource_Release(t);
    ID3D12DescriptorHeap_Release(heap);
}

static void path_upload(void)
{
    ID3D12Resource *up = buffer(D3D12_HEAP_TYPE_UPLOAD, ROW * H, D3D12_RESOURCE_STATE_GENERIC_READ);
    ID3D12Resource *t = texture(D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_TEXTURE_COPY_LOCATION src = { up, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT };
    D3D12_TEXTURE_COPY_LOCATION dst = { t, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX };
    D3D12_RANGE none = { 0, 0 };
    BYTE *p;

    ID3D12Resource_Map(up, 0, &none, (void **)&p);
    for (UINT i = 0; i < ROW * H / 4; i++)
        ((UINT *)p)[i] = 0x8040c020;
    ID3D12Resource_Unmap(up, 0, NULL);
    src.PlacedFootprint.Footprint.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    src.PlacedFootprint.Footprint.Width = W;
    src.PlacedFootprint.Footprint.Height = H;
    src.PlacedFootprint.Footprint.Depth = 1;
    src.PlacedFootprint.Footprint.RowPitch = ROW;
    ID3D12GraphicsCommandList_CopyTextureRegion(list, &dst, 0, 0, 0, &src, NULL);
    barrier(t, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE);
    check("upload", t, 0x8040c020);
    ID3D12Resource_Release(t);
    ID3D12Resource_Release(up);
}

int main(void)
{
    HMODULE m = LoadLibraryA("d3d12.dll");
    PFN_CREATE create = m ? (PFN_CREATE)GetProcAddress(m, "D3D12CreateDevice") : NULL;
    D3D12_COMMAND_QUEUE_DESC qd = { D3D12_COMMAND_LIST_TYPE_DIRECT };
    IDXGIAdapter1 *a;
    HRESULT hr;

    printf("build: %s\n", probe_build_arch());
    if (!create) {
        printf("d3d12.dll not available\n");
        return 2;
    }
    a = probe_find_adapter(0);
    if (!a)
        return 2;
    hr = create((IUnknown *)a, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device, (void **)&dev);
    if (FAILED(hr)) {
        printf("D3D12CreateDevice on the VirtIO adapter hr=0x%08lx\n", (unsigned long)hr);
        return 2;
    }
    ID3D12Device_CreateCommandQueue(dev, &qd, &IID_ID3D12CommandQueue, (void **)&queue);
    ID3D12Device_CreateCommandAllocator(dev, D3D12_COMMAND_LIST_TYPE_DIRECT,
                                        &IID_ID3D12CommandAllocator, (void **)&alloc);
    ID3D12Device_CreateCommandList(dev, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, NULL,
                                   &IID_ID3D12GraphicsCommandList, (void **)&list);
    ID3D12Device_CreateFence(dev, 0, D3D12_FENCE_FLAG_NONE, &IID_ID3D12Fence, (void **)&fence);
    path_clear();
    path_upload();
    hr = ID3D12Device_GetDeviceRemovedReason(dev);
    if (hr != S_OK) {
        printf("device removed: 0x%08lx\n", (unsigned long)hr);
        failures++;
    }
    printf("READBACK12 %s (%d failing path(s))\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
