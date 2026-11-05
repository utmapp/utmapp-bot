// screenaccess - is Screen Recording allowed for the app this runs under
// (the terminal)?  If not, ask: macOS shows its dialog and lists the app in
// System Settings > Privacy & Security > Screen & System Audio Recording.
// Window capture alone never asks, so without this the app never appears there.
import CoreGraphics
if CGPreflightScreenCaptureAccess() {
    print("granted")
} else {
    _ = CGRequestScreenCaptureAccess()
    print("requested")
}
