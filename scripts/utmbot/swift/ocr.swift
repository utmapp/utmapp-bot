// ocr - the text in an image, with where it is: one line per text box,
//   <x>\t<y>\t<width>\t<height>\t<confidence>\t<text>\t<words>
// in the image's pixels, origin top left (the coordinates `input mouse click`
// takes on a capture screen PNG).  Uses the Vision framework (accurate level).
// <words> places each word of a box that holds several (Vision often reads a
// menu bar as one line): space-separated <start>-<end>:<x>:<width>, start and
// end in Unicode scalars of <text>, so a click can go to the word itself.
//
// usage: ocr IMAGE.png
import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write("usage: ocr IMAGE\n".data(using: .utf8)!)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: url),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read \(url.path)\n".data(using: .utf8)!)
    exit(1)
}
let width = CGFloat(cg.width), height = CGFloat(cg.height)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
do {
    try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
} catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
for obs in request.results ?? [] {
    guard let top = obs.topCandidates(1).first else { continue }
    // Vision boxes are normalized with the origin at the bottom left
    let b = obs.boundingBox
    let x = Int(b.minX * width), y = Int((1 - b.maxY) * height)
    let w = Int(b.width * width), h = Int(b.height * height)
    let text = top.string
    var words: [String] = []
    var i = text.startIndex
    while i < text.endIndex {
        guard !text[i].isWhitespace else { i = text.index(after: i); continue }
        var j = i
        while j < text.endIndex && !text[j].isWhitespace { j = text.index(after: j) }
        if i != text.startIndex || j != text.endIndex,  // a single word needs no entry
           let r = try? top.boundingBox(for: i..<j)?.boundingBox {
            let s = text.unicodeScalars.distance(from: text.startIndex, to: i)
            let e = text.unicodeScalars.distance(from: text.startIndex, to: j)
            words.append("\(s)-\(e):\(Int(r.minX * width)):\(Int(r.width * width))")
        }
        i = j
    }
    print("\(x)\t\(y)\t\(w)\t\(h)\t\(String(format: "%.2f", top.confidence))\t\(text)\t\(words.joined(separator: " "))")
}
