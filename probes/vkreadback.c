/* vkreadback: pixel-exact Vulkan checks without a window (Linux guest, Venus).
 *
 * Paths (each into a device-local image, copied to a sentinel-filled
 * HOST_VISIBLE buffer, waited on with a fence, compared):
 *   clear     vkCmdClearColorImage
 *   upload    host-visible buffer -> vkCmdCopyBufferToImage
 *
 * Host-visible memory maps to a different host mechanism than device-local
 * memory, and a buffer->image copy to a different one than a clear, so they
 * can fail independently.  Copy this file and replace a path's body to make
 * a repro.  Picks the first non-CPU device; prints which.
 *
 * build in the guest: cc -O2 -o vkreadback vkreadback.c -lvulkan
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#define W 64
#define H 64
#define SENTINEL 0xDEADBEEFu
#define CHECK(x) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { \
    printf("%s failed: %d\n", #x, r_); exit(2); } } while (0)

static VkDevice dev;
static VkPhysicalDevice phys;
static VkQueue queue;
static VkCommandPool pool;
static int failures;

static uint32_t memtype(uint32_t bits, VkMemoryPropertyFlags want)
{
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(phys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want)
            return i;
    printf("no memory type 0x%x\n", want);
    exit(2);
}

static VkBuffer buffer(VkDeviceMemory *mem, void **map)
{
    VkBufferCreateInfo bi = { VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, NULL, 0, W * H * 4,
                              VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT };
    VkMemoryRequirements req;
    VkBuffer b;
    CHECK(vkCreateBuffer(dev, &bi, NULL, &b));
    vkGetBufferMemoryRequirements(dev, b, &req);
    VkMemoryAllocateInfo ai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size,
        memtype(req.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) };
    CHECK(vkAllocateMemory(dev, &ai, NULL, mem));
    CHECK(vkBindBufferMemory(dev, b, *mem, 0));
    CHECK(vkMapMemory(dev, *mem, 0, W * H * 4, 0, map));
    return b;
}

static VkImage image(VkDeviceMemory *mem)
{
    VkImageCreateInfo ii = { VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, NULL, 0, VK_IMAGE_TYPE_2D,
                             VK_FORMAT_R8G8B8A8_UNORM, { W, H, 1 }, 1, 1, VK_SAMPLE_COUNT_1_BIT,
                             VK_IMAGE_TILING_OPTIMAL,
                             VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT };
    VkMemoryRequirements req;
    VkImage img;
    CHECK(vkCreateImage(dev, &ii, NULL, &img));
    vkGetImageMemoryRequirements(dev, img, &req);
    VkMemoryAllocateInfo ai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size,
                                memtype(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) };
    CHECK(vkAllocateMemory(dev, &ai, NULL, mem));
    CHECK(vkBindImageMemory(dev, img, *mem, 0));
    return img;
}

static void barrier(VkCommandBuffer cb, VkImage img, VkImageLayout from, VkImageLayout to)
{
    VkImageMemoryBarrier b = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, NULL,
                               VK_ACCESS_MEMORY_WRITE_BIT, VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT,
                               from, to, VK_QUEUE_FAMILY_IGNORED, VK_QUEUE_FAMILY_IGNORED, img,
                               { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 } };
    vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                         0, 0, NULL, 0, NULL, 1, &b);
}

static VkCommandBuffer begin(void)
{
    VkCommandBufferAllocateInfo ai = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, pool,
                                       VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1 };
    VkCommandBufferBeginInfo bi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
                                    VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
    VkCommandBuffer cb;
    CHECK(vkAllocateCommandBuffers(dev, &ai, &cb));
    CHECK(vkBeginCommandBuffer(cb, &bi));
    return cb;
}

static void submit(VkCommandBuffer cb)
{
    VkFenceCreateInfo fi = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
    VkSubmitInfo si = { VK_STRUCTURE_TYPE_SUBMIT_INFO, NULL, 0, NULL, NULL, 1, &cb };
    VkFence f;
    CHECK(vkEndCommandBuffer(cb));
    CHECK(vkCreateFence(dev, &fi, NULL, &f));
    CHECK(vkQueueSubmit(queue, 1, &si, f));
    if (vkWaitForFences(dev, 1, &f, VK_TRUE, 10000000000ull) != VK_SUCCESS)
        printf("fence not signalled in 10 s (GPU hang or lost completion)\n");
    vkDestroyFence(dev, f, NULL);
    vkFreeCommandBuffers(dev, pool, 1, &cb);
}

/* img is in TRANSFER_SRC_OPTIMAL; cb is recording. */
static void check(const char *path, VkCommandBuffer cb, VkImage img, uint32_t expect)
{
    VkDeviceMemory mem;
    uint32_t *p, bad = 0, first = 0, sentinel = 0;
    VkBuffer rb = buffer(&mem, (void **)&p);
    VkBufferImageCopy r = { 0, 0, 0, { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 }, { 0, 0, 0 }, { W, H, 1 } };
    for (int i = 0; i < W * H; i++)
        p[i] = SENTINEL;
    vkCmdCopyImageToBuffer(cb, img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rb, 1, &r);
    submit(cb);
    for (int i = 0; i < W * H; i++)
        if (p[i] != expect) {
            if (!bad)
                first = p[i];
            bad++;
            sentinel += p[i] == SENTINEL;
        }
    vkDestroyBuffer(dev, rb, NULL);
    vkFreeMemory(dev, mem, NULL);
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
    VkDeviceMemory mem;
    VkImage img = image(&mem);
    VkClearColorValue c = { .float32 = { 1.0f, 0.0f, 0.0f, 1.0f } };
    VkImageSubresourceRange range = { VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1 };
    VkCommandBuffer cb = begin();
    barrier(cb, img, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    vkCmdClearColorImage(cb, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &c, 1, &range);
    barrier(cb, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    check("clear", cb, img, 0xff0000ff);
    vkDestroyImage(dev, img, NULL);
    vkFreeMemory(dev, mem, NULL);
}

static void path_upload(void)
{
    VkDeviceMemory imem, bmem;
    uint32_t *p;
    VkImage img = image(&imem);
    VkBuffer up = buffer(&bmem, (void **)&p);
    VkBufferImageCopy r = { 0, 0, 0, { VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1 }, { 0, 0, 0 }, { W, H, 1 } };
    VkCommandBuffer cb = begin();
    for (int i = 0; i < W * H; i++)
        p[i] = 0x8040c020;
    barrier(cb, img, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL);
    vkCmdCopyBufferToImage(cb, up, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &r);
    barrier(cb, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL);
    check("upload", cb, img, 0x8040c020);
    vkDestroyBuffer(dev, up, NULL);
    vkFreeMemory(dev, bmem, NULL);
    vkDestroyImage(dev, img, NULL);
    vkFreeMemory(dev, imem, NULL);
}

int main(void)
{
    VkApplicationInfo app = { VK_STRUCTURE_TYPE_APPLICATION_INFO, NULL, "vkreadback", 1, NULL, 0,
                              VK_API_VERSION_1_1 };
    VkInstanceCreateInfo ici = { VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, NULL, 0, &app };
    VkInstance inst;
    VkPhysicalDevice devs[8];
    uint32_t n = 8, qf = 0;
    float prio = 1.0f;
    VkPhysicalDeviceProperties props;

    CHECK(vkCreateInstance(&ici, NULL, &inst));
    CHECK(vkEnumeratePhysicalDevices(inst, &n, devs));
    phys = VK_NULL_HANDLE;
    for (uint32_t i = 0; i < n; i++) {
        vkGetPhysicalDeviceProperties(devs[i], &props);
        printf("device %u: %s (type %d)\n", i, props.deviceName, props.deviceType);
        if (!phys && props.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU)
            phys = devs[i];
    }
    if (!phys) {
        printf("no GPU device (only a CPU device such as llvmpipe): Venus is not working\n");
        return 2;
    }
    vkGetPhysicalDeviceProperties(phys, &props);
    printf("using: %s\n", props.deviceName);
    VkDeviceQueueCreateInfo qi = { VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, NULL, 0, qf, 1, &prio };
    VkDeviceCreateInfo dci = { VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, NULL, 0, 1, &qi };
    CHECK(vkCreateDevice(phys, &dci, NULL, &dev));
    vkGetDeviceQueue(dev, qf, 0, &queue);
    VkCommandPoolCreateInfo pi = { VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL, 0, qf };
    CHECK(vkCreateCommandPool(dev, &pi, NULL, &pool));
    path_clear();
    path_upload();
    printf("VKREADBACK %s (%d failing path(s))\n", failures ? "FAIL" : "PASS", failures);
    return failures ? 1 : 0;
}
