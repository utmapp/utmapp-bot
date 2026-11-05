"""UTM's UI on other macOS and iOS versions: run a UTM build in a macOS VM or an
iOS simulator (on this Mac or inside a macOS VM with an older Xcode), and
screenshot it.

  utmbot ui mac VM --app UTM.app [--out PNG] [--wait TEXT]
        copy the app into the VM (~/Applications), start it, wait for TEXT
        (default "Welcome to UTM") and save the VM's screen
  utmbot ui sim [--vm VM] --app UTM.app --runtime "iOS 16.4" [--device NAME | --family iPad|iPhone]
                [--bundle-id ID] [--out PNG] [--wait S]
        install the simulator app on a simulator of that runtime (made when
        there is none), launch it and save the simulator's screen; with --vm
        in that macOS VM (the app must have been built there: ui build-ios)
  utmbot ui build-ios VM --checkout UTM [--scheme iOS]
        copy a UTM checkout (with its sysroot-ios_simulator-arm64) into the
        VM and build the simulator app with the VM's Xcode; prints its path
  utmbot ui runtimes [--vm VM]      the simulator runtimes and devices
  utmbot ui record (--vm VM [--sim UDID] | --sim UDID) --seconds N [--out FILE]
        a video: of a simulator (simctl recordVideo, in the VM with --vm), or
        of a macOS VM's window (this Mac's screencapture -v; UTM is brought
        to the front).  It blocks for N seconds: run it in the background and
        act on the screen meanwhile
  utmbot ui publish PR FILE...      put screenshots and videos on the bot's fork
        (branch review-assets, pr-<PR>/) and print Markdown that shows them

--fixture (ui mac, ui sim) puts a sample VM (~/.utmapp-bot/ui/fixtures/Sample
VM.utm, no drives) into the app's documents first, for screens that need a VM
in the list.  --gui (ui sim --vm) shows the simulator in the VM's
Simulator.app, so `utmbot mac click/key/type VM` can tap and type in it.
  utmbot ui lldb VM [--process UTM] [-o CMD ...] [--port 5555]
        attach the host's LLDB to a process in the VM: this Mac's debugserver
        (macOS 13 and later guests) attaches there, and the host's lldb
        connects to it (macOS 12: the guest's own lldb, after mac clt); with -o, runs the commands, detaches and prints the
        output, else prints the lldb command line to connect yourself

A macOS app for another Mac must be ad-hoc signed (build-utm.sh --unsigned):
the VM entitlement profiles of a dev build tie it to this Mac.
"""

import argparse
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from .common import DATA, VM, die, ensure_dir, log, run, slug
from . import macvm, remote

SHOTS = DATA / "ui"


def _tar_to_vm(vm, src, dest_dir):
    """Copy a directory tree (an app bundle, symlinks kept) into the VM."""
    src = Path(src).resolve()
    remote.ensure_master(vm)
    d = remote.dest(vm)
    tar = subprocess.Popen(["tar", "-C", str(src.parent), "-cf", "-", src.name],
                           stdout=subprocess.PIPE)
    rc = subprocess.run(["ssh"] + remote.ssh_opts(vm) + [d, f"mkdir -p {dest_dir} && rm -rf "
                         f"{dest_dir}/{shlex.quote(src.name)} && tar -C {dest_dir} -xf -"],
                        stdin=tar.stdout, capture_output=True, timeout=3600)
    tar.wait()
    if rc.returncode != 0 or tar.returncode != 0:
        die(f"copying {src.name} into '{vm.name}' failed: {rc.stderr.decode()[-400:]}")


def _out(kind, name, out):
    if out:
        return Path(out)
    return ensure_dir(SHOTS / slug(name)) / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.png"


FIXTURE = DATA / "ui" / "fixtures" / "Sample VM.utm"


def _fixture():
    if not (FIXTURE / "config.plist").exists():
        die(f"no fixture VM at {FIXTURE}")
    return FIXTURE


def mac(name, app, out=None, wait="Welcome to UTM", fixture=False):
    app = Path(app).expanduser()
    if not (app / "Contents" / "MacOS").is_dir():
        die(f"{app} is not an app bundle")
    vm = macvm.up(name)
    _tar_to_vm(vm, app, "~/Applications")
    if fixture:
        # the sandbox container exists after the first launch; the sample VM goes into its
        # documents, which UTM lists
        docs = "~/Library/Containers/com.utmapp.UTM/Data/Documents"
        if remote.ssh(vm, f"test -d {docs} && echo yes", timeout=30)[1].strip() != "yes":
            remote.ssh(vm, f"open ~/Applications/{shlex.quote(app.name)}; sleep 10; pkill -x {shlex.quote(app.stem)}; sleep 2",
                       timeout=120)
        remote.ssh(vm, f"pkill -x {shlex.quote(app.stem)}; mkdir -p {docs}", timeout=30)
        _tar_to_vm(vm, _fixture(), docs)
    remote.ssh(vm, f"pkill -x {shlex.quote(app.stem)}; sleep 1; open ~/Applications/{shlex.quote(app.name)}",
               timeout=60)
    log(f"'{name}': {app.name} started; waiting for {wait!r}")
    t0 = time.time()
    while True:
        scr = macvm.screen(name)
        if scr.has(wait):
            break
        # first launch: UTM's What's New sheet (its Continue closes it)
        if scr.has("What's New") and macvm.click_text(name, "Continue", scr, exact=True):
            time.sleep(2)
            continue
        if time.time() - t0 > 180:
            die(f"'{name}': {wait!r} did not appear ({scr.png}): {scr.text()[:300]!r}")
        time.sleep(5)
    p = _out("mac", name, out)
    macvm.screen(name, p)
    ver = remote.ssh(vm, "sw_vers -productVersion", timeout=30)[1].strip()
    print(f"SHOT {p} (macOS {ver}, {scr.size[0]}x{scr.size[1]})")
    return p


# ---- simulators -------------------------------------------------------------------

def _sh(vm, cmd, timeout=600, check=True):
    if vm:
        rc, out = remote.ssh(vm, cmd, timeout=timeout)
    else:
        rc, out = run(["bash", "-c", cmd], timeout=timeout)
    if check and rc != 0:
        die(f"`{cmd[:100]}` failed ({rc}): {out[-500:]}")
    return out


def runtimes(vm=None):
    return json.loads(_sh(vm, "xcrun simctl list -j runtimes devices"))


def device_for(vm, runtime, device=None, family="iPad"):
    """The UDID of a simulator for the runtime ("iOS 16.4"), created when there is none."""
    data = runtimes(vm)
    rt = next((r for r in data["runtimes"] if r["name"] == runtime and r.get("isAvailable")), None)
    if not rt:
        have = ", ".join(r["name"] for r in data["runtimes"])
        die(f"no {runtime} simulator runtime{' in ' + repr(vm.name) if vm else ''} (have: {have})")
    types = [t for t in rt.get("supportedDeviceTypes", [])
             if (t["name"] == device if device else t.get("productFamily") == family)]
    if not device:
        # a plain model by default, not a special one such as iPhone Duo
        types = [t for t in types if re.fullmatch(r"(iPhone|iPad Pro|iPad|iPad Air|iPad mini) .*", t["name"])
                 and "Duo" not in t["name"]] or types
    names = {t["name"] for t in types}
    devs = [d for d in data["devices"].get(rt["identifier"], []) if d.get("isAvailable")
            and (d["name"] == device if device else d["name"].removesuffix(" utmbot") in names)]
    if devs:
        return devs[0]["udid"]
    t = next((t for t in types if device and t["name"] == device), types[-1] if types else None)
    if not t:
        die(f"{runtime} supports no {family} device type{' named ' + device if device else ''}")
    udid = _sh(vm, f"xcrun simctl create {shlex.quote(t['name'] + ' utmbot')} "
                   f"{t['identifier']} {rt['identifier']}").strip()
    log(f"created {t['name']} ({runtime}) {udid}")
    return udid


def bundle_id(app):
    import plistlib
    return plistlib.loads(Path(app, "Info.plist").read_bytes())["CFBundleIdentifier"]


def sim(app, runtime, device=None, vm_name=None, bid=None, out=None, wait=15, family="iPad",
        fixture=False, gui=False):
    vm = macvm.up(vm_name) if vm_name else None
    if vm and Path(app).expanduser().is_dir():
        # an app built on this Mac (Xcode here can target older iOS than its simulators):
        # copied into the VM
        local = Path(app).expanduser().resolve()
        _tar_to_vm(vm, local, "~/apps")
        app = f"{_sh(vm, 'echo $HOME').strip()}/apps/{local.name}"
    if vm:
        if _sh(vm, f"test -d {shlex.quote(app)} && echo yes", check=False).strip() != "yes":
            die(f"no {app} in '{vm_name}' (build it there: utmbot ui build-ios)")
        bid = bid or _sh(vm, f"defaults read {shlex.quote(app)}/Info.plist CFBundleIdentifier").strip()
    else:
        app = str(Path(app).expanduser().resolve())
        bid = bid or bundle_id(app)
    udid = device_for(vm, runtime, device, family)
    # one simulator at a time: several booted at once saturate a VM until simctl hangs
    booted = json.loads(_sh(vm, "xcrun simctl list -j devices booted"))
    for devs in booted.get("devices", {}).values():
        for d in devs:
            if d["udid"] != udid:
                _sh(vm, f"xcrun simctl shutdown {d['udid']}", check=False, timeout=300)
    _sh(vm, f"xcrun simctl boot {udid} 2>/dev/null; xcrun simctl bootstatus {udid} -b", timeout=900)
    _sh(vm, f"xcrun simctl install {udid} {shlex.quote(app)}", timeout=600)
    # UTM shows What's New on the first launch of a version: mark it seen, in
    # the app's own preferences (its data container)
    ver = _sh(vm, f"defaults read {shlex.quote(app)}/Info.plist CFBundleShortVersionString").strip()
    data = _sh(vm, f"xcrun simctl get_app_container {udid} {bid} data").strip()
    # (through the simulator's own defaults: its cfprefsd caches the file)
    _sh(vm, f"xcrun simctl spawn {udid} defaults write {shlex.quote(data)}/Library/Preferences/{bid} "
            f"ReleaseNotesLastVersion -string {shlex.quote(ver)}")
    if fixture:
        if vm:
            _tar_to_vm(vm, _fixture(), f"{data}/Documents")
        else:
            _sh(None, f"mkdir -p {shlex.quote(data)}/Documents && cp -R {shlex.quote(str(_fixture()))} "
                      f"{shlex.quote(data)}/Documents/")
    if gui:
        if not vm:
            die("--gui needs --vm: on this Mac nothing may click into Simulator.app")
        # Simulator.app on the VM's screen, showing this device: tap it with mac click VM …
        # (by its path: an Xcode copied in is not registered with LaunchServices)
        # (Xcode 27 keeps it in Contents/Applications, older ones in Contents/Developer/Applications)
        # Simulator.app (Contents/Developer/Applications), or DeviceHub.app from Xcode 27 on
        # (Contents/Applications), which shows the booted simulators
        _sh(vm, "d=$(xcode-select -p); for s in \"$d/Applications/Simulator.app\" \"$d/../Applications/DeviceHub.app\"; do "
                f"[ -d \"$s\" ] && open \"$s\" --args -CurrentDeviceUDID {udid} && break; done; sleep 8", timeout=120)
        # Device Hub ignores -CurrentDeviceUDID: pick the device in its sidebar
        dev_name = next((d["name"] for devs in runtimes(vm)["devices"].values() for d in devs
                         if d["udid"] == udid), None)
        scr = macvm.screen(vm_name)
        if dev_name and scr.has("Device Hub"):
            hits = [b for b in scr.find(dev_name, exact=True) if b.x < scr.size[0] // 2]
            if hits:
                macvm.click(vm_name, *hits[0].center)
                time.sleep(4)
    _sh(vm, f"xcrun simctl terminate {udid} {bid} 2>/dev/null; xcrun simctl launch {udid} {bid}", timeout=120)
    time.sleep(wait)
    where = vm_name or "host"
    p = _out(f"sim-{runtime.replace(' ', '')}", where, out)
    _sim_screenshot(vm, udid, p)
    print(f"SHOT {p} ({runtime} simulator {udid}{' in ' + vm_name if vm else ''})")
    print(f"UDID {udid}")
    return p


def _sim_screenshot(vm, udid, dest):
    """The simulator's screen.  A device with several displays (iPhone Duo: the outer one,
    black while it is open, and the inner one) gets the display with the most in it."""
    tmp = "/tmp/utmbot-sim.png"
    _sh(vm, f"xcrun simctl io {udid} screenshot {tmp} >/dev/null", timeout=120)
    size = int(_sh(vm, f"stat -f%z {tmp}").strip() or 0)
    if size < 120_000:  # a black or nearly empty display compresses to almost nothing
        best = _sh(vm, f"best=; bs=0; for d in $(xcrun simctl io {udid} enumerate 2>/dev/null | "
                       "awk '/UUID:/{u=$2} /Port Identifier: com.apple.framebuffer.display/{print u}'); do "
                       f"xcrun simctl io {udid} screenshot --display=$d /tmp/utmbot-d.png >/dev/null 2>&1 || continue; "
                       "z=$(stat -f%z /tmp/utmbot-d.png); [ $z -gt $bs ] && { bs=$z; cp /tmp/utmbot-d.png /tmp/utmbot-best.png; best=$d; }; "
                       "done; echo $bs", timeout=600, check=False).strip()
        if best.isdigit() and int(best) > size:
            tmp = "/tmp/utmbot-best.png"
    if vm:
        remote.get(vm, tmp, str(dest))
    else:
        Path(tmp).replace(dest)


def shot(vm_name, udid, out=None):
    """A screenshot of a booted simulator (after navigating in it)."""
    vm = macvm.up(vm_name) if vm_name else None
    p = _out("sim", vm_name or "host", out)
    _sim_screenshot(vm, udid, p)
    print(f"SHOT {p}")


def record(vm_name, udid, seconds, out=None):
    """A video of a simulator, or of a macOS VM's window, for `seconds`."""
    import signal as _signal
    where = vm_name or "host"
    p = Path(out) if out else ensure_dir(SHOTS / slug(where)) / f"rec-{time.strftime('%Y%m%d-%H%M%S')}.mov"
    if udid:
        vm = macvm.up(vm_name) if vm_name else None
        cmd = (f"xcrun simctl io {udid} recordVideo --force /tmp/utmbot-rec.mov & r=$!; sleep {int(seconds)}; "
               "kill -INT $r; wait $r")
        _sh(vm, cmd, timeout=int(seconds) + 120)
        if vm:
            remote.get(vm, "/tmp/utmbot-rec.mov", str(p))
        else:
            Path("/tmp/utmbot-rec.mov").rename(p)
    else:
        if not vm_name:
            die("ui record: --vm VM (a macOS VM's window) or --sim UDID")
        from .capture import tool
        from . import utm
        # the VM's window on this Mac's screen, in front (a covered window records what covers it)
        run(["osascript", "-e", f'tell application "{utm.app_path()}" to activate'], timeout=30)
        time.sleep(1)
        rows = [l.split("\t") for l in run([tool("winlist"), "UTM"])[1].splitlines() if l.count("\t") >= 3]
        win = next((r for r in rows if r[3] == vm_name or r[3].startswith(vm_name + " ")), None)
        if not win:
            die(f"no UTM window titled '{vm_name}' on this Mac's screen (is the VM running?)")
        rc, msg = run(["screencapture", "-v", "-x", "-V", str(int(seconds)), "-R", win[1], str(p)],
                      timeout=int(seconds) + 60)
        if rc != 0 or not p.exists():
            die(f"screencapture failed ({rc}): {msg[-300:]}")
    print(f"VIDEO {p}")
    return p


ASSETS_BRANCH = "review-assets"


def publish(pr, files, upstream="utmapp/UTM"):
    """Screenshots and videos on the bot's fork (branch review-assets, pr-<PR>/),
    for a PR comment; prints Markdown that shows each.  Pushed with gh's token
    over HTTPS, never forced."""
    import urllib.parse
    rc, login = run(["gh", "api", "user", "--jq", ".login"], timeout=60)
    login = login.strip()
    if rc != 0 or not login:
        die("GitHub: gh is not logged in (gh auth login as the bot account)")
    name = upstream.split("/")[1]
    rc, parent = run(["gh", "api", f"repos/{login}/{name}", "--jq", ".parent.full_name"], timeout=60)
    if rc != 0 or parent.strip().lower() != upstream.lower():
        die(f"{login}/{name} is not a fork of {upstream} (fork it first: gh repo fork {upstream} --clone=false)")
    url = f"https://github.com/{login}/{name}.git"
    cred = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
    work = ensure_dir(DATA / "ui" / "review-assets")

    def git(*args, check=True):
        p = subprocess.run(["git", *cred, "-C", str(work), *args], capture_output=True, text=True)
        if check and p.returncode:
            die(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[-300:]}")
        return p

    if not (work / ".git").exists():
        git("init", "-q")
    exists = subprocess.run(["git", *cred, "ls-remote", "--heads", url, ASSETS_BRANCH],
                            capture_output=True, text=True).stdout.strip()
    if exists:
        git("fetch", "-q", "--depth", "1", url, ASSETS_BRANCH)
        git("checkout", "-q", "-B", ASSETS_BRANCH, "FETCH_HEAD")
    else:
        git("checkout", "-q", "--orphan", ASSETS_BRANCH, check=False)
        git("rm", "-rq", "--cached", ".", check=False)
    dest = ensure_dir(work / f"pr-{pr}")
    names = []
    for f in files:
        f = Path(f).expanduser()
        if not f.is_file():
            die(f"no file {f}")
        target = dest / f.name
        target.write_bytes(f.read_bytes())
        names.append(f.name)
    git("add", f"pr-{pr}")
    git("-c", "user.name=" + login, "-c", f"user.email={login}@users.noreply.github.com",
        "commit", "-q", "-m", f"assets for {upstream}#{pr}", check=False)
    for attempt in range(3):
        p = git("push", "-q", url, f"HEAD:refs/heads/{ASSETS_BRANCH}", check=False)
        if p.returncode == 0:
            break
        # someone else published meanwhile: put these on top of theirs
        git("fetch", "-q", "--depth", "1", url, ASSETS_BRANCH)
        git("rebase", "-q", "FETCH_HEAD")
    else:
        die(f"pushing to {login}/{name}:{ASSETS_BRANCH} failed: {p.stderr.strip()[-300:]}")
    for n_ in names:
        link = f"https://github.com/{login}/{name}/raw/{ASSETS_BRANCH}/pr-{pr}/{urllib.parse.quote(n_)}"
        if n_.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
            print(f"![{n_}]({link})")
        else:
            print(f"[{n_}]({link})")


def build_ios(vm_name, checkout, scheme="iOS"):
    """Build the simulator app in the VM, from a copy of the checkout."""
    checkout = Path(checkout).expanduser().resolve()
    if not (checkout / "sysroot-ios_simulator-arm64").is_dir():
        die(f"{checkout} has no sysroot-ios_simulator-arm64 (UTM's Sysroot-ios_simulator-arm64 "
            "CI artifact, extracted into the checkout)")
    vm = macvm.up(vm_name)
    remote.ensure_master(vm)
    log(f"'{vm_name}': copying {checkout.name} (without .git, build output and macOS sysroots)")
    excl = [f"--exclude={e}" for e in (".git", "build", "DerivedData", "sysroot-macOS-*", "*.xcarchive")]
    tar = subprocess.Popen(["tar", "-C", str(checkout.parent), "-cf", "-", *excl, checkout.name],
                           stdout=subprocess.PIPE)
    rc = subprocess.run(["ssh"] + remote.ssh_opts(vm) + [remote.dest(vm), "mkdir -p ~/src && rm -rf ~/src/UTM && "
                         f"tar -C ~/src -xf - && mv ~/src/{shlex.quote(checkout.name)} ~/src/UTM 2>/dev/null; true"],
                        stdin=tar.stdout, capture_output=True, timeout=7200)
    tar.wait()
    if rc.returncode != 0:
        die(f"copying the checkout failed: {rc.stderr.decode()[-400:]}")
    log(f"'{vm_name}': building the {scheme} simulator app")
    dd = "~/src/dd"
    out = _sh(vm, f"cd ~/src/UTM && xcodebuild -project UTM.xcodeproj -scheme {scheme} "
                  f"-destination 'generic/platform=iOS Simulator' -configuration Debug -derivedDataPath {dd} "
                  # (a new Xcode asks before trusting package plugins and macros)
                  "-skipPackagePluginValidation -skipMacroValidation "
                  "ARCHS=arm64 ONLY_ACTIVE_ARCH=NO CODE_SIGN_IDENTITY=- CODE_SIGNING_REQUIRED=NO build "
                  "> ~/src/build.log 2>&1; echo rc=$?; grep -E 'error:|BUILD (SUCCEEDED|FAILED)' ~/src/build.log | tail -5",
              timeout=4 * 3600)
    if "rc=0" not in out:
        die(f"'{vm_name}': the simulator build failed:\n{out[-1500:]}")
    app = _sh(vm, f"ls -d {dd}/Build/Products/Debug-iphonesimulator/UTM.app").strip()
    print(f"APP {app}")
    return app


def lldb(vm_name, process="UTM", commands=(), port=5555):
    vm = macvm.up(vm_name)
    major = int(_sh(vm, "sw_vers -productVersion").strip().split(".")[0])
    if major < 13:
        # this Mac's debugserver needs macOS 13: the guest's own lldb, from the
        # Command Line Tools (utmbot mac clt, installed on request)
        if _sh(vm, "test -x \"$(xcode-select -p 2>/dev/null)/usr/bin/lldb\" && echo yes", check=False).strip() != "yes":
            die(f"'{vm_name}' runs macOS {major}: this Mac's debugserver needs macOS 13 or later, and "
                f"the guest has no lldb. Install the Command Line Tools there: utmbot mac clt '{vm_name}'")
        pid = _sh(vm, f"pgrep -x {shlex.quote(process)} | head -1", check=False).strip()
        if not pid:
            die(f"no process {process!r} in '{vm_name}'")
        if not commands:
            print(f"lldb is in '{vm_name}': ssh in (ssh -t, as utmbot --vm '{vm_name}' guest is batch "
                  f"only) and run sudo lldb -p {pid}")
            return
        args = " ".join(f"-o {shlex.quote(c)}" for c in commands)
        # (as root: a debugger over SSH cannot ask for permission to attach)
        print(_sh(vm, f"sudo lldb -p {pid} --batch {args} -o detach", timeout=600, check=False))
        return
    dev = run(["xcode-select", "-p"])[1].strip()
    ds = Path(dev).parent / "SharedFrameworks/LLDB.framework/Versions/A/Resources/debugserver"
    if not ds.exists():
        die(f"no debugserver in {ds.parent} (is Xcode selected with xcode-select?)")
    remote.put(vm, ds, "debugserver")
    pid = _sh(vm, f"pgrep -x {shlex.quote(process)} | head -1", check=False).strip()
    if not pid:
        die(f"no process {process!r} in '{vm_name}' (start it first, e.g. utmbot ui mac)")
    _sh(vm, f"sudo pkill -f '[d]ebugserver 0.0.0.0:{port}'; "
            f"(sudo nohup ~/debugserver 0.0.0.0:{port} --attach={pid} > /tmp/debugserver.log 2>&1 &); sleep 3; "
            f"pgrep -f '[d]ebugserver 0.0.0.0:{port}' >/dev/null || (cat /tmp/debugserver.log; exit 1)")
    connect = f"process connect connect://{vm.host()}:{port}"
    if not commands:
        print(f"debugserver attached to {process} (pid {pid}) in '{vm_name}'. Connect with:\n"
              f"  lldb -o {shlex.quote(connect)}")
        return
    args = ["lldb", "--batch", "-o", connect]
    for c in commands:
        args += ["-o", c]
    args += ["-o", "detach"]
    rc, out = run(args, timeout=600)
    print(out)


def main(argv):
    p = argparse.ArgumentParser(prog="utmbot ui", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("mac"); s.add_argument("vm"); s.add_argument("--app", required=True)
    s.add_argument("--out"); s.add_argument("--wait", default="Welcome to UTM")
    s.add_argument("--fixture", action="store_true")
    s = sub.add_parser("sim"); s.add_argument("--vm"); s.add_argument("--app", required=True)
    s.add_argument("--runtime", required=True); s.add_argument("--device")
    s.add_argument("--family", default="iPad", choices=["iPad", "iPhone"],
                   help="device family when --device is not given (iPad: the split view with the welcome pane)")
    s.add_argument("--bundle-id"); s.add_argument("--out"); s.add_argument("--wait", type=int, default=15)
    s.add_argument("--fixture", action="store_true"); s.add_argument("--gui", action="store_true")
    s = sub.add_parser("build-ios"); s.add_argument("vm"); s.add_argument("--checkout", required=True)
    s.add_argument("--scheme", default="iOS")
    s = sub.add_parser("runtimes"); s.add_argument("--vm")
    s = sub.add_parser("shot"); s.add_argument("--vm"); s.add_argument("--sim", required=True)
    s.add_argument("--out")
    s = sub.add_parser("record"); s.add_argument("--vm"); s.add_argument("--sim")
    s.add_argument("--seconds", type=int, default=10); s.add_argument("--out")
    s = sub.add_parser("publish"); s.add_argument("pr", type=int); s.add_argument("files", nargs="+")
    s.add_argument("--upstream", default="utmapp/UTM")
    s = sub.add_parser("lldb"); s.add_argument("vm"); s.add_argument("--process", default="UTM")
    s.add_argument("-o", dest="commands", action="append", default=[]); s.add_argument("--port", type=int, default=5555)
    a = p.parse_args(argv)
    if a.cmd == "mac":
        mac(a.vm, a.app, a.out, a.wait, a.fixture)
    elif a.cmd == "sim":
        sim(a.app, a.runtime, a.device, a.vm, a.bundle_id, a.out, a.wait, a.family, a.fixture, a.gui)
    elif a.cmd == "build-ios":
        build_ios(a.vm, a.checkout, a.scheme)
    elif a.cmd == "shot":
        shot(a.vm, a.sim, a.out)
    elif a.cmd == "record":
        record(a.vm, a.sim, a.seconds, a.out)
    elif a.cmd == "publish":
        publish(a.pr, a.files, a.upstream)
    elif a.cmd == "lldb":
        lldb(a.vm, a.process, a.commands, a.port)
    elif a.cmd == "runtimes":
        vm = macvm.up(a.vm) if a.vm else None
        data = runtimes(vm)
        for r in data["runtimes"]:
            n = len([d for d in data["devices"].get(r["identifier"], []) if d.get("isAvailable")])
            fams = sorted({t.get("productFamily", "?") for t in r.get("supportedDeviceTypes", [])})
            special = [t["name"] for t in r.get("supportedDeviceTypes", []) if "Duo" in t["name"]]
            print(f"{r['name']:12s} {'available' if r.get('isAvailable') else 'UNAVAILABLE'}  {n} device(s); "
                  f"families: {', '.join(fams) or '-'}{'; ' + ', '.join(special) if special else ''}")
    return 0
