---
name: attribute-to-layer
description: Decide which layer of the VirtIO GPU stack owns a bug before fixing it — the guest driver (UMD/KMD), the virtio transport and virglrenderer, the d3dmetal-native or DXMT backend shim, Apple's D3DMetal itself, Wine/CrossOver behavior, or UTM's display path — by swapping backends, pinning D3DMetal versions, comparing against CrossOver correctly, and running one probe through every layer. Use when a bug might not be ours, when a result differs between backends or from CrossOver, or before investing in a fix.
argument-hint: "<bug or failing test>"
---

# Attribute a bug to a layer

Bug: `$ARGUMENTS`

The same symptom can come from six layers. A fix in the wrong one either
does nothing or masks the real bug. Attribute first.

```
guest app -> D3D runtime -> UMD/KMD (guest) -> virtio-gpu (QEMU) -> virglrenderer
          -> backend shim (d3dmetal-native | DXMT) -> D3DMetal (Apple) -> Metal
display:  guest present -> scanout -> QEMU SPICE GL -> UTM window
```

## Steps

1. **Swap the backend.** Run the repro under both backends:
   `utmbot vm up --backend d3dmetal` and `--backend dxmt`, and prove each
   with `utmbot backend`.
   - Fails on one backend only: that backend's shim or D3DMetal/DXMT itself,
     or a translation our layer applies only for it.
   - Fails on both: our guest driver, the transport or virglrenderer, the
     display path, or the app.
   - Compare at equal frame rates: anything rate-dependent (tearing,
     flashes) looks backend-specific when it is not.
2. **Pin the D3DMetal version.** Record the version that ran next to every
   result (`utmbot backend` prints it). Different D3DMetal releases fix and
   break different things.
   - Point `D3DMETAL_FRAMEWORK_PATH` at another D3DMetal build
     (`vm up --env …`) to test a version theory.
   - Before calling something a regression, check which D3DMetal the
     "passing" run used. A known-good result once came from a different
     D3DMetal that had drifted in.
3. **Compare with CrossOver,** which runs the same D3DMetal/DXMT on the host
   without our guest layers. If it works under CrossOver and fails in the
   VM, the difference is ours: guest driver, transport, shim usage or caps.
   `/utmapp-bot:crossover-compare` runs a game or an x64 probe in a bottle
   on a chosen backend and proves the backend (`utmbot cx run`, `cx verify`,
   `cx exec`). Rules:
   - **Verify the backend CrossOver really used from inside the app**, e.g.
     the adapter name or vendor ID it reports. A bottle setting or
     environment override is not proof: CrossOver's compatibility database
     can pick another backend per app, and a D3D9 or OpenGL game never
     reaches D3DMetal or DXMT at all. `cx run` and `cx proof` print the
     proven `BACKEND` and exit 2 when the run did not respect the
     backend asked for.
   - **Make CrossOver look like the VM, never the reverse.** Use the same
     executable name, the same reported vendor and device IDs, the same
     feature level and memory size (`D3DM_VENDOR_ID`, `D3DM_DEVICE_ID`,
     `D3DM_EXE_OVERRIDE` for D3DMetal; crossover-compare has the adapters
     each backend reports by default). D3DMetal and some engines switch
     behavior on the executable name or GPU vendor. *Example: an engine
     enabled shared video surfaces only for some GPU vendor IDs: video was
     black in the VM, fine in CrossOver, and nothing was wrong in the
     driver.*
   - **CrossOver's capability answers are not ground truth.** Its backends
     answer some queries with placeholders. Parity means the same behavior,
     not the same numbers.
   - **A CrossOver "timeout" is often a crash** waiting on a debugger
     dialog. Run CrossOver only while the VM is down: they share the GPU.
4. **One probe through every layer** (`/utmapp-bot:write-gpu-probe`). Run the
   same minimal repro:
   1. against the backend directly on the host (a small program that loads
      D3DMetal or DXMT the way the shim does);
   2. through the shim (d3dmetal-native/DXMT native API);
   3. under CrossOver/Wine as a Windows exe;
   4. in the Neptune guest.

   The first layer where the verdict flips owns the bug.
5. **The display path is its own suspect.** If offscreen readback in the
   guest is right but the window is wrong, it is scanout, SPICE GL or UTM.
   Frames going backwards can only be the display path (triage skill,
   black-screen-and-pacing.md).

## Decide

- **Ours:** fix it in our layer, generically, and land it with
  `/utmapp-bot:land-stack-change` (its review step comes first).
- **A backend defect** (fails raw on the host, or under CrossOver with
  identical failure text): record it with the probe that shows it, and work
  around it in the shim only when the workaround is correct for every legal
  input. Fix our own bugs first. A test counts as a backend defect only when
  CrossOver fails it with the *same* failure; a different failure text is
  two bugs.
- **App behavior** (vendor checks, executable-name profiles): document it.
  Change the driver only if the API contract says we are wrong.
- **More than one layer:** one symptom can need two fixes. An odd answer in
  one layer (a mode list, a capability, a format query) can make the app
  take a path that then breaks another layer. Attribute each cause
  separately, starting from the earliest difference from a working run,
  and expect fixes in more than one repository.

## Conformance-scale parity (optional)

When a whole test suite differs between CrossOver and the VM, compare per
test case, not by totals. The directx-cts harness
(`https://github.com/osy/directx-cts`) runs Microsoft's HLK D3D conformance
content under both and diffs the results per case. A sampled run hides
crashes, so run the full plan of the area you changed. Every test that
passes on CrossOver must pass in the VM; anything only the VM fails is
ours until shown otherwise.
