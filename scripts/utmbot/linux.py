"""Linux guest operations (Venus Vulkan, and Neptune D3D under Wine + FEX).

  * GUI programs must be fully detached (setsid, stdio to a file) or they die
    with the SSH session; a program's block-buffered output is lost if it is
    killed, so run it straight to a file, never through `tee`.
  * The guest's graphics drivers for Windows programs run x86_64 under FEX:
    anything built for that path must be x86_64.
  * The first Wine run after a boot is ~30 s slower (wineserver, .NET).
"""

import re
import shlex
import time

from . import remote
from .common import Session
from .wait import until

# X11 clients (xcb WSI, winex11.drv) also need XAUTHORITY: GNOME's Xwayland
# keeps its auth file in the runtime dir, and starts (and writes it) only when
# a first X client connects, so poke :0 once before looking for it.
GUI_ENV = ("XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0} "
           "DISPLAY=${DISPLAY:-:0} "
           "XAUTHORITY=${XAUTHORITY:-$( (timeout 3 xset -q >/dev/null 2>&1 || true); "
           "ls -t /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1)}")


def launch(vm, cmd, name=None, env=None):
    name = name or f"run{int(time.time()) % 100000}"
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    out = f"~/utmbot/{name}.log"
    remote.ssh(vm, f"mkdir -p ~/utmbot; {GUI_ENV} {envs} setsid bash -c "
                   f"{shlex.quote(f'exec {cmd}')} > {out} 2>&1 < /dev/null & disown; echo started",
               check=True)
    Session(vm)["last_launch"] = {"out": out, "cmd": cmd}
    print(f"LAUNCHED {cmd} -> {out}")


def test(vm, cmd, timeout_s=600, env=None):
    name = f"t{int(time.time()) % 100000}"
    envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    out = f"~/utmbot/{name}.log"
    remote.ssh(vm, f"mkdir -p ~/utmbot; {GUI_ENV} {envs} setsid bash -c "
                   f"{shlex.quote('( ' + cmd + ' ); echo __UTMBOT_EXIT__ $?')} > {out} 2>&1 < /dev/null & "
                   "disown", check=True)

    def check():
        rc, o = remote.ssh(vm, f"tail -c 2000 {out}")
        m = re.search(r"__UTMBOT_EXIT__ (\d+)", o)
        return bool(m), (f"exit {m[1]}" if m else (o.strip().splitlines() or ["…"])[-1][:100])
    o, obs = until(check, timeout_s, cmd.split()[0], every=20, vm=vm, interval=3)
    rc, text = remote.ssh(vm, f"cat {out}")
    m = re.search(r"__UTMBOT_EXIT__ (\d+)", text)
    print(re.sub(r"__UTMBOT_EXIT__.*", "", text).rstrip())
    (Session(vm).run_dir() / f"test-{name}.txt").write_text(text)
    print(f"TEST {cmd.split()[0]}: {o} exit={m[1] if m else None}")
    return o, (int(m[1]) if m else None), text


def verify(vm):
    """Venus: the guest's Vulkan device must be the Venus GPU, not llvmpipe."""
    rc, out = remote.ssh(vm, "vulkaninfo --summary 2>&1", timeout=120)
    devs = re.findall(r"deviceName\s*=\s*(.*)", out)
    drivers = re.findall(r"driverID\s*=\s*(\S+)", out)
    for d, i in zip(devs, drivers):
        print(f"vulkan device: {d.strip()} ({i})")
    ok = any("VENUS" in i for i in drivers)
    if not ok:
        print("no Venus device: only llvmpipe means the host did not answer the Venus capset "
              "(render server not started, or a capset with wire format 0)")
    print(f"VERIFY {'PASS' if ok else 'FAIL'}")
    return ok
