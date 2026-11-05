// imgdiff a.png b.png -> mean absolute per-channel difference (0-255) over a downsampled grid
import CoreGraphics
import ImageIO
import Foundation
func load(_ p: String) -> CGImage { let s = CGImageSourceCreateWithURL(URL(fileURLWithPath: p) as CFURL, nil)!; return CGImageSourceCreateImageAtIndex(s, 0, nil)! }
func pixels(_ i: CGImage, _ w: Int, _ h: Int) -> [UInt8] {
    var b = [UInt8](repeating: 0, count: w * h * 4)
    let c = CGContext(data: &b, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    c.draw(i, in: CGRect(x: 0, y: 0, width: w, height: h)); return b }
let a = pixels(load(CommandLine.arguments[1]), 256, 192), b = pixels(load(CommandLine.arguments[2]), 256, 192)
var s = 0; var n = 0
for k in stride(from: 0, to: a.count, by: 4) { for j in 0..<3 { s += abs(Int(a[k+j]) - Int(b[k+j])); n += 1 } }
print(String(format: "%.2f", Double(s) / Double(n)))
