"""Keyboard and mouse input to a running VM, through UTM's scripting.

UTM's `input scan code` presses and releases a key in the same instant;
many games poll input once per frame and never see such a press, so keys
here are held (--hold) between a press and a release sent apart.

  utmbot input key KEY [--hold S] [--times N] [--gap S]
  utmbot input combo KEY+KEY[+KEY]  [--hold S]      e.g. alt+enter, shift+w
  utmbot input text TEXT                           ASCII, through UTM's keystroke
  utmbot input click X Y [--button left|right|middle]

KEY is a name (esc, enter, space, tab, backspace, w, a, s, d, f1, shift,
ctrl, alt, 0-9, a-z) or sc:N, a PC AT set-1 scan code in decimal.  Arrow keys and
the other extended (E0-prefixed) keys cannot be sent this way.
"""

import time

from .common import die
from .utm import osa

KEYS = {"esc": 1, "escape": 1, "backspace": 14, "tab": 15, "enter": 28, "return": 28,
        "ctrl": 29, "lshift": 42, "shift": 42, "rshift": 54, "alt": 56, "space": 57,
        "capslock": 58, "minus": 12, "equals": 13, "comma": 51, "period": 52, "slash": 53,
        "semicolon": 39, "quote": 40, "backquote": 41, "lbracket": 26, "rbracket": 27}
KEYS.update({str(d): 2 + i for i, d in enumerate("1234567890")})
for row, first in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)):
    KEYS.update({c: first + i for i, c in enumerate(row)})
KEYS.update({f"f{i}": 58 + i for i in range(1, 11)})
KEYS.update({"f11": 87, "f12": 88})


def code(key):
    k = key.lower()
    if k in KEYS:
        return KEYS[k]
    if k.startswith("sc:") and k[3:].isdigit() and 0 < int(k[3:]) < 128:
        return int(k[3:])
    die(f"unknown key {key!r}: use a name ({', '.join(sorted(KEYS)[:12])}, …) or sc:N for a "
        "decimal scan code")


def press(vm, codes, hold):
    osa("input-scancodes", vm.name, *codes)
    time.sleep(hold)
    osa("input-scancodes", vm.name, *[c + 128 for c in reversed(codes)])


def main(vm, rest):
    import argparse
    sub = rest[0] if rest else ""
    p = argparse.ArgumentParser(prog=f"utmbot input {sub}")
    if sub == "key":
        p.add_argument("key")
        p.add_argument("--hold", type=float, default=0.15)
        p.add_argument("--times", type=int, default=1)
        p.add_argument("--gap", type=float, default=0.3)
        a = p.parse_args(rest[1:])
        for i in range(a.times):
            press(vm, [code(a.key)], a.hold)
            if i + 1 < a.times:
                time.sleep(a.gap)
    elif sub == "combo":
        p.add_argument("keys")
        p.add_argument("--hold", type=float, default=0.15)
        a = p.parse_args(rest[1:])
        press(vm, [code(k) for k in a.keys.split("+")], a.hold)
    elif sub == "text":
        p.add_argument("text")
        a = p.parse_args(rest[1:])
        osa("input-text", vm.name, a.text)
    elif sub == "click":
        p.add_argument("x", type=int)
        p.add_argument("y", type=int)
        p.add_argument("--button", default="left", choices=["left", "right", "middle"])
        a = p.parse_args(rest[1:])
        osa("input-click", vm.name, a.x, a.y, a.button)
    else:
        print(__doc__)
        return 2
    print(f"INPUT {sub} {' '.join(rest[1:])}")
    return 0
