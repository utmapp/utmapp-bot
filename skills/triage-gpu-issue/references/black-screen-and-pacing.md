# Black screen, flicker, held frames, stutter

The display path in UTM is: guest present → KMD flip or blit → virtio-gpu
scanout → QEMU (SPICE GL) → ANGLE/Metal → CocoaSpice → the UTM window.
Every hop can drop, repeat or reorder a frame. First establish **what the
guest presented** versus **what the window showed**.

## Measure before you look

- `$UB frames 20 --save-every 2` gives distinct fps, holds (>100 ms gaps),
  the black fraction and mean luma, plus sample PNGs. A single `shot` can be
  a stale frame of a rendering window; never call something black or frozen
  from one still.
- Compare with what the app thinks it presents: its own fps counter, or
  `presentloop` (`/utmapp-bot:write-gpu-probe`).
- Capture **while** the problem happens. A short workload, such as a ~30 s
  benchmark test, is over long before a late capture.
- Before trusting any capture:
  - wake the host display;
  - keep the screen unlocked (a locked Mac shows nothing useful and naps
    UTM);
  - keep UTM in front;
  - keep the window scale the same between runs (a 1x vs 2x display makes
    black-frame counts incomparable).

## Fingerprints

| Observation | Meaning |
|---|---|
| Window black after the guest idled; one input brings it back | Guest display power-off. Not a bug (`vm up` disables the timeout). |
| Black from the moment the driver loaded; app logs fine | Scanout never bound: the host-visible memory window is missing or the KMD reports no 3D support (install-and-load.md), or presents never reach the scanout. |
| **Frames go backwards** (older content after newer) | Only the display/scanout binding can do this. A present gate can delay a frame, never reorder one. Look at QEMU/UTM display code and uncommitted edits there. |
| The guest's frame check says the presented frame has content, but the window shows black | The display path dropped good frames. |
| The guest's frame check says the presented frame is black | Content bug: corruption.md. |
| Black flashes that grow with fps above the display refresh, absent below it | Host display reading a buffer the guest is reusing. A buffer-lifetime race in the scanout path; check it with a run below the refresh rate as the control. |
| Regular ~1 s stalls | A present waits for a completion that comes only at a timeout. A classic cause is a pooled auto-reset event left armed from an earlier wait, so the next waiter wakes on someone else's signal and then sleeps out the deadline. |
| Steady but low distinct fps with high delivered fps | The window re-presents the same image. Count distinct frames, never delivered ones. |
| Holds of hundreds of ms on one setup only | Ask for exact host specs, refresh rate and resolution. Display EDID and refresh (UTM tells the guest the host refresh rate) change pacing. Reproduce at the reporter's mode before concluding anything. |
| Runs windowed at about half fps after a mode switch; benchmark says "windowed" | The foreground lock denied fullscreen. `install` runs `fglock`; benchmarks keep `fgforce` running. |

## Decision tree

1. Is it the guest display power-off? (input-click, re-sample)
2. Guest presented content? Use a guest frame check (UMD knob, see knobs.md)
   or a readback probe of the same content. If **no**: corruption.md.
3. Guest presented content but the window disagrees: which hop?
   - `-trace` QEMU events for virtio-gpu scanout and SPICE GL in the VM's
     QEMU arguments. They land in debug.log: compare the guest's
     set-scanout/flush cadence with the display's draw cadence.
   - Run the same workload below the refresh rate and at another resolution
     as controls.
4. Before theorizing about any hop: `git status` **every** tree in the path
   (QEMU, virglrenderer, the UTM checkout, CocoaSpice, the guest driver) and
   check that the embedded binaries match. An uncommitted edit in a display
   file outlived several sessions and produced exactly this symptom class.

## Dead ends, do not redo

- **Shortening the present wait to "fix" hitches.** It hides the ordering
  bug and makes the wait trace meaningless (a short poll reads 0 and looks
  like a dropped event).
- **Publishing the fence value earlier on the host** to fix a 1 s hitch. It
  measured zero benefit twice. A wake in a few microseconds followed by a
  long sleep is the *wrong frame's* wake, not a publish lag.
- **"Lost present" theories.** A lost present shows the previous frame, not
  black.
- **Blaming the backend whose fps is highest.** Flashes looked DXMT-only
  because DXMT ran above the refresh rate. Compare at equal fps.
