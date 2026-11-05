// videoframes.swift - pull still frames out of a video attachment (mp4, mov,
// webm where macOS can decode it) with AVFoundation, since ffmpeg is not
// assumed.
//
// usage: videoframes VIDEO OUTDIR [COUNT=12] [--at SECONDS ...]
//                                   [--every SECONDS [--from S] [--to S]]
//
// --every samples at a fixed interval (between --from and --to), dense
// enough to follow a walk through menus or a level step by step.
//
// Prints the duration, size and nominal frame rate (a screen recording's
// real rate varies), then writes COUNT frames spread evenly over the video
// (or the given times) as OUTDIR/frame-<ms>.png (dir 700, files 600: they are
// a user's private data).  Each line carries the frame's time and its mean
// difference from the previous frame on a 64x64 grayscale thumbnail
// (0-255): near 0 means the picture did not change (a moving pointer alone
// stays below ~1), so a run of near-zero values is a freeze.
import AVFoundation
import AppKit
import Foundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

var args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2 else { fail("usage: videoframes VIDEO OUTDIR [COUNT] [--at S ...] [--every S [--from S] [--to S]]") }
let video = URL(fileURLWithPath: args.removeFirst())
let outDir = URL(fileURLWithPath: args.removeFirst())
var count = 12
var times: [Double] = []
var every: Double? = nil
var from = 0.0
var to: Double? = nil
var i = 0
while i < args.count {
    if args[i] == "--at", i + 1 < args.count, let t = Double(args[i + 1]) {
        times.append(t); i += 2
    } else if args[i] == "--every", i + 1 < args.count, let t = Double(args[i + 1]), t > 0 {
        every = t; i += 2
    } else if args[i] == "--from", i + 1 < args.count, let t = Double(args[i + 1]) {
        from = max(0, t); i += 2
    } else if args[i] == "--to", i + 1 < args.count, let t = Double(args[i + 1]) {
        to = t; i += 2
    } else if let n = Int(args[i]) {
        count = max(1, min(n, 200)); i += 1
    } else {
        fail("unexpected argument \(args[i])")
    }
}
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true,
                                         attributes: [.posixPermissions: 0o700])

func thumb(_ image: CGImage) -> [UInt8] {
    let n = 64
    var px = [UInt8](repeating: 0, count: n * n)
    px.withUnsafeMutableBytes { buf in
        if let ctx = CGContext(data: buf.baseAddress, width: n, height: n, bitsPerComponent: 8,
                               bytesPerRow: n, space: CGColorSpaceCreateDeviceGray(),
                               bitmapInfo: CGImageAlphaInfo.none.rawValue) {
            ctx.interpolationQuality = .medium
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: n, height: n))
        }
    }
    return px
}
var previous: [UInt8]? = nil

let asset = AVURLAsset(url: video)
let sem = DispatchSemaphore(value: 0)
var duration = 0.0
var size = CGSize.zero
var fps: Float = 0
Task {
    do {
        duration = try await asset.load(.duration).seconds
        if let track = try await asset.loadTracks(withMediaType: .video).first {
            size = try await track.load(.naturalSize)
            fps = try await track.load(.nominalFrameRate)
        }
    } catch {
        fail("cannot read \(video.lastPathComponent): \(error.localizedDescription)")
    }
    sem.signal()
}
sem.wait()
guard duration.isFinite, duration > 0 else { fail("no playable video track (format not decodable by AVFoundation?)") }
print(String(format: "duration %.2fs size %.0fx%.0f fps %.2f", duration, size.width, size.height, fps))

if let step = every {
    let end = min(to ?? duration, duration)
    var t = from
    while t <= end && times.count < 600 {
        times.append(t)
        t += step
    }
} else if times.isEmpty {
    times = (0..<count).map { duration * (Double($0) + 0.5) / Double(count) }
}
let gen = AVAssetImageGenerator(asset: asset)
gen.appliesPreferredTrackTransform = true
gen.requestedTimeToleranceBefore = CMTime(seconds: 0.05, preferredTimescale: 600)
gen.requestedTimeToleranceAfter = CMTime(seconds: 0.05, preferredTimescale: 600)
for t in times {
    let time = CMTime(seconds: min(t, duration), preferredTimescale: 600)
    let s2 = DispatchSemaphore(value: 0)
    gen.generateCGImageAsynchronously(for: time) { image, actual, error in
        defer { s2.signal() }
        guard let image = image else {
            print("frame at \(t)s: \(error?.localizedDescription ?? "failed")")
            return
        }
        let rep = NSBitmapImageRep(cgImage: image)
        let ms = Int(actual.seconds * 1000)
        let url = outDir.appendingPathComponent(String(format: "frame-%07d.png", ms))
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let t = thumb(image)
        var diff = "-"
        if let p = previous {
            let sum = zip(t, p).reduce(0) { $0 + abs(Int($1.0) - Int($1.1)) }
            diff = String(format: "%.2f", Double(sum) / Double(t.count))
        }
        previous = t
        print(String(format: "t=%.2fs diff=%@ %@", actual.seconds, diff, url.path))
    }
    s2.wait()
}
