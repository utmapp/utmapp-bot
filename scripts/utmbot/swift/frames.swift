// frames: sample a window with ScreenCaptureKit and log every delivered frame.
//
// usage: frames <window id> <seconds> <out dir> [--fps N] [--save-every SECONDS]
//
// Writes <out dir>/frames.csv with one row per delivered frame:
//   t_ms,hash,luma,r,g,b
// hash is a 64-bit FNV-1a over a 64x36 grid sampled from the window's
// interior (title bar and borders excluded), luma the mean luma of that grid
// and r,g,b its mean channel values (0-255; for colour tints and swapped
// channels).  A frame whose hash differs from the previous one is a new guest
// frame; ScreenCaptureKit also delivers repeats when the window re-presents
// the same image, so counting delivered frames overstates the frame rate.
// Every --save-every seconds one PNG is written as <out dir>/t<ms>.png.
//
// Unlike `screencapture -l`, this does not stall while the window renders,
// and unlike recording a screen region it never captures the lock screen.
import AppKit
import CoreImage
import Foundation
import ScreenCaptureKit

let args = CommandLine.arguments
guard args.count >= 4, let wid = UInt32(args[1]), let secs = Double(args[2]) else {
    FileHandle.standardError.write("usage: frames <window id> <seconds> <out dir> [--fps N] [--save-every S]\n".data(using: .utf8)!)
    exit(2)
}
let outDir = URL(fileURLWithPath: args[3])
var fps = 120
var saveEvery = 0.0
var i = 4
while i < args.count {
    if args[i] == "--fps", i + 1 < args.count { fps = Int(args[i + 1]) ?? fps; i += 2 }
    else if args[i] == "--save-every", i + 1 < args.count { saveEvery = Double(args[i + 1]) ?? 0; i += 2 }
    else { i += 1 }
}
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
FileManager.default.createFile(atPath: outDir.appendingPathComponent("frames.csv").path, contents: nil)
let csv = try! FileHandle(forWritingTo: outDir.appendingPathComponent("frames.csv"))
csv.write("t_ms,hash,luma,r,g,b\n".data(using: .utf8)!)

final class Sink: NSObject, SCStreamOutput {
    let start = Date()
    var lastSave = -1e9
    let ctx = CIContext()
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        // skip SCK's idle/blank status frames
        if let att = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
           let raw = att.first?[.status] as? Int, let st = SCFrameStatus(rawValue: raw), st != .complete {
            return
        }
        CVPixelBufferLockBaseAddress(pb, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        let bpr = CVPixelBufferGetBytesPerRow(pb)
        guard let base = CVPixelBufferGetBaseAddress(pb)?.assumingMemoryBound(to: UInt8.self) else { return }
        // interior: skip a title bar (top 8%) and 3% borders
        let x0 = w * 3 / 100, x1 = w - x0, y0 = h * 8 / 100, y1 = h - h * 3 / 100
        var hash: UInt64 = 0xcbf29ce484222325
        var lsum = 0.0, rsum = 0.0, gsum = 0.0, bsum = 0.0
        let gx = 64, gy = 36
        for yi in 0..<gy {
            let y = y0 + (y1 - y0) * yi / gy
            for xi in 0..<gx {
                let x = x0 + (x1 - x0) * xi / gx
                let p = base + y * bpr + x * 4 // BGRA
                let b = p[0], g = p[1], r = p[2]
                for v in [b, g, r] { hash = (hash ^ UInt64(v)) &* 0x100000001b3 }
                rsum += Double(r); gsum += Double(g); bsum += Double(b)
                lsum += 0.2126 * Double(r) + 0.7152 * Double(g) + 0.0722 * Double(b)
            }
        }
        let t = Date().timeIntervalSince(start) * 1000
        let n = Double(gx * gy)
        csv.write(String(format: "%.1f,%016llx,%.1f,%.1f,%.1f,%.1f\n", t, hash, lsum / n,
                         rsum / n, gsum / n, bsum / n).data(using: .utf8)!)
        if saveEvery > 0 && t - lastSave >= saveEvery * 1000 {
            lastSave = t
            let ci = CIImage(cvPixelBuffer: pb)
            if let cg = ctx.createCGImage(ci, from: ci.extent) {
                let rep = NSBitmapImageRep(cgImage: cg)
                if let png = rep.representation(using: .png, properties: [:]) {
                    try? png.write(to: outDir.appendingPathComponent(String(format: "t%06.0f.png", t)))
                }
            }
        }
    }
}

let sem = DispatchSemaphore(value: 0)
var stream: SCStream?
let sink = Sink()
Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        guard let win = content.windows.first(where: { $0.windowID == wid }) else {
            FileHandle.standardError.write("window \(wid) not found (Screen Recording permission?)\n".data(using: .utf8)!)
            exit(1)
        }
        let filter = SCContentFilter(desktopIndependentWindow: win)
        let cfg = SCStreamConfiguration()
        let scale = Int(NSScreen.main?.backingScaleFactor ?? 2)
        cfg.width = Int(win.frame.width) * scale
        cfg.height = Int(win.frame.height) * scale
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.queueDepth = 8
        cfg.showsCursor = false
        let s = SCStream(filter: filter, configuration: cfg, delegate: nil)
        try s.addStreamOutput(sink, type: .screen, sampleHandlerQueue: DispatchQueue(label: "frames"))
        try await s.startCapture()
        stream = s
        try await Task.sleep(nanoseconds: UInt64(secs * 1e9))
        try await s.stopCapture()
    } catch {
        FileHandle.standardError.write("capture failed: \(error)\n".data(using: .utf8)!)
        exit(1)
    }
    sem.signal()
}
while sem.wait(timeout: .now() + 0.05) == .timedOut {
    RunLoop.main.run(until: Date().addingTimeInterval(0.05))
}
csv.closeFile()
