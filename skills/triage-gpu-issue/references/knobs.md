# Debug knobs

Knobs come and go with the code. A name from an old note that the code no
longer reads is silently ignored, and an experiment run with it measures
nothing. Before relying on one, check it against the trees you actually run:

```sh
${CLAUDE_PLUGIN_ROOT}/scripts/knob-audit.sh <virglrenderer>/src <d3dmetal-native>/src <dxmt>/src <mesa>/src/neptune
${CLAUDE_PLUGIN_ROOT}/scripts/knob-audit.sh --has NPT_DEBUG <virglrenderer>
```

**Where each kind goes:**
- Host knobs go to QEMU and the render server with `$UB vm up --env NAME=value`
  (UTM's `start … environment`; the Debug Log header shows what arrived).
- Guest knobs go to the program with `$UB test|launch … --genv NAME=value`.
  The task wrapper writes them as `set "NAME=value"` lines. An unquoted
  `set NAME=value &&` keeps a trailing space in the value.

## Host (audited 2026-10 against virglrenderer, d3dmetal-native and dxmt main branches)

| Knob | Read by | Effect |
|---|---|---|
| `VIRGL_LOG_FILE=<path>` | render server | Log file; `%PID%` is replaced by the process id (one file per worker). Without it the log goes to QEMU's stderr, i.e. debug.log. The sandbox allows only UTM's group container, which the terminal cannot read without Full Disk Access, so leave it unset. |
| `VIRGL_LOG_LEVEL=debug` | render server | Set by UTM's Debug Log. |
| `NPT_DEBUG=profile,fence_trace` | virglrenderer (Neptune) | `profile`: periodic per-method dispatch/latency profile (`NPT-PROF-HOST*` lines in debug.log: `hostlog --grep NPT-PROF`). Methods are named by number; decode them with the protocol headers. It timestamps every command: under Rosetta that cost 21% of the ring thread, so take no CPU profile with it on. `fence_trace`: per-frame fence/queue trace. Unknown tokens are silently ignored. |
| `NPT_PROFILE_PERIOD_MS=<ms>` | virglrenderer | Profile reporting period. |
| `NPT_BACKEND=d3dmetal\|dxmt` | virglrenderer | Set by UTM from `QEMUDirectXDriver`; use `--backend`, not this. |
| `NPT_CAPSET_D3D12=0\|1`, `NPT_CAPSET_CAPS=<hex>` | virglrenderer | Override what the capset advertises to the guest (D3D12 support, capability bits). Only for testing guest behavior under other caps. |
| `NPT_WA_FLAGS=<hex>` | virglrenderer | Forces the host workaround-flag set. `0` disables every host workaround, to see raw backend behavior. |
| `VKR_DEBUG=validate,gbm,udmabuf` | virglrenderer (Venus) | `validate` turns on the Vulkan validation layer for Venus contexts. |
| `VREND_DEBUG=…` | virglrenderer (GL) | GL-path debug flags (virgl GL contexts only). |
| `DMN_LOG=error\|warn\|info\|debug\|trace\|quiet` | d3dmetal-native | Shim log level. `trace` logs every call, far too much for a whole run. |
| `DMN_ALLOC_TRACE` | d3dmetal-native | Allocation trace, lighter than `DMN_LOG=trace`. |
| `DMN_SPARSE=0` | d3dmetal-native | Disables sparse/reserved-resource emulation (resources fully backed). An A/B lever for tiled-resource bugs. |
| `DMN_SPARSE_STATS`, `DMN_SPARSE_OVERSUBSCRIBE` | d3dmetal-native | Sparse pool statistics; pool sizing. |
| `DMN_RESIDENCY_SET`, `DMN_PRESENT_FALLBACK`, `DMN_RETINA`, `DMN_VSYNC` | d3dmetal-native | Residency mechanism, present path, Retina scaling and vsync choices. Read the code for the values before using them. |
| `D3DMETAL_FRAMEWORK_PATH=<D3DMetal binary>` | d3dmetal-native | Which D3DMetal to load. UTM sets it to the embedded one; point it at another build to compare D3DMetal versions. |
| `DXMT_LOG_LEVEL`, `DXMT_LOG_PATH` | DXMT | DXMT logging. |
| `DXMT_CAPTURE_FRAME`, `DXMT_CAPTURE_EXECUTABLE`, `MTL_CAPTURE_ENABLED=1` | DXMT, Metal | Metal GPU frame capture (`.gputrace`). |
| `DXMT_CONFIG`, `DXMT_CONFIG_FILE`, `DXMT_SHADER_CACHE`, `DXMT_SHADER_CACHE_PATH`, `DXMT_USE_DEFAULT_METAL_CACHE` | DXMT | Configuration and shader cache. |
| `MTL_DEBUG_LAYER=1` | Metal | Use `defaults write com.utmapp.UTM GraphicsValidation -bool YES` instead (it applies only with Debug Log on). It names the offending object for many silent-drop bugs (e.g. writes to resources that are not resident). Costs ~20% fps. |

**Do not use:**
- `MTL_SHADER_VALIDATION=1` with D3DMetal: it has wedged the GPU.
- `vmmap` on a render server: it stalls GPU submission long enough for the
  guest to reset the GPU.
- Shader-cache control through `DIRHELPER_USER_DIR_SUFFIX`: it is a Darwin
  per-user cache-dir suffix that worked for an unsandboxed render server.
  Under UTM's sandbox it is unverified. Check where the cache actually lives
  before you rely on cold/warm control.

## Guest (Windows UMD/KMD, Linux Mesa)

The guest driver has trace knobs for:
- the present path (gate waits, event waits);
- fences and submission;
- per-frame content checks (sampling the presented frame to tell "presented
  black" from "displayed black");
- protocol logging.

Their names change. List them with
`knob-audit.sh <mesa>/src/virtio/neptune` and read the flag tables
(`npt_env.c`). As of 2026-11, in `osy/virtio-win-mesa` `neptune`:

| Knob | Effect |
|---|---|
| `NPT_DEBUG=present_timing` | Every 120 presents: `PT n=… avg_us sig= arm= wait= cb= total= wait_max= nowait=`. `wait` is the pacing wait for frame N−depth; the depth is logged as `present pacing: depth …`. D3D12 titles log `12.PT n=…` instead, and only for presents that take the present-arm path, which leaves `12.PresentArm #` lines. If the log has `12.Present #` lines but none of those, the UMD is not pacing that title: there is no pacing wait to measure, so take present intervals from `etw analyze`. |
| `NPT_PRESENT_GATE_TRACE=<ms>` | Logs each present whose gate took at least `<ms>` (`present-gate SLOW …`). |
| `NPT_STUTTER_MS=<ms>` | Present-to-present gap that logs a STUTTER line (default 50). |
| `NPT_EVENT_WAITER_TRACE=<ms>` | Slow event-waiter wakeups. |

The UMD logs with `OutputDebugString`: collect it with `$UB ods <secs>`
around the run and `$UB ods get`.

Constant patterns:
- Guest `NPT_DEBUG` tokens go to the guest driver; host `NPT_DEBUG` goes
  through `--env`.
- Some guest debug modes force a test transport (no GPU). If a debug knob
  turns "device creation failed" into a different failure, run without it to
  see the real error.
- The KMD's verbosity is a registry value read at driver load. Set it before
  `install`, then watch `kd.log`.
- The Linux Venus driver reads `VN_DEBUG=init,…` and `VN_PERF`; the Vulkan
  loader reads `VK_LOADER_DEBUG=all`.

## Removed diagnostics, kept as techniques

Older notes name knobs that were session-only diagnostics and are gone. Do
not look for them; rebuild the technique when you need it:
- **Per-call synchronous trace** to find which call hangs the GPU: sync after
  every command. The last call logged without a sync is only where the CPU
  was.
- **Shader replace/skip by hash** to bisect a GPU hang or corruption to one
  shader.
- **Pre-poisoned readback buffers.** A sentinel separates "wrong value" from
  "never written". The probes do this.
- **Retaining command buffers** to test a resource-lifetime theory.
- **Serializing queues** to test an ordering theory.
- **A host GPU watchdog / stall detector.**

Each is a few lines of code in the layer under test. Keep it as a patch; do
not commit it.
