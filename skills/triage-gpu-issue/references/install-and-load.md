# Driver install and load failures

| Fingerprint | Cause |
|---|---|
| `D3D11CreateDevice` returns S_OK but the driver logs nothing | The app landed on **WARP** (Microsoft Basic Render Driver, vendor `0x1414`). `NULL` + `HARDWARE` does that when our adapter is missing or the call runs outside the console session. Test with `adaptercheck` (explicit vendor `0x1af4`). |
| `virtgpu: no PCI device matching …` | Run over SSH: sshd's window station has no display devices. Use `$UB test`. |
| `KMD reports no 3D/shmem support` → `0x80004005` | The host-visible memory window (the virtio-gpu "hostmem" shared-memory BAR) is missing. Check `hostmem=` in the QEMU arguments (`run_dir/qemu-cmdline.txt`) and the device model. After install the desktop also goes black, because the driver owns scanout but has nothing to present from. |
| `wire format mismatch host=… guest=…` → `failed to create renderer` | Guest driver and host protocol versions differ. Use a driver built for this UTM: a released UTM wants the matching guest-tools driver; a dev host wants a dev driver. |
| `ERROR_BAD_EXE_FORMAT` (`193`/`0x800700C1`), `0xC000007B` | An x64-emulated caller (an x64 exe, .NET/AnyCPU, PowerShell `Add-Type`) loading the native arm64 driver directly. Not a driver bug. A successful `NtCreateSection(SEC_IMAGE)` proves the PE itself is valid. |
| pnputil "up-to-date" / nothing installed | An equal or newer driver version is already bound. Start from a fresh disposable boot. |
| pnputil "catalog … hash" / "no signature" | Files changed after signing, or a partial push. Push the whole signed package. |
| "certificate chain … not trusted" | The test certificate must be in **both** Root and TrustedPublisher (`install` does this from the catalog's signer). |
| Device `CM_PROB_FAILED_INSTALL` after a build that "succeeded" | The build failed later in the sequence, and an earlier project's `Build succeeded` hid it. Or an incremental object went stale. Compare file times **on the build VM** with the source; copied files carry meaningless times. |
| Changes have no effect | The build copied the source from somewhere else and you edited the wrong copy, or a stale object survived a rebase. Put a unique string in the change and look for it in the installed DLL. |
| 32-bit apps run on WARP or fail | The package has no 32-bit (WOW64) user-mode driver, or the driver advertises 64-bit only. |
| Console tasks hang right after install | Windows Terminal renders through D3D, so a broken driver hangs every console. `vm up` makes conhost the console host. |
| Display wedged after reinstall | Reinstall into a boot where the driver already ran. Always `vm down` + `vm up` before `install`. |
| `LNK1201` writing the KMD's `.pdb` on the build VM | kd holds the PDB open. `$UB kd stop` first. |
| Inf2Cat `22.9.1 … missing` | A file listed in the INF's `CopyFiles` is not in the project's package file list. |
| KMD loads, but its `DbgPrint` output is missing from kd.log | The KMD's debug verbosity is a registry value read at load. Set it before installing. |

To watch a driver load live, attach kd (`/utmapp-bot:kernel-debug`) before
`install`: the KMD's `DriverEntry`/`StartDevice` prints stream to kd.log.

To find which user-mode DLL fails to load, set the image's IFEO
`GlobalFlag=2` (show loader snaps) for the exe. Loader messages then stream
to kd with the exact missing DLL and NTSTATUS:

```
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\<exe>" /v GlobalFlag /t REG_DWORD /d 2 /f
```
