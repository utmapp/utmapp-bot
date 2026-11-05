"""macOS VMs (UTM's Apple backend): create, install, set up, and drive the screen.

  utmbot mac create NAME (--ipsw FILE | --latest) [--cpus N] [--memory MiB] [--disk-gb N]
  utmbot mac install NAME            install macOS (UTM downloads the latest IPSW
                                     for a VM made with --latest); waits
  utmbot mac setup NAME [--user U] [--password P] [--full-name F]
                                     first boot to a desktop: the unattended
                                     provisioning of macOS 27 on a macOS 27 host,
                                     else Setup Assistant driven through the screen
  utmbot mac baseline NAME [--key PUB]
                                     on the desktop: Remote Login, no display sleep,
                                     no screen saver or lock, the host key; registers
                                     the VM for ssh
  utmbot mac provision NAME (--ipsw FILE | --latest) [--cpus N] [--memory MiB] [--disk-gb N]
                     [--user U] [--password P] [--key PUB]
                                     all of the above, resuming at the stage that
                                     did not finish, then shut down and snapshot
                                     the result as macos-baseline
  utmbot mac up NAME | down NAME     start (waits for SSH) / shut down cleanly
  utmbot mac clt NAME                Apple's Command Line Tools (lldb, clang, git) in the VM,
                                     through softwareupdate, no dialog: only when you
                                     debug on a guest this Mac's debugserver cannot run
                                     on (macOS 12); not part of provisioning
  utmbot mac xcode NAME --xip FILE|Xcode.app [--ios VER ...] [--platform visionOS] [--ios-dmg FILE ...] [--snapshot NAME]
                                     install Xcode from a XIP (and iOS simulator
                                     runtimes) in a provisioned VM, then snapshot
                                     it (default xcode-<version>).  A runtime comes
                                     from Xcode (xcodebuild -downloadPlatform
                                     -buildVersion: iOS 16 and later), else Apple's
                                     public simulator index (iOS 15 and older), or a
                                     DMG given with --ios-dmg (offline)
  utmbot mac screen NAME [--out PNG] [--raw]
                                     capture the display; prints its text with the
                                     centre of each box (x,y in capture pixels)
  utmbot mac click NAME (TEXT [--nth N] [--below Y] | --at X Y) [--button right]
  utmbot mac wait NAME TEXT [--timeout S]      until TEXT is on screen
  utmbot mac key NAME KEY[+KEY...] ...         e.g. return, tab, cmd+space, cmd+q, down
  utmbot mac type NAME TEXT                    ASCII text

The display is driven through UTM's scripting (capture screen, input mouse
click, input keystroke, input scan code), which on the Apple backend needs the
VM's window open: UTM opens it when the VM starts.  Coordinates are in the
pixels of the capture.  Text is found with the Vision framework (swift/ocr.swift).

The VM's window size decides the guest's resolution (dynamic resolution), and
so where things are: always find them on a fresh capture.
"""

import argparse
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .common import DATA, Fail, die, ensure_dir, log, run, slug
from . import utm

DEFAULT_USER = "utm"
DEFAULT_PASSWORD = "utmutm"  # macOS wants four characters or more
SHOTS = DATA / "macvm"

# PC AT set-1 make codes (UTM translates them to Mac keys); E0xx are extended
KEYS = {"esc": 0x01, "escape": 0x01, "backspace": 0x0E, "delete": 0x0E, "tab": 0x0F,
        "return": 0x1C, "enter": 0x1C, "ctrl": 0x1D, "control": 0x1D, "shift": 0x2A,
        "option": 0x38, "alt": 0x38, "space": 0x39, "capslock": 0x3A,
        "cmd": 0xE05B, "command": 0xE05B, "up": 0xE048, "down": 0xE050, "left": 0xE04B,
        "right": 0xE04D, "home": 0xE047, "end": 0xE04F, "pageup": 0xE049, "pagedown": 0xE051,
        "fdelete": 0xE053, "minus": 0x0C, "equals": 0x0D, "comma": 0x33, "period": 0x34,
        "slash": 0x35, "semicolon": 0x27, "quote": 0x28, "backquote": 0x29}
KEYS.update({str(d): 2 + i for i, d in enumerate("1234567890")})
for _row, _first in (("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
    KEYS.update({c: _first + i for i, c in enumerate(_row)})
KEYS.update({f"f{i}": 0x3A + i for i in range(1, 11)})
KEYS.update({"f11": 0x57, "f12": 0x58})


def _break(code):
    return (code | 0x80) if code < 0x100 else (0xE000 | (code & 0xFF) | 0x80)


# ---- the screen ----------------------------------------------------------------

class Box:
    __slots__ = ("x", "y", "w", "h", "conf", "text", "words")

    def __init__(self, x, y, w, h, conf, text, words=None):
        self.x, self.y, self.w, self.h, self.conf, self.text = x, y, w, h, conf, text
        self.words = words or []  # (start, end, x, w) of each word, when the box holds several

    def part(self, start, end):
        """The box narrowed to characters start..end of its text: the words
        there (from OCR), or, without them, a share by character count."""
        if start <= 0 and end >= len(self.text):
            return self
        ws = [w for w in self.words if w[0] < end and w[1] > start]
        if ws:
            x0 = min(w[2] for w in ws)
            x1 = max(w[2] + w[3] for w in ws)
        else:
            n = max(len(self.text), 1)
            x0 = self.x + self.w * start // n
            x1 = self.x + self.w * end // n
        return Box(x0, self.y, max(x1 - x0, 1), self.h, self.conf, self.text[start:end])

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2

    def __repr__(self):
        cx, cy = self.center
        return f"{cx},{cy} {self.text}"


class Screen:
    def __init__(self, png, boxes, size):
        self.png, self.boxes, self.size = png, boxes, size

    def find(self, text, exact=False, below=None, above=None):
        """Boxes whose text matches (case-insensitive, typographic apostrophes as
        plain ones; exact, or as whole words anywhere in the box: "Continue"
        matches "Continue" but not "Discontinue")."""
        def norm(x):  # keeps every character where it was (offsets into the box's text)
            return x.lower().replace("\u2019", "'").replace("\u2018", "'")
        t = norm(text).strip()
        out = []
        for b in self.boxes:
            if not ((below is None or b.y >= below) and (above is None or b.y <= above)):
                continue
            s = norm(b.text)
            if s.strip() == t:
                out.append(b)
            elif not exact:
                # a whole-word match inside a longer box (a menu bar read as one
                # line): the box narrowed to it, so a click lands on the word
                m = re.search(rf"(?<!\w){re.escape(t)}(?!\w)", s)
                if m:
                    out.append(b.part(m.start(), m.end()))
        return out

    def has(self, *texts, exact=False):
        return any(self.find(t, exact=exact) for t in texts)

    def text(self):
        return "\n".join(b.text for b in self.boxes)


def _shot_path(name):
    return ensure_dir(SHOTS / slug(name)) / "screen.png"


def screen(name, out=None):
    """Capture the display and read its text."""
    from .capture import tool
    png = Path(out).expanduser().resolve() if out else _shot_path(name)
    ensure_dir(png.parent)  # UTM's sandbox can write the file, not make its folder
    rc, msg = utm.osa_run("capture-screen", name, str(png), timeout=60)
    if rc != 0:
        if "not supported" in msg.lower() or "-10000" in msg:
            die(f"{utm.app_path()} cannot capture a macOS VM's screen: it is too old for this "
                "plugin. Update UTM")
        die(f"capture screen of '{name}' failed: {msg} (is the VM running with its window open?)")
    rc, txt = run([tool("ocr"), str(png)], timeout=120)
    if rc != 0:
        die(f"ocr failed: {txt}")
    boxes = []
    for line in txt.splitlines():
        parts = line.split("\t")
        if len(parts) >= 6:
            words = []
            for w in (parts[6].split() if len(parts) > 6 else []):
                m = re.fullmatch(r"(\d+)-(\d+):(-?\d+):(\d+)", w)
                if m:
                    words.append(tuple(int(g) for g in m.groups()))
            boxes.append(Box(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]),
                             float(parts[4]), parts[5], words))
    rc, dims = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(png)], timeout=30)
    w = int(re.search(r"pixelWidth: (\d+)", dims).group(1)) if "pixelWidth" in dims else 0
    h = int(re.search(r"pixelHeight: (\d+)", dims).group(1)) if "pixelHeight" in dims else 0
    return Screen(png, boxes, (w, h))


def click(name, x, y, button="left"):
    utm.osa("input-click", name, int(x), int(y), button)


def click_text(name, text, scr=None, nth=0, exact=False, below=None, above=None):
    """Click a text on screen; False when it is not there."""
    scr = scr or screen(name)
    hits = scr.find(text, exact=exact, below=below, above=above)
    if len(hits) <= nth:
        return False
    click(name, *hits[nth].center)
    return True


def keys(name, combo):
    """Press a key or a chord (cmd+space): every key down in order, then up in reverse."""
    codes = []
    for k in combo.lower().split("+"):
        if k not in KEYS:
            die(f"unknown key {k!r} (known: {', '.join(sorted(KEYS))})")
        codes.append(KEYS[k])
    seq = codes + [_break(c) for c in reversed(codes)]
    utm.osa("input-scancodes", name, *seq)


def type_text(name, text):
    if not text:
        return
    utm.osa("input-text", name, text)


def wait_text(name, *texts, timeout=300, every=5, exact=False):
    """The first screen that shows one of texts; dies after timeout."""
    t0 = time.time()
    while True:
        scr = screen(name)
        if scr.has(*texts, exact=exact):
            return scr
        if time.time() - t0 > timeout:
            die(f"'{name}': none of {texts} on screen after {timeout} s; last screen "
                f"{scr.png}: {scr.text()[:400]!r}")
        time.sleep(every)


# ---- create and install ----------------------------------------------------------

def create(name, ipsw=None, latest=False, cpus=4, memory=8192, disk_gb=96):
    utm.ensure_app()
    if utm.osa("exists", name) == "true":
        die(f"UTM already has a VM named '{name}'")
    if not latest:
        if not ipsw or not Path(ipsw).expanduser().is_file():
            die(f"no IPSW at {ipsw}")
        ipsw = str(Path(ipsw).expanduser().resolve())
    out = utm.osa("create-mac", name, ipsw or "latest", memory, cpus, disk_gb * 1024, timeout=600)
    log(f"created '{name}' ({out})")


def install(name, timeout=4 * 3600):
    utm.ensure_app()
    utm.osa("install-mac", name, timeout=600)
    t0, last = time.time(), -1.0
    while True:
        rc, out = utm.osa_run("query-install", name, timeout=120)
        if rc != 0:
            die(f"installing macOS in '{name}' failed: {out}")
        m = re.search(r"installing:(true|false), progress:([0-9.]+)", out)
        if not m:
            die(f"unexpected install status: {out}")
        prog = float(m.group(2))
        if m.group(1) == "false":
            log(f"'{name}': macOS installed")
            return
        if prog - last >= 0.1 or (prog == 0 and last < 0):
            latest = (SHOTS / slug(name) / "version").exists() and \
                (SHOTS / slug(name) / "version").read_text().strip() == "latest"
            log(f"'{name}': install {prog:.0%}" + (" (UTM downloads the latest IPSW first)"
                                                   if prog == 0 and latest else ""))
            last = prog
        if time.time() - t0 > timeout:
            die(f"installing macOS in '{name}' took over {timeout // 60} min")
        time.sleep(30)


# ---- first boot -----------------------------------------------------------------

def guest_major(name):
    """The guest's macOS major version, from the IPSW the VM was made from (recorded at
    create; "latest" is the newest this host supports, its own major), else None."""
    st = SHOTS / slug(name) / "version"
    if not st.exists():
        return None
    v = st.read_text().strip()
    return host_major() if v == "latest" else int(v.split(".")[0])


def record_version(name, ipsw=None, latest=False):
    v = "latest" if latest else None
    if ipsw:
        m = re.search(r"_(\d+(?:\.\d+)*)_", Path(ipsw).name)
        v = m.group(1) if m else None
    if v:
        (ensure_dir(SHOTS / slug(name)) / "version").write_text(v)


def host_major():
    return int(run(["sw_vers", "-productVersion"])[1].strip().split(".")[0])


def setup(name, user=DEFAULT_USER, password=DEFAULT_PASSWORD, full_name=None, timeout=1800):
    """From the first boot after install to the user's desktop."""
    utm.ensure_app()
    major = guest_major(name)
    if major and major >= 27 and host_major() >= 27:
        utm.osa("start-provision", name, user, password, full_name or user, timeout=600)
        log(f"'{name}': unattended setup (macOS {major}); waiting for the desktop")
        t0 = time.time()
        while not _desktop(screen(name)):
            if time.time() - t0 > timeout:
                die(f"'{name}': no desktop {timeout // 60} min after the unattended setup started")
            time.sleep(15)
        log(f"'{name}': at the desktop")
        return
    if utm.osa("status", name) != "started":
        utm.osa("start", name)
    assistant(name, user, password, full_name or user, timeout=timeout)


# Setup Assistant, pane by pane.  Each screen is matched against the panes in
# order: the first whose `when` texts are all on screen acts.  It is data so a
# pane that a version adds, renames or moves needs a line here, not new code.
#
# act: ("click", text) | ("type", text) | ("key", combo) | ("account",) |
#      ("hello",) | ("pause", s); a list runs in order; ("click", text) may
#      give alternatives as a tuple.
PANES = [
    # the account form: its fields are filled by tabbing from the first one
    {"when": ["passwords don't match"], "act": [("click", ("Go Back", "OK"))]},
    # the login window, after a restart before automatic login is on
    {"when": ["Enter Password"], "act": [("login",)]},
    {"when": ["Create a Computer Account"], "act": [("account",)]},
    {"when": ["Create a Mac Account"], "act": [("account",)]},
    # pick the first language (English) on the Language pane
    {"when": ["Language", "English"], "unless": ["Preferred Languages", "Written and Spoken"],
     "act": [("key", "return")]},
    {"when": ["Select Your Country or Region"],
     "act": [("select", "United States"), ("pause", 1), ("click", ("Continue",))]},
    {"when": ["Migration Assistant"], "act": [("click", ("Not Now",))]},
    # "Transfer Your Data to This Mac" (15+): the default is a migration, so
    # Continue only after "Set up as new" is picked (no click, no Continue)
    {"when": ["Transfer Your Data", "Set up as new"],
     "act": [("click", ("Set up as new",)), ("pause", 1), ("click", ("Continue",))]},
    {"when": ["Transfer Your Data"], "act": [("click", ("Not Now",))]},
    {"when": ["Sign In with Your Apple ID"], "act": [("click", ("Set Up Later",))]},
    # 26: "Set Up Later" is in the "Other Sign-In Options" menu
    {"when": ["Sign In to Your Apple Account"],
     "act": [("click", ("Set Up Later", "Sign in Later in Settings", "Other Sign-In Options"))]},
    {"when": ["Are you sure you want to skip"], "act": [("click", ("Skip",))]},
    {"when": ["Terms and Conditions"], "act": [("click", ("Agree",))]},
    # 26: who uses this Mac
    {"when": ["Age Range", "Adult"], "act": [("click", ("Adult",)), ("pause", 1),
                                             ("click", ("Continue",))]},
    {"when": ["I have read and agree"], "act": [("click", ("Agree",))]},
    {"when": ["Enable Location Services"], "act": [("click", ("Continue",))]},
    # "Are you sure you don't want to use Location Services?" (two lines)
    {"when": ["Don't Use"], "act": [("click", ("Don't Use",))]},
    {"when": ["Screen Time"], "act": [("click", ("Set Up Later", "Continue"))]},
    {"when": ["Siri"], "unless": ["Analytics"],
     "act": [("click", ("Enable Ask Siri", "Enable Siri")), ("pause", 1),
             ("click", ("Continue",))]},
    {"when": ["Accessibility"], "act": [("click", ("Not Now", "Continue"))]},
    # 26: "Mac Data Will Not Be Securely Encrypted", after FileVault's Not Now
    {"when": ["Securely Encrypted"], "act": [("click", ("Continue",))]},
    {"when": ["FileVault"], "act": [("click", ("Not Now", "Continue"))]},
    {"when": ["Touch ID"], "act": [("click", ("Set Up Later", "Continue"))]},
    {"when": ["Update Mac Automatically"], "act": [("click", ("Only Download Automatically",
                                                             "Continue"))]},
    {"when": ["Welcome to Mac"], "act": [("click", ("Continue",))]},
    # the opening greeting: its only control is the arrow above "Get Started"
    {"when": [], "act": [("hello",)], "hello": True},
    # anything else with a way forward
    {"when": ["Continue"], "act": [("click", ("Continue",))]},
    {"when": ["Get Started"], "act": [("click", ("Get Started",))]},
    {"when": ["Not Now"], "act": [("click", ("Not Now",))]},
]
DESKTOP = ("Finder",)


def _desktop(scr):
    # the menu bar's Finder menu, at the top left, and no Setup Assistant button
    # (the menu bar may read as one line: " Finder File Edit View Go Window Help")
    top = [b for b in scr.find("Finder") if b.y < scr.size[1] * 0.04]
    return bool(top) and not scr.find("Continue", exact=True)


def _hello(scr):
    """The greeting: a word or two and nothing else."""
    words = [b for b in scr.boxes if b.y > scr.size[1] * 0.08]
    return 0 < len(words) <= 3 and not scr.has("Continue", "Back")


def _field(name, scr, *labels):
    """Click into the text field of a label: to its right for "Label:" forms
    (macOS 12), on the placeholder itself for forms without labels; then select
    what it holds, so typing replaces it.  False when no label is there."""
    for label in labels:
        hits = scr.find(label)
        if hits:
            b = hits[0]
            if b.text.rstrip().endswith(":"):
                click(name, b.x + b.w + scr.size[0] // 60, b.y + b.h // 2)
            else:
                click(name, *b.center)
            time.sleep(0.5)
            keys(name, "cmd+a")
            return True
    return False


def _account(name, user, password, full_name, scr):
    if scr.find("Full name:"):
        # labelled form (12): each field to the right of its label
        _field(name, scr, "Full name:")
        type_text(name, full_name)
        time.sleep(1)
        scr = screen(name)
        _field(name, scr, "Account name:")
        type_text(name, user)
        _field(name, scr, "Password:")
        type_text(name, password)
        keys(name, "tab")
        keys(name, "cmd+a")
        type_text(name, password)
    else:
        # placeholder form (13+): the first field by its placeholder (or what a
        # previous attempt left in it), then the rest in tab order; the
        # account name fills in from the full name, so it is replaced too
        first = scr.find("Full Name", exact=True) or scr.find(full_name, exact=True)
        if not first:
            die(f"'{name}': no full name field on the account pane ({scr.png})")
        click(name, *first[0].center)
        time.sleep(0.5)
        for value in (full_name, user):
            keys(name, "cmd+a")
            type_text(name, value)
            time.sleep(0.5)
            keys(name, "tab")
        time.sleep(1)
        scr = screen(name)
        # the password row: its placeholders, or the dots of an earlier attempt
        # (Tab from Password skips Verify on 26, so each is clicked)
        dots = [b for b in scr.boxes if re.fullmatch(r"[•·.\s0o]{1,}", b.text) and "•" in b.text]
        pw = scr.find("Password", exact=True) or sorted(dots, key=lambda b: b.x)[:1]
        if not pw:
            die(f"'{name}': no password field on the account pane ({scr.png})")
        pw = pw[0]
        verify = scr.find("Verify Password", exact=True) or \
            [b for b in dots if abs(b.y - pw.y) < pw.h * 2 and b.x > pw.x + scr.size[0] // 20]
        # a hint left by an earlier attempt (one holding the password is refused)
        for b in scr.find(password, exact=True):
            if b.y > pw.y + pw.h:
                click(name, *b.center)
                keys(name, "cmd+a")
                keys(name, "backspace")
        targets = [pw] + verify[:1]
        if len(targets) < 2:
            # no Verify text: its field is the right half of the password row
            targets.append(Box(pw.x + scr.size[0] // 7, pw.y, pw.w, pw.h, 1.0, ""))
        for b in targets:
            click(name, *b.center)
            time.sleep(0.5)
            keys(name, "cmd+a")
            type_text(name, password)
            time.sleep(0.5)
    time.sleep(1)
    s2 = screen(name)
    if not click_text(name, "Continue", s2, exact=True):
        keys(name, "return")


def assistant(name, user, password, full_name, timeout=1800):
    t0 = time.time()
    last, same = None, 0
    while time.time() - t0 < timeout:
        scr = screen(name)
        if _desktop(scr):
            log(f"'{name}': at the desktop")
            return
        sig = scr.text()
        if not sig.strip():
            # blank: between panes, the desktop loading, or the display asleep
            # (display sleep is on until the baseline): a key wakes it
            keys(name, "shift")
            time.sleep(5)
            continue
        if sig == last:
            same += 1
            if same >= 6:
                die(f"'{name}': Setup Assistant is stuck on this screen ({scr.png}):\n{sig[:600]}")
        else:
            same = 0
        last = sig
        pane = None
        for p in PANES:
            if p.get("hello"):
                if _hello(scr):
                    pane = p
                    break
                continue
            if all(scr.has(t) for t in p["when"]) and not any(scr.has(t) for t in p.get("unless", [])):
                pane = p
                break
        if not pane:
            time.sleep(5)  # loading, or an animation between panes
            continue
        log(f"'{name}': pane {pane['when'] or 'greeting'}")
        for act in pane["act"]:
            kind = act[0]
            if kind == "click":
                if not any(click_text(name, t, scr, exact=True) or click_text(name, t, scr)
                           for t in act[1]):
                    break
            elif kind == "select":
                # a list row: click it, after typing its name to scroll it into view
                if not click_text(name, act[1], scr, exact=True):
                    type_text(name, act[1])
                    time.sleep(1)
                    scr = screen(name)
                    if not click_text(name, act[1], scr, exact=True):
                        break
                time.sleep(0.5)
                scr = screen(name)
            elif kind == "type":
                type_text(name, act[1])
            elif kind == "key":
                keys(name, act[1])
            elif kind == "pause":
                time.sleep(act[1])
            elif kind == "login":
                type_text(name, password)
                keys(name, "return")
                time.sleep(10)
            elif kind == "account":
                _account(name, user, password, full_name, scr)
            elif kind == "hello":
                w, h = scr.size
                word = [b for b in scr.boxes if b.y > h * 0.8]
                x, y = word[0].center if word else (w // 2, int(h * 0.9))
                click(name, x, y - int(h * 0.025))
            if kind != "pause":
                scr = screen(name) if kind == "click" and len(pane["act"]) > 1 else scr
        time.sleep(4)
    die(f"'{name}': not at the desktop after {timeout // 60} min of Setup Assistant")


# ---- the baseline -------------------------------------------------------------------

def terminal(name, command, wait=3):
    """Type one command into a new Terminal window.  Terminal is opened from
    Finder's Utilities folder (Spotlight is still indexing on a new system)."""
    keys(name, "esc")
    # Finder is in front after login; if it is not, a click on the desktop
    # brings it (on 14+ that shows a tip about the wallpaper, which Esc closes)
    for attempt in range(3):
        keys(name, "cmd+shift+u")
        time.sleep(3)
        if screen(name).has("Utilities"):
            break
        w, h = screen(name).size
        click(name, int(w * 0.5), int(h * 0.6))
        time.sleep(1)
        keys(name, "esc")
    else:
        die(f"'{name}': could not open Finder's Utilities folder ({screen(name).png})")
    time.sleep(1)
    type_text(name, "Terminal")
    time.sleep(1)
    keys(name, "cmd+o")
    wait_text(name, "Last login", "zsh", timeout=90)
    time.sleep(2)
    type_text(name, command)
    keys(name, "return")
    time.sleep(wait)


def baseline(name, user=DEFAULT_USER, password=DEFAULT_PASSWORD, key=None):
    """On the desktop: SSH on (through Terminal, the one step that needs the
    screen), then everything else over SSH."""
    utm.ensure_app()
    pub = Path(key).expanduser() if key else next(
        (p for p in (Path.home() / ".ssh" / f for f in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"))
         if p.exists()), None)
    if not pub or not pub.exists():
        die("no SSH public key in ~/.ssh (pass --key)")
    # Remote Login: the ssh daemon enabled for good, with the password typed at sudo's prompt
    if utm.osa("status", name) != "started":
        # a provisioned VM (Remote Login on) or one at its desktop after setup
        utm.osa("start", name)
        t0 = time.time()
        while True:
            time.sleep(10)
            ip = guest_address(name)
            if _port_open(ip, 22) or _desktop(screen(name)):
                break
            if time.time() - t0 > 600:
                die(f"'{name}' started but neither SSH nor its desktop came up in 10 min")
    ip = guest_address(name)
    if not _port_open(ip, 22):
        terminal(name, "sudo launchctl enable system/com.openssh.sshd && "
                       "sudo launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist")
        wait_text(name, "Password", timeout=30)
        type_text(name, password)
        keys(name, "return")
        t0 = time.time()
        while not _port_open(ip, 22):
            if time.time() - t0 > 90:
                die(f"'{name}': Remote Login did not come up at {ip} (screen: {screen(name).png})")
            time.sleep(5)
        keys(name, "cmd+q")  # Terminal
    utm.vm_register(name, user, "macos", str(pub))
    from .common import VM
    from . import remote
    vm = VM(name)
    # the key, by password once (sshpass is not there: an askpass helper answers)
    _copy_key(vm, user, password, pub)
    cmds = [
        # no display sleep, no system sleep, no screen saver, no lock
        f"echo {shlex.quote(password)} | sudo -S pmset -a displaysleep 0 sleep 0 disksleep 0",
        "defaults -currentHost write com.apple.screensaver idleTime -int 0",
        f"sysadminctl -screenLock off -password {shlex.quote(password)} 2>&1 || true",
        "defaults write com.apple.screensaver askForPassword -int 0",
        # no Spotlight indexing: it saturates the VM after every large copy (Xcode, runtimes, sources)
        f"echo {shlex.quote(password)} | sudo -S mdutil -a -i off >/dev/null 2>&1 || true",
        # sudo without a password for the harness
        f"echo {shlex.quote(password)} | sudo -S sh -c "
        f"\"echo '{user} ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/utmbot && chmod 440 /etc/sudoers.d/utmbot\"",
    ]
    # automatic login, so a reboot comes back to the desktop (screenshots, prompts)
    import base64
    kc = base64.b64encode(_kcpassword(password)).decode()
    cmds += [f"echo {kc} | base64 -D | sudo tee /etc/kcpassword >/dev/null && sudo chmod 600 /etc/kcpassword",
             f"sudo defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser {shlex.quote(user)}"]
    for c in cmds:
        rc, out = remote.ssh(vm, c, timeout=120)
        if rc != 0:
            die(f"'{name}': `{c.split(' ')[0]} …` failed: {out[-300:]}")
    rc, out = remote.ssh(vm, "sw_vers -productVersion; pmset -g | grep -E ' (displaysleep|sleep) '", timeout=60)
    log(f"'{name}': baseline done (macOS {out.splitlines()[0] if out else '?'})")


def _kcpassword(password):
    """/etc/kcpassword: the password XORed with loginwindow's fixed key, padded to 12 bytes."""
    key = [0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F]
    data = bytearray(password.encode())
    data += bytes(12 - len(data) % 12)
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def guest_address(name, timeout=300):
    """The guest's address: its MAC's DHCP lease from the host (the Shared network)."""
    import plistlib
    bundle = utm.vm_file(name)
    nets = plistlib.loads((Path(bundle) / "config.plist").read_bytes()).get("Network") or []
    if not nets or not nets[0].get("MacAddress"):
        die(f"'{name}' has no network interface")
    mac = nets[0]["MacAddress"]
    t0 = time.time()
    while True:
        rc, ip = run(["bash", str(utm.UTM_SH), "ip", mac], timeout=30)
        if ip.strip():
            return ip.strip()
        if time.time() - t0 > timeout:
            die(f"'{name}' ({mac}) has no DHCP lease after {timeout} s")
        time.sleep(10)


def _port_open(ip, port):
    import socket
    try:
        with socket.create_connection((ip, port), timeout=3):
            return True
    except OSError:
        return False


def _copy_key(vm, user, password, pub):
    """Install the public key with the password, through an SSH_ASKPASS helper."""
    askpass = ensure_dir(SHOTS / "askpass") / "pw.sh"
    askpass.write_text(f"#!/bin/sh\necho {shlex.quote(password)}\n")
    askpass.chmod(0o700)
    host = vm.ssh_dest()
    env = dict(os.environ, SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY=":0")
    k = pub.read_text().strip()
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "PubkeyAuthentication=no",
           "-o", f"UserKnownHostsFile={vm.state_dir / 'known_hosts'}", "-o", f"HostKeyAlias={slug(vm.name)}",
           host, f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF {shlex.quote(k)} "
                 f"~/.ssh/authorized_keys 2>/dev/null || echo {shlex.quote(k)} >> ~/.ssh/authorized_keys; "
                 "chmod 600 ~/.ssh/authorized_keys"]
    for _ in range(12):
        rc, out = run(cmd, timeout=60, env=env)
        if rc == 0:
            return
        time.sleep(10)
    die(f"could not install the SSH key in '{vm.name}': {out[-300:]}")


# ---- all of it ----------------------------------------------------------------------

STAGES = ("created", "installed", "setup", "baseline", "snapshot")


def _stage(name):
    f = SHOTS / slug(name) / "stage"
    return f.read_text().strip() if f.exists() else ""


def _done(name, stage):
    (ensure_dir(SHOTS / slug(name)) / "stage").write_text(stage + "\n")
    log(f"'{name}': {stage}")


def _past(name, stage):
    cur = _stage(name)
    return cur in STAGES and STAGES.index(cur) >= STAGES.index(stage)


def shutdown(name, timeout=300):
    from .common import VM
    from . import remote
    if utm.osa("status", name) != "started":
        return
    remote.ssh(VM(name), "sudo shutdown -h now", timeout=60)
    t0 = time.time()
    while utm.osa("status", name) != "stopped":
        if time.time() - t0 > timeout:
            utm.osa("stop", name)
            break
        time.sleep(5)


def provision(name, ipsw=None, latest=False, cpus=4, memory=8192, disk_gb=96,
              user=DEFAULT_USER, password=DEFAULT_PASSWORD, key=None):
    utm.ensure_app()
    if not _past(name, "created"):
        if utm.osa("exists", name) != "true":
            create(name, ipsw, latest, cpus, memory, disk_gb)
        record_version(name, ipsw, latest)
        _done(name, "created")
    if not _past(name, "installed"):
        install(name)
        _done(name, "installed")
    if not _past(name, "setup"):
        setup(name, user, password)
        _done(name, "setup")
    if not _past(name, "baseline"):
        if utm.osa("status", name) != "started":
            utm.osa("start", name)
            time.sleep(30)
        baseline(name, user, password, key)
        _done(name, "baseline")
    if not _past(name, "snapshot"):
        from .common import VM
        from . import snap
        shutdown(name)
        snap.create(VM(name), "macos-baseline", overwrite=True, kind="baseline")
        _done(name, "snapshot")
    log(f"'{name}' is provisioned: ssh as {user} (`utmbot --vm '{name}' guest …`), snapshot "
        "macos-baseline")


# ---- running ----------------------------------------------------------------------

def up(name, timeout=300):
    """Start a provisioned VM and wait until SSH answers."""
    from .common import VM
    from . import remote
    utm.ensure_app()
    vm = VM(name)
    if utm.osa("status", name) != "started":
        utm.osa("start", name)
    t0 = time.time()
    while not remote.reachable(vm):
        if time.time() - t0 > timeout:
            die(f"'{name}' did not answer SSH within {timeout} s")
        time.sleep(5)
    log(f"'{name}' is up: {vm.ssh_dest()}")
    return vm


def _ssh(vm, cmd, timeout=600):
    from . import remote
    rc, out = remote.ssh(vm, cmd, timeout=timeout)
    if rc != 0:
        die(f"'{vm.name}': `{cmd[:80]}` failed ({rc}): {out[-600:]}")
    return out


# ---- Command Line Tools ---------------------------------------------------------

def clt(name):
    vm = up(name)
    from . import remote
    if remote.ssh(vm, "xcode-select -p >/dev/null 2>&1 && test -x \"$(xcode-select -p)/usr/bin/lldb\" && echo yes",
                  timeout=60)[1].strip() == "yes":
        log(f"'{name}': the Command Line Tools are installed")
        _developer_mode(vm)
        return
    # the marker makes softwareupdate list the tools, as the install-on-demand dialog would
    out = _ssh(vm, "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress; "
                   "softwareupdate -l 2>&1 | sed -n 's/^[* ]*Label: //p' | grep 'Command Line Tools' "
                   "| sort -V | tail -1", timeout=600).strip()
    if not out:
        die(f"'{name}': softwareupdate offers no Command Line Tools (no network, or Apple's catalog)")
    log(f"'{name}': installing {out}")
    _ssh(vm, f"sudo softwareupdate -i {shlex.quote(out)} --verbose 2>&1 | tail -3; "
             "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress", timeout=3600)
    _developer_mode(vm)
    log(f"'{name}': {_ssh(vm, 'xcode-select -p; lldb --version').strip()}")


def _developer_mode(vm):
    """A debugger started over SSH cannot ask for permission: developer mode, the user a developer."""
    _ssh(vm, f"sudo DevToolsSecurity -enable >/dev/null; sudo dseditgroup -o edit -a {vm.user} -t user _developer")


# ---- Xcode ----------------------------------------------------------------------

SIM_INDEX = "https://devimages-cdn.apple.com/downloads/xcode/simulators/index2.dvtdownloadableindex"


def _index_runtime(ver):
    """The iOS <ver> runtime DMG from Apple's public simulator index, downloaded to the cache;
    dies with what to do when Apple serves it only to signed-in developers."""
    import plistlib
    import urllib.request
    cache = ensure_dir(DATA / "cache" / "simruntimes")
    dmg = cache / f"iOS-{ver}.dmg"
    if dmg.exists():
        return dmg
    with urllib.request.urlopen(SIM_INDEX, timeout=60) as r:
        index = plistlib.loads(r.read())
    entry = next((d for d in index.get("downloadables", []) if re.fullmatch(
        rf"iOS {re.escape(ver)}( Simulator| Simulator Runtime)", d.get("name", ""))), None)
    if not entry:
        die(f"Apple's simulator index has no iOS {ver} runtime")
    src = entry.get("source") or ""
    if not src:
        die(f"Apple's simulator index lists iOS {ver} without a download (newer runtimes come only "
            "through Xcode itself)")
    if "devimages-cdn.apple.com" not in src:
        die(f"neither this Xcode nor Apple's public simulator index offers the iOS {ver} runtime "
            f"(the index points at {src}, which needs a developer sign-in); pass a DMG with --ios-dmg")
    log(f"downloading the iOS {ver} runtime ({entry.get('fileSize', 0) // 2**20} MiB) from Apple")
    rc, out = run(["curl", "-sfL", "-C", "-", "-o", f"{dmg}.part", src], timeout=6 * 3600)
    if rc != 0:
        die(f"downloading {src} failed: {out[-300:]}")
    Path(f"{dmg}.part").rename(dmg)
    return dmg


def _install_runtime_dmg(vm, dmg):
    """A runtime DMG into the VM: the package inside an old one (iOS 15 and
    earlier) with installer, a runtime disk image with simctl."""
    from . import remote
    if not dmg.is_file():
        die(f"no runtime DMG at {dmg}")
    log(f"'{vm.name}': installing the simulator runtime {dmg.name}")
    remote.put(vm, dmg, "runtime.dmg", timeout=3600)
    out = _ssh(vm, "set -e; m=$(hdiutil attach -nobrowse -readonly ~/runtime.dmg | awk -F'\t' '/Volumes/{print $NF}' | tail -1); "
                   "pkg=$(ls \"$m\"/*.pkg 2>/dev/null | head -1); "
                   # the package wants the system volume: its runtime bundle goes where
                   # CoreSimulator looks instead
                   "if [ -n \"$pkg\" ]; then rm -rf ~/rtpkg; pkgutil --expand-full \"$pkg\" ~/rtpkg; hdiutil detach \"$m\" -quiet; "
                   # (the payload is the runtime bundle itself, or holds one)
                   "rt=$(find ~/rtpkg -name '*.simruntime' -maxdepth 4 -type d | head -1); "
                   "if [ -z \"$rt\" ] && [ -f ~/rtpkg/Payload/Contents/Info.plist ]; then rt=~/rtpkg/Payload; fi; test -n \"$rt\"; "
                   "name=$(defaults read \"$rt/Contents/Info.plist\" CFBundleName 2>/dev/null || basename \"$rt\" .simruntime); "
                   # (the user's profiles: the system one is CoreSimulator's own on 15+)
                   "R=~/Library/Developer/CoreSimulator/Profiles/Runtimes; mkdir -p $R; "
                   "rm -rf \"$R/$name.simruntime\"; mv \"$rt\" \"$R/$name.simruntime\"; rm -rf ~/rtpkg; "
                   "killall -9 com.apple.CoreSimulator.CoreSimulatorService 2>/dev/null || true; sleep 3; "
                   "echo installed $name; "
                   "else hdiutil detach \"$m\" -quiet; xcrun simctl runtime add ~/runtime.dmg; fi; rm -f ~/runtime.dmg",
               timeout=3 * 3600)
    log(f"'{vm.name}': {out.strip().splitlines()[-1] if out.strip() else 'installed'}")


def xcode(name, xip, runtimes=(), snapshot=None, dmgs=(), platforms=()):
    """Xcode from a XIP (or an installed Xcode.app of this Mac) into
    /Applications/Xcode-<version>.app, selected, its license accepted and first
    launch done, plus iOS simulator runtimes and other platforms (visionOS)."""
    from .common import VM
    from . import remote, snap
    xip = Path(xip).expanduser()
    if xip.is_dir() and (xip / "Contents" / "Info.plist").exists():
        import plistlib
        version = plistlib.loads((xip / "Contents" / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    elif xip.is_file():
        m = re.search(r"Xcode[_-]?(\d+(?:\.\d+)*)", xip.name)
        version = m.group(1) if m else "x"
    else:
        die(f"no XIP or Xcode.app at {xip}")
    app = f"/Applications/Xcode-{version}.app"
    vm = up(name)
    have = remote.ssh(vm, f"test -d {app} && echo yes", timeout=60)[1].strip() == "yes"
    if not have and xip.is_dir():
        # an Xcode installed on this Mac: copied as it is (its signature stays intact)
        log(f"'{name}': copying {xip.name} from this Mac")
        remote.ensure_master(vm)
        tar = subprocess.Popen(["tar", "-C", str(xip.parent), "-cf", "-", xip.name], stdout=subprocess.PIPE)
        rc = subprocess.run(["ssh"] + remote.ssh_opts(vm) + [remote.dest(vm),
                             f"rm -rf ~/xcode-copy && mkdir ~/xcode-copy && tar -C ~/xcode-copy -xf - && "
                             f"sudo rm -rf {app} && sudo mv ~/xcode-copy/{shlex.quote(xip.name)} {app} && rm -rf ~/xcode-copy"],
                            stdin=tar.stdout, capture_output=True, timeout=3 * 3600)
        tar.wait()
        if rc.returncode != 0:
            die(f"'{name}': copying {xip.name} failed: {rc.stderr.decode()[-400:]}")
        have = True
    if not have:
        free = int(_ssh(vm, "df -k ~ | awk 'NR==2{print $4}'").strip() or 0) // (1024 * 1024)
        need = xip.stat().st_size // (1024 ** 3) * 5 + 10
        if free < need:
            die(f"'{name}' has {free} GB free; Xcode needs about {need} GB (provision with a bigger "
                "--disk-gb)")
        log(f"'{name}': copying {xip.name} ({xip.stat().st_size // 2**20} MiB)")
        remote.put(vm, xip, "Xcode.xip", timeout=3600)
        log(f"'{name}': expanding it (xip checks Apple's signature first; 10 to 30 min)")
        _ssh(vm, "rm -rf ~/xip && mkdir ~/xip && cd ~/xip && nohup xip --expand ~/Xcode.xip > ~/xip.log 2>&1 &"
                 " echo started", timeout=60)
        t0 = time.time()
        while remote.ssh(vm, "pgrep -x xip >/dev/null && echo running", timeout=60)[1].strip() == "running":
            if time.time() - t0 > 3 * 3600:
                die(f"'{name}': xip still expanding after 3 h")
            time.sleep(30)
        out = _ssh(vm, "ls ~/xip; tail -3 ~/xip.log")
        if "Xcode" not in out:
            die(f"'{name}': expanding {xip.name} failed: {out[-400:]}")
        _ssh(vm, f"sudo rm -rf {app} && sudo mv ~/xip/Xcode*.app {app} && rm -rf ~/xip ~/Xcode.xip")
    _ssh(vm, f"sudo xcode-select -s {app}/Contents/Developer && sudo xcodebuild -license accept && "
             "sudo xcodebuild -runFirstLaunch", timeout=1800)
    log(f"'{name}': {_ssh(vm, 'xcodebuild -version').strip().splitlines()[0]} selected")
    # the iOS platform of this Xcode, without which it builds nothing for a simulator
    sdk = re.search(r"iphonesimulator(\d+(?:\.\d+)*)", _ssh(vm, "xcodebuild -showsdks"))
    if not sdk or f"iOS {sdk.group(1)} " not in _ssh(vm, "xcrun simctl list runtimes"):
        log(f"'{name}': downloading this Xcode's iOS platform (iOS {sdk.group(1) if sdk else '?'})")
        _ssh(vm, "xcodebuild -downloadPlatform iOS", timeout=3 * 3600)
    # the Metal toolchain, also a separate download (UTM's shaders need it)
    if remote.ssh(vm, "xcrun -f metal >/dev/null 2>&1 && xcrun metal --version >/dev/null 2>&1 && echo ok",
                  timeout=120)[1].strip() != "ok":
        log(f"'{name}': downloading the Metal toolchain")
        _ssh(vm, "xcodebuild -downloadComponent MetalToolchain", timeout=3600)
    for plat in platforms:
        log(f"'{name}': downloading the {plat} platform")
        _ssh(vm, f"xcodebuild -downloadPlatform {shlex.quote(plat)}", timeout=3 * 3600)
    for dmg in dmgs:
        _install_runtime_dmg(vm, Path(dmg).expanduser())
    for ver in runtimes:
        if f"iOS {ver}" in _ssh(vm, "xcrun simctl list runtimes"):
            continue
        log(f"'{name}': downloading the iOS {ver} simulator runtime")
        rc, out = remote.ssh(vm, f"xcodebuild -downloadPlatform iOS -buildVersion {shlex.quote(ver)}",
                             timeout=3 * 3600)
        if rc != 0 and "not available for download" in out:
            # older runtimes are not in this Xcode's catalog: Apple's public index has some
            _install_runtime_dmg(vm, _index_runtime(ver))
        elif rc != 0:
            die(f"'{name}': the iOS {ver} simulator runtime did not download: {out[-600:]}")
    log(f"'{name}': runtimes:\n{_ssh(vm, 'xcrun simctl list runtimes').strip()}")
    shutdown(name)
    snap.create(VM(name), snapshot or f"xcode-{version}", overwrite=True, kind="xcode",
                xcode=version, runtimes=",".join(runtimes))


# ---- command line -------------------------------------------------------------------

def main(argv):
    p = argparse.ArgumentParser(prog="utmbot mac", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("create"); s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--ipsw"); g.add_argument("--latest", action="store_true")
    s.add_argument("--cpus", type=int, default=4); s.add_argument("--memory", type=int, default=8192)
    s.add_argument("--disk-gb", type=int, default=96)
    s = sub.add_parser("install"); s.add_argument("name")
    s = sub.add_parser("setup"); s.add_argument("name")
    s.add_argument("--user", default=DEFAULT_USER); s.add_argument("--password", default=DEFAULT_PASSWORD)
    s.add_argument("--full-name")
    s = sub.add_parser("baseline"); s.add_argument("name")
    s.add_argument("--user", default=DEFAULT_USER); s.add_argument("--password", default=DEFAULT_PASSWORD)
    s.add_argument("--key")
    s = sub.add_parser("provision"); s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--ipsw"); g.add_argument("--latest", action="store_true")
    s.add_argument("--cpus", type=int, default=4); s.add_argument("--memory", type=int, default=8192)
    s.add_argument("--disk-gb", type=int, default=96)
    s.add_argument("--user", default=DEFAULT_USER); s.add_argument("--password", default=DEFAULT_PASSWORD)
    s.add_argument("--key")
    s = sub.add_parser("up"); s.add_argument("name")
    s = sub.add_parser("down"); s.add_argument("name")
    s = sub.add_parser("clt"); s.add_argument("name")
    s = sub.add_parser("xcode"); s.add_argument("name"); s.add_argument("--xip", required=True)
    s.add_argument("--ios", action="append", default=[]); s.add_argument("--snapshot")
    s.add_argument("--ios-dmg", action="append", default=[])
    s.add_argument("--platform", action="append", default=[], help="another platform to download (visionOS)")
    s = sub.add_parser("screen"); s.add_argument("name"); s.add_argument("--out")
    s.add_argument("--raw", action="store_true")
    s = sub.add_parser("click"); s.add_argument("name"); s.add_argument("text", nargs="?")
    s.add_argument("--at", nargs=2, type=int); s.add_argument("--nth", type=int, default=0)
    s.add_argument("--below", type=int); s.add_argument("--button", default="left")
    s = sub.add_parser("wait"); s.add_argument("name"); s.add_argument("text")
    s.add_argument("--timeout", type=int, default=300)
    s = sub.add_parser("key"); s.add_argument("name"); s.add_argument("keys", nargs="+")
    s = sub.add_parser("type"); s.add_argument("name"); s.add_argument("text")
    a = p.parse_args(argv)
    if a.cmd == "create":
        create(a.name, a.ipsw, a.latest, a.cpus, a.memory, a.disk_gb)
        record_version(a.name, a.ipsw, a.latest)
    elif a.cmd == "provision":
        provision(a.name, a.ipsw, a.latest, a.cpus, a.memory, a.disk_gb, a.user, a.password, a.key)
    elif a.cmd == "install":
        install(a.name)
    elif a.cmd == "setup":
        setup(a.name, a.user, a.password, a.full_name)
    elif a.cmd == "baseline":
        baseline(a.name, a.user, a.password, a.key)
    elif a.cmd == "up":
        up(a.name)
    elif a.cmd == "down":
        shutdown(a.name)
    elif a.cmd == "clt":
        clt(a.name)
    elif a.cmd == "xcode":
        xcode(a.name, a.xip, a.ios, a.snapshot, a.ios_dmg, a.platform)
    elif a.cmd == "screen":
        scr = screen(a.name, a.out)
        print(f"# {scr.png} {scr.size[0]}x{scr.size[1]}")
        for b in scr.boxes:
            print(b.text if a.raw else repr(b))
    elif a.cmd == "click":
        if a.at:
            click(a.name, a.at[0], a.at[1], a.button)
        elif not a.text or not (click_text(a.name, a.text, nth=a.nth, exact=True, below=a.below)
                                or click_text(a.name, a.text, nth=a.nth, below=a.below)):
            die(f"'{a.text}' is not on screen")
    elif a.cmd == "wait":
        print(repr(wait_text(a.name, a.text, timeout=a.timeout).find(a.text)[0]))
    elif a.cmd == "key":
        for k in a.keys:
            keys(a.name, k)
            time.sleep(0.3)
    elif a.cmd == "type":
        type_text(a.name, a.text)
    return 0
