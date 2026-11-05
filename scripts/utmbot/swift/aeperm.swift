// aeperm BUNDLE_ID [--ask]: may this process send Apple Events to the app?
// Prints "ok", "consent" (macOS has to ask the user first), "not-running" or
// the OSStatus.  With --ask, macOS shows its Automation prompt and this
// waits for the answer.  Without it nothing blocks, unlike an AppleScript
// call that waits on a prompt nobody sees (a locked screen).
import CoreServices
import Foundation

let args = CommandLine.arguments
guard args.count > 1 else { print("usage: aeperm BUNDLE_ID [--ask]"); exit(2) }
var target = AEAddressDesc()
let id = Array(args[1].utf8)
_ = id.withUnsafeBufferPointer { AECreateDesc(typeApplicationBundleID, $0.baseAddress, $0.count, &target) }
let st = AEDeterminePermissionToAutomateTarget(&target, typeWildCard, typeWildCard, args.contains("--ask"))
AEDisposeDesc(&target)
switch st {
case noErr: print("ok")
case OSStatus(errAEEventWouldRequireUserConsent): print("consent")
case OSStatus(procNotFound): print("not-running")
case OSStatus(errAEEventNotPermitted): print("denied")
default: print(st)
}
