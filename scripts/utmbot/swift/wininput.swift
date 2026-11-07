// wininput: keyboard and mouse input to one app's window (a CrossOver game's).
//
//   wininput trusted [--prompt]     "trusted" (exit 0) or "untrusted" (exit 1):
//                                   may this process post input (Accessibility)?
//                                   --prompt: macOS lists the app and asks
//   wininput locked                 "locked" (exit 0) or "unlocked" (exit 1)
//   wininput windows                on-screen windows at every level, one per line:
//                                   id \t x,y,w,h \t owner \t title \t pid \t layer \t scale
//   wininput click PID WID X Y [left|right|middle] [HOLD]
//   wininput move  PID WID X Y
//   wininput key   PID CODE[,CODE...] [HOLD]
//                                   macOS virtual key codes, pressed in order,
//                                   held HOLD seconds, released in reverse
//                                   (modifiers first: 56,0 = shift+a)
//   wininput text  PID TEXT         US-layout ASCII, typed key by key
//
// X Y are pixels of the window's screenshot (`screencapture -l WID`): points
// from the window's top-left corner times the display's backing scale.
//
// What a CrossOver 26.3 game accepts (Forspoken, Notepad, checked):
//  * The app has to be frontmost, or it ignores keys.  NSRunningApplication
//    .activate is a request macOS ignores when the caller is not the active
//    app; setting AXFrontmost (Accessibility) is obeyed.
//  * Keys posted to the process (CGEventPostToPid) arrive.  Wine maps the
//    key code, not the event's string: a string on key code 0 types "a".
//  * Mouse moves posted to the process arrive (menus highlight), mouse
//    buttons do not: Wine reads the HID button state.  Buttons go to the HID
//    event tap, and only after checking that the screen is unlocked and the
//    app is still frontmost, so they cannot land in another app.
//  * A frontmost full-screen Wine window moves up to a higher window level
//    (26), out of a "layer 0 only" window list; `windows` lists every level.
import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

func fail(_ s: String) -> Never {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
    exit(2)
}

let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else { fail("usage: wininput trusted|locked|windows|click|move|key|text ...") }

func screenLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

func scale(of r: CGRect) -> CGFloat {
    // NSScreen frames are bottom-left based; CGWindow bounds top-left based
    guard let main = NSScreen.screens.first else { return 1 }
    let c = CGPoint(x: r.midX, y: main.frame.maxY - r.midY)
    return (NSScreen.screens.first { $0.frame.contains(c) } ?? main).backingScaleFactor
}

if cmd == "trusted" {
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: args.contains("--prompt")]
    let ok = AXIsProcessTrustedWithOptions(opts as CFDictionary)
    print(ok ? "trusted" : "untrusted")
    exit(ok ? 0 : 1)
}
if cmd == "locked" {
    let l = screenLocked()
    print(l ? "locked" : "unlocked")
    exit(l ? 0 : 1)
}
if cmd == "windows" {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] else { exit(1) }
    for w in list {
        let layer = w[kCGWindowLayer as String] as? Int ?? 0
        guard layer >= 0, layer < 1000, (w[kCGWindowAlpha as String] as? Double ?? 1) > 0,
              let b = w[kCGWindowBounds as String] as? [String: Any],
              let r = CGRect(dictionaryRepresentation: b as CFDictionary) else { continue }
        let id = w[kCGWindowNumber as String] as? Int ?? 0
        let pid = w[kCGWindowOwnerPID as String] as? Int ?? 0
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        let name = w[kCGWindowName as String] as? String ?? ""
        print("\(id)\t\(Int(r.minX)),\(Int(r.minY)),\(Int(r.width)),\(Int(r.height))\t\(owner)\t\(name)\t\(pid)\t\(layer)\t\(scale(of: r))")
    }
    exit(0)
}

guard args.count >= 3, let pid = pid_t(args[1]) else { fail("usage: wininput \(cmd) PID ...") }
if !AXIsProcessTrusted() { fail("untrusted: this process may not post input (Accessibility)") }
if screenLocked() { fail("locked: the screen is locked; input would not reach the app") }
let src = CGEventSource(stateID: .hidSystemState)

// NSWorkspace learns of activations through the run loop: spin it first
func frontPid() -> pid_t {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    return NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
}

func activate() {
    let ax = AXUIElementCreateApplication(pid)
    for _ in 0..<30 {
        if frontPid() == pid { return }
        AXUIElementSetAttributeValue(ax, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        usleep(100_000)
    }
    fail("could not bring pid \(pid) to the front (the front app is pid \(frontPid()))")
}

func post(_ e: CGEvent?) {
    guard let e = e else { fail("could not create an event") }
    e.postToPid(pid)
}

func postHID(_ e: CGEvent?) {
    guard let e = e else { fail("could not create an event") }
    if screenLocked() { fail("locked: the screen locked during input") }
    if frontPid() != pid { fail("pid \(pid) is no longer the front app (pid \(frontPid()) is); stopped") }
    e.post(tap: .cghidEventTap)
}

func windowBounds(_ wid: CGWindowID) -> CGRect {
    guard let l = CGWindowListCopyWindowInfo([.optionIncludingWindow], wid) as? [[String: Any]],
          let w = l.first, (w[kCGWindowOwnerPID as String] as? Int).map(pid_t.init) == pid,
          let b = w[kCGWindowBounds as String] as? [String: Any],
          let r = CGRect(dictionaryRepresentation: b as CFDictionary) else {
        fail("no window \(wid) of pid \(pid)")
    }
    return r
}

func point(_ wid: String, _ x: String, _ y: String) -> (CGPoint, CGWindowID) {
    guard let w = CGWindowID(wid), let px = Double(x), let py = Double(y) else { fail("bad window or point") }
    let r = windowBounds(w)
    let s = Double(scale(of: r))
    if px < 0 || py < 0 || px >= Double(r.width) * s || py >= Double(r.height) * s {
        fail("point \(x),\(y) is outside the window (\(Int(Double(r.width) * s))x\(Int(Double(r.height) * s)) pixels)")
    }
    return (CGPoint(x: Double(r.minX) + px / s, y: Double(r.minY) + py / s), w)
}

func mouse(_ type: CGEventType, _ p: CGPoint, _ b: CGMouseButton, _ wid: CGWindowID, hid: Bool) {
    let e = CGEvent(mouseEventSource: src, mouseType: type, mouseCursorPosition: p, mouseButton: b)
    e?.setIntegerValueField(.mouseEventWindowUnderMousePointer, value: Int64(wid))
    e?.setIntegerValueField(.mouseEventWindowUnderMousePointerThatCanHandleThisEvent, value: Int64(wid))
    if type != .mouseMoved { e?.setIntegerValueField(.mouseEventClickState, value: 1) }
    hid ? postHID(e) : post(e)
}

func moveTo(_ p: CGPoint, _ wid: CGWindowID) {
    CGWarpMouseCursorPosition(p)
    CGAssociateMouseAndMouseCursorPosition(1)
    mouse(.mouseMoved, p, .left, wid, hid: false)
    usleep(80_000)
}

switch cmd {
case "move":
    guard args.count >= 5 else { fail("usage: wininput move PID WID X Y") }
    let (p, w) = point(args[2], args[3], args[4])
    activate()
    moveTo(p, w)
case "click":
    guard args.count >= 5 else { fail("usage: wininput click PID WID X Y [button] [hold]") }
    let (p, w) = point(args[2], args[3], args[4])
    let name = args.count > 5 ? args[5] : "left"
    let hold = args.count > 6 ? Double(args[6]) ?? 0.1 : 0.1
    let (b, down, up): (CGMouseButton, CGEventType, CGEventType) =
        name == "right" ? (.right, .rightMouseDown, .rightMouseUp) :
        name == "middle" ? (.center, .otherMouseDown, .otherMouseUp) : (.left, .leftMouseDown, .leftMouseUp)
    activate()
    moveTo(p, w)
    mouse(down, p, b, w, hid: true)
    usleep(useconds_t(hold * 1_000_000))
    mouse(up, p, b, w, hid: true)
case "key":
    let codes = args[2].split(separator: ",").compactMap { CGKeyCode($0) }
    if codes.isEmpty { fail("bad key codes \(args[2])") }
    let hold = args.count > 3 ? Double(args[3]) ?? 0.15 : 0.15
    let mods: [CGKeyCode: CGEventFlags] = [56: .maskShift, 60: .maskShift, 59: .maskControl,
                                           62: .maskControl, 58: .maskAlternate, 61: .maskAlternate,
                                           55: .maskCommand, 54: .maskCommand]
    // what a real keyboard sets on arrows and the navigation keys
    let arrows: Set<CGKeyCode> = [123, 124, 125, 126]
    let nav: Set<CGKeyCode> = [114, 115, 116, 117, 119, 121]   // help home pgup del end pgdn
    func extra(_ c: CGKeyCode) -> CGEventFlags {
        arrows.contains(c) ? [.maskNumericPad, .maskSecondaryFn] : nav.contains(c) ? .maskSecondaryFn : []
    }
    activate()
    var flags: CGEventFlags = []
    for c in codes {
        if let m = mods[c] { flags.insert(m) }
        let e = CGEvent(keyboardEventSource: src, virtualKey: c, keyDown: true)
        e?.flags = flags.union(extra(c))
        if mods[c] != nil { e?.type = .flagsChanged }
        post(e)
        usleep(20_000)
    }
    usleep(useconds_t(hold * 1_000_000))
    for c in codes.reversed() {
        if let m = mods[c] { flags.remove(m) }
        let e = CGEvent(keyboardEventSource: src, virtualKey: c, keyDown: false)
        e?.flags = flags.union(extra(c))
        if mods[c] != nil { e?.type = .flagsChanged }
        post(e)
        usleep(20_000)
    }
case "text":
    // US layout: index = macOS virtual key code ("\0": no character there)
    let plain = Array("asdfhgzxcv\u{0}bqweryt123465=97-80]ou[ip\u{0}lj'k;\\,/nm.")
    let shifted = Array("ASDFHGZXCV\u{0}BQWERYT!@#$^%+(&_*)}OU{IP\u{0}LJ\"K:|<?NM>")
    var map: [Character: (CGKeyCode, Bool)] = [" ": (49, false), "\t": (48, false), "\n": (36, false),
                                               "`": (50, false), "~": (50, true)]
    for (i, c) in plain.enumerated() where c != "\u{0}" { map[c] = (CGKeyCode(i), false) }
    for (i, c) in shifted.enumerated() where c != "\u{0}" { map[c] = (CGKeyCode(i), true) }
    let text = args[2...].joined(separator: " ")
    for ch in text where map[ch] == nil { fail("cannot type \(ch) (US-layout ASCII only)") }
    activate()
    for ch in text {
        let (c, shift) = map[ch]!
        let f: CGEventFlags = shift ? .maskShift : []
        if shift {
            let e = CGEvent(keyboardEventSource: src, virtualKey: 56, keyDown: true)
            e?.type = .flagsChanged; e?.flags = f; post(e)
        }
        for down in [true, false] {
            let e = CGEvent(keyboardEventSource: src, virtualKey: c, keyDown: down)
            e?.flags = f
            post(e)
            usleep(15_000)
        }
        if shift {
            let e = CGEvent(keyboardEventSource: src, virtualKey: 56, keyDown: false)
            e?.type = .flagsChanged; e?.flags = []; post(e)
        }
        usleep(25_000)
    }
default:
    fail("unknown command \(cmd)")
}
print("ok")
