"""SSH/SCP to a VM, with one multiplexed connection per VM.

Rapid reconnects get throttled (ssh exits 255), so every call reuses one
master connection.  The master is started on its own with all of its stdio on
/dev/null: a master that `ControlMaster=auto` forks from an ordinary call
inherits that call's output pipe and holds it open for ControlPersist, so
whoever reads the output (a subprocess capture, `| tail`) hangs for ten
minutes.  Windows tools print in the OEM code page, so output is decoded with
errors=replace.
"""

import os
import subprocess

from .common import die, ensure_dir, run


def _ctl():
    # a Unix socket path is limited to 104 bytes, so the control sockets live
    # in a short directory, not under the data dir
    ctl = ensure_dir(f"/tmp/utmbot-{os.getuid()}")
    os.chmod(ctl, 0o700)
    return f"{ctl}/%C"


def ssh_opts(vm, timeout=8, master=False):
    """Options for ssh and scp.  Host keys are kept per VM under its name
    (HostKeyAlias), so a forwarded port that another VM used before, or a
    DHCP address that changed, never trips a host key mismatch."""
    opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={vm.known_hosts}", "-o", f"HostKeyAlias={vm.slug}",
            "-o", f"Port={vm.ssh_port() or 22}",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
            "-o", f"ControlPath={_ctl()}", "-o", "LogLevel=ERROR",
            "-o", "ControlMaster=" + ("yes" if master else "no")]
    if master:
        opts += ["-o", "ControlPersist=600"]
    k = vm.key()
    if k:
        opts = ["-i", k] + opts
    return opts


def ensure_master(vm, timeout=8):
    """Start the shared connection if it is not up; False when the guest is unreachable."""
    if not vm.host():
        return False
    d = dest(vm)
    if subprocess.run(["ssh", "-o", f"ControlPath={_ctl()}", "-o", f"Port={vm.ssh_port() or 22}",
                       "-O", "check", d],
                      stdin=subprocess.DEVNULL, capture_output=True, timeout=10).returncode == 0:
        return True
    try:
        rc = subprocess.run(["ssh", "-f", "-N"] + ssh_opts(vm, timeout, master=True) + [d],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout + 15).returncode
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def dest(vm):
    host = vm.host()
    if not host:
        die(f"no address for '{vm.name}' (MAC {vm.mac}) in the host's DHCP leases; "
            "is it running?")
    return f"{vm.user}@{host}"


def ssh(vm, cmd, timeout=120, check=False):
    """Run cmd on the VM (cmd.exe on Windows, the login shell on Linux)."""
    d = dest(vm)
    ensure_master(vm)
    rc, out = run(["ssh"] + ssh_opts(vm) + [d, cmd], timeout=timeout)
    out = out.replace("\r", "")
    if check and rc != 0:
        die(f"guest command failed ({rc}): {cmd}\n{out.strip()[-1500:]}")
    return rc, out


def ps1(vm, script, timeout=120, check=False):
    """Run a PowerShell snippet on a Windows guest (passed base64, so no quoting)."""
    import base64
    # progress records would otherwise arrive on stderr as CLIXML
    script = "$ProgressPreference = 'SilentlyContinue'\n" + script
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return ssh(vm, f"powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                   f"-EncodedCommand {enc}", timeout=timeout, check=check)


def put(vm, local, remote, timeout=600, recursive=False):
    """Copy to the guest; remote uses forward slashes (C:/utmbot/x)."""
    ensure_master(vm)
    cmd = ["scp", "-q"] + (["-r"] if recursive else []) + ssh_opts(vm) + \
        [str(local), f"{dest(vm)}:{remote}"]
    rc, out = run(cmd, timeout=timeout)
    if rc != 0:
        die(f"scp {local} -> {remote} failed: {out.strip()[-500:]}")


def get(vm, remote, local, timeout=600, recursive=False, check=True):
    ensure_master(vm)
    cmd = ["scp", "-q"] + (["-r"] if recursive else []) + ssh_opts(vm) + \
        [f"{dest(vm)}:{remote}", str(local)]
    rc, out = run(cmd, timeout=timeout)
    if rc != 0 and check:
        die(f"scp {remote} -> {local} failed: {out.strip()[-500:]}")
    return rc == 0


def reachable(vm, timeout=6):
    if not vm.host(refresh=True) or not ensure_master(vm, timeout):
        return False
    rc, out = run(["ssh"] + ssh_opts(vm, timeout) + [dest(vm), "echo utmbot-ok"],
                  timeout=timeout + 10)
    return "utmbot-ok" in out


def close(vm):
    """Drop the multiplexed connection (after the guest rebooted or stopped)."""
    if vm.host():
        subprocess.run(["ssh", "-o", f"ControlPath={_ctl()}", "-o", f"Port={vm.ssh_port() or 22}",
                        "-O", "exit", dest(vm)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=10)


def boot_id(vm):
    """An id that changes on every boot, to tell a new boot from a dying one."""
    if vm.is_windows:
        rc, out = ps1(vm, "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime"
                          ".ToFileTimeUtc()", timeout=40)
        import re
        m = re.search(r"\b(\d{15,})\b", out)
        return m[1] if m else None
    rc, out = ssh(vm, "cat /proc/sys/kernel/random/boot_id", timeout=20)
    return out.strip() if rc == 0 else None
