# Rendering corruption

Wrong colors, missing or garbage geometry, missing effects (shadows, bloom,
video), black or garbage textures.

## Method

1. **Get ground truth off the display.** Reproduce with an offscreen
   readback probe (`/utmapp-bot:write-gpu-probe`). Render into a texture,
   copy it to a CPU-readable resource that was pre-filled with a sentinel,
   and compare per texel. No swap chain, compositor or scanout is involved,
   and the sentinel separates *wrote the wrong value* from *never wrote*.
2. **Split by access path.** Clear, CPU upload, buffer-to-texture copy,
   vertex/constant-buffer draw, dynamic `Map(WRITE_DISCARD)`, compute UAV
   write and sampling each map to a different mechanism on the host and fail
   independently. `readback11`/`readback12`/`vkreadback` exercise them one
   by one. Find the first path that fails.
3. **Classify the wrong value exactly.**
   - The texel equals the sentinel: the write or copy never happened.
   - Channels permuted the same way everywhere: a format label or swizzle
     (e.g. R↔B on the whole frame).
   - Permuted in one pass only, or in some objects only: a pass-local bug
     (one shader, one resource).
   - Black or zero: a dropped dispatch or draw, an unbound resource, or a
     resource that was not resident.
   - Garbage: uninitialized memory, a stale alias, a wrong row pitch or
     offset.
4. **Compare against another backend and CrossOver**
   (`/utmapp-bot:attribute-to-layer`) before reading any of our code. Many
   corruptions are backend defects with a workaround in our layer.
5. **Capture the GPU frame** when the probe does not reproduce it. Use
   DXMT's frame capture or Metal capture (knobs.md), scoped to **one**
   worker process. Arming capture in every worker at once has taken the host
   down. Diff the bad frame against a good one (another backend, or before
   the regression): draw count, bound resources, formats.
6. **Prove the cause with a substitution.** Substitute the suspect shader or
   format on the host, by hash, as a proof of concept. Then fix it
   generically in the right layer. The fix must hold for every legal use of
   the API, not one title.

## Suspects worth checking first

These were each the real cause of a "random" corruption at least once:

- **Silent drops in the backend.** A dispatch or draw that does nothing, with
  no error, under specific conditions. Seen with:
  - writes to sparse/reserved resources;
  - 64-bit atomics;
  - draws into integer (`_SINT`/`_UINT`) render targets;
  - sample offsets on non-power-of-two textures;
  - vertex-shader reads of UAVs bound for the output merger (on older
    D3DMetal);
  - copies between buffers of different lengths.

  The Metal debug layer (`GraphicsValidation`) names some of these, e.g. a
  shader writing to a non-resident resource.
- **Capability contradictions.** The driver advertises a format/feature
  combination it does not implement consistently, e.g. typed UAV loads, MSAA
  support across a typeless family, or a format bit in one table but not
  another. The app takes a path that renders wrong without any error.
  `0x887a0020` (DRIVER_INTERNAL_ERROR) at device creation is the loud version
  of the same thing.
- **Formats.** Packed 16-bit formats (B4G4R4A4, B5G6R5) channel order; depth
  and stencil uploads that drop the stencil plane; BC/planar/video formats
  through staging Map (row pitch, block size); sRGB views of UNORM.
- **Shader translation.** DXBC/DXIL features with no direct Metal
  equivalent: input coverage, `SV_Coverage` with forced sample count,
  minimum precision, attribute ordinals vs registers, immediate texel
  offsets. A missing feature usually renders as *something missing*: no
  shadow, no edge, a cut-out.
- **Video/media paths.** Media Foundation or video decode output is usually
  a shared texture between devices. Check whether the app gets a shared
  handle at all: some engines enable shared video surfaces only for specific
  GPU vendor IDs. That makes the result "black video" on our adapter and
  fine on CrossOver, with nothing wrong in the driver.

## Dead ends, do not redo

- **"Compute is broken."** It was the readback copy that dropped, not the
  dispatch. Always pre-poison readback buffers, and test the copy path on
  its own.
- **Synchronization/ordering theories for a corruption that a caps change
  fixes.** One bloom bug was blamed on fences across four separate
  experiments; the cause was a capability contradiction. Check caps against
  a known-good stack before building ordering hypotheses.
- **Equalizing API answers with CrossOver.** Parity means the same
  *behavior*, not the same capability numbers. CrossOver's backends answer
  some queries with placeholder values.
