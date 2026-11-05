// winlist: on-screen windows of an app, one per line:
//   <window id>\t<x>,<y>,<w>,<h>\t<owner>\t<title>\t<owner pid>
// usage: winlist [owner]   (default owner "UTM"; "*" lists every app's windows,
//                           e.g. to pick a Wine program's by its process id)
// Window titles need Screen Recording permission for the calling terminal;
// without it titles are empty.
import CoreGraphics
import Foundation

let want = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "UTM"
guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID)
        as? [[String: Any]] else { exit(1) }
for w in list {
    let owner = w[kCGWindowOwnerName as String] as? String ?? ""
    guard want == "*" || owner == want,
          (w[kCGWindowLayer as String] as? Int) == 0 else { continue }
    let id = w[kCGWindowNumber as String] as? Int ?? 0
    let pid = w[kCGWindowOwnerPID as String] as? Int ?? 0
    let name = w[kCGWindowName as String] as? String ?? ""
    let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
    let x = b["X"] as? Int ?? 0, y = b["Y"] as? Int ?? 0
    let wd = b["Width"] as? Int ?? 0, ht = b["Height"] as? Int ?? 0
    print("\(id)\t\(x),\(y),\(wd),\(ht)\t\(owner)\t\(name)\t\(pid)")
}
