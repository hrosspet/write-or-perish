import SwiftUI

/// Minimal SVG path-data parser so the web's inline SVG icons can be drawn
/// natively, vector-sharp and tinted. Supports M L H V C S Q T Z (absolute and
/// relative). Arcs (A) are not supported; use an SF Symbol for icons that need them.
enum SVGPath {
    static func parse(_ d: String) -> Path {
        var path = Path()
        var tokens = Tokenizer(d)
        var current = CGPoint.zero
        var start = CGPoint.zero
        var lastControl: CGPoint?

        func offset(_ p: CGPoint, _ base: CGPoint) -> CGPoint { CGPoint(x: base.x + p.x, y: base.y + p.y) }

        while let token = tokens.next() {
            guard case .command(let command) = token else { continue } // stray number
            if command == "Z" || command == "z" {
                path.closeSubpath()
                current = start
                lastControl = nil
                continue
            }
            let relative = command.isLowercase
            var firstGroup = true
            // One command letter may be followed by several argument groups.
            while tokens.peekIsNumber() {
                let base = relative ? current : .zero
                switch command.uppercased().first! {
                case "M":
                    guard let p = tokens.point() else { return path }
                    current = offset(p, base)
                    if firstGroup {
                        path.move(to: current)
                        start = current
                    } else {
                        path.addLine(to: current) // implicit lineto after a moveto
                    }
                    lastControl = nil
                case "L":
                    guard let p = tokens.point() else { return path }
                    current = offset(p, base)
                    path.addLine(to: current)
                    lastControl = nil
                case "H":
                    guard let x = tokens.number() else { return path }
                    current = CGPoint(x: (relative ? current.x : 0) + x, y: current.y)
                    path.addLine(to: current)
                    lastControl = nil
                case "V":
                    guard let y = tokens.number() else { return path }
                    current = CGPoint(x: current.x, y: (relative ? current.y : 0) + y)
                    path.addLine(to: current)
                    lastControl = nil
                case "C":
                    guard let c1 = tokens.point(), let c2 = tokens.point(), let p = tokens.point() else { return path }
                    let control2 = offset(c2, base)
                    current = offset(p, base)
                    path.addCurve(to: current, control1: offset(c1, base), control2: control2)
                    lastControl = control2
                case "S":
                    guard let c2 = tokens.point(), let p = tokens.point() else { return path }
                    let reflected = lastControl.map { CGPoint(x: 2 * current.x - $0.x, y: 2 * current.y - $0.y) } ?? current
                    let control2 = offset(c2, base)
                    current = offset(p, base)
                    path.addCurve(to: current, control1: reflected, control2: control2)
                    lastControl = control2
                case "Q":
                    guard let c = tokens.point(), let p = tokens.point() else { return path }
                    let control = offset(c, base)
                    current = offset(p, base)
                    path.addQuadCurve(to: current, control: control)
                    lastControl = control
                case "T":
                    guard let p = tokens.point() else { return path }
                    let control = lastControl.map { CGPoint(x: 2 * current.x - $0.x, y: 2 * current.y - $0.y) } ?? current
                    current = offset(p, base)
                    path.addQuadCurve(to: current, control: control)
                    lastControl = control
                default:
                    _ = tokens.number() // unsupported command (e.g. A): skip its numbers
                }
                firstGroup = false
            }
        }
        return path
    }

    private enum Token { case command(Character), number(CGFloat) }

    private struct Tokenizer {
        private let chars: [Character]
        private var index = 0

        init(_ s: String) { chars = Array(s) }

        mutating func next() -> Token? {
            skipSeparators()
            guard index < chars.count else { return nil }
            let c = chars[index]
            if c.isLetter {
                index += 1
                return .command(c)
            }
            return readNumber().map { .number($0) }
        }

        mutating func peekIsNumber() -> Bool {
            skipSeparators()
            guard index < chars.count else { return false }
            let c = chars[index]
            return c.isNumber || c == "-" || c == "+" || c == "."
        }

        mutating func number() -> CGFloat? {
            skipSeparators()
            return readNumber()
        }

        mutating func point() -> CGPoint? {
            guard let x = number(), let y = number() else { return nil }
            return CGPoint(x: x, y: y)
        }

        private mutating func skipSeparators() {
            while index < chars.count, chars[index] == " " || chars[index] == "," || chars[index].isNewline
                    || chars[index] == "\t" {
                index += 1
            }
        }

        private mutating func readNumber() -> CGFloat? {
            var s = ""
            var sawDot = false
            var sawExp = false
            if index < chars.count, chars[index] == "-" || chars[index] == "+" {
                s.append(chars[index])
                index += 1
            }
            while index < chars.count {
                let c = chars[index]
                if c.isNumber {
                    s.append(c)
                } else if c == ".", !sawDot, !sawExp {
                    sawDot = true
                    s.append(c)
                } else if (c == "e" || c == "E"), !sawExp, !s.isEmpty {
                    sawExp = true
                    s.append(c)
                    if index + 1 < chars.count, chars[index + 1] == "-" || chars[index + 1] == "+" {
                        index += 1
                        s.append(chars[index])
                    }
                } else {
                    break
                }
                index += 1
            }
            return Double(s).map { CGFloat($0) }
        }
    }
}

/// A shape drawn from SVG path data in a `viewBox` of `size`, scaled to fit.
struct SVGShape: Shape {
    let path: Path
    let viewBox: CGSize

    init(_ d: String, viewBox: CGSize) {
        path = SVGPath.parse(d)
        self.viewBox = viewBox
    }

    func path(in rect: CGRect) -> Path {
        let scale = min(rect.width / viewBox.width, rect.height / viewBox.height)
        let dx = rect.minX + (rect.width - viewBox.width * scale) / 2
        let dy = rect.minY + (rect.height - viewBox.height * scale) / 2
        return path.applying(CGAffineTransform(a: scale, b: 0, c: 0, d: scale, tx: dx, ty: dy))
    }
}

/// The Loore mark: the ECG line of `loore-logo-transparent.svg`, drawn in `accent`.
struct LooreLogo: View {
    var size: CGFloat = 22
    var color: Color = LooreColor.accent

    private static let line = "M 43.9,285.3 L 117.0,285.3 L 153.6,263.3 L 182.9,299.9 L 241.4,87.8 L 307.2,424.2 L 351.1,190.2 L 380.3,285.3 L 409.6,285.3 L 468.1,285.3"
    private static let peak = "M 182.9,299.9 L 241.4,87.8 L 307.2,424.2 L 351.1,190.2"

    var body: some View {
        let box = CGSize(width: 512, height: 512)
        let scale = size / 512
        ZStack {
            SVGShape(Self.line, viewBox: box)
                .stroke(color, style: StrokeStyle(lineWidth: 27 * scale, lineCap: .round, lineJoin: .round))
            SVGShape(Self.peak, viewBox: box)
                .stroke(color.opacity(0.55), style: StrokeStyle(lineWidth: 41 * scale, lineCap: .round, lineJoin: .round))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The sliders glyph that marks craft mode (web `CraftIcon.js`).
struct CraftIcon: View {
    var size: CGFloat = 14

    var body: some View {
        let box = CGSize(width: 24, height: 24)
        ZStack {
            SVGShape("M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3", viewBox: box)
                .stroke(style: StrokeStyle(lineWidth: 1.6 * size / 24, lineCap: .round, lineJoin: .round))
            SVGShape("M1 14h6M9 8h6M17 16h6", viewBox: box)
                .stroke(style: StrokeStyle(lineWidth: 1.6 * size / 24, lineCap: .round, lineJoin: .round))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The X logo of the "Sign in with X" button.
struct XLogo: View {
    var size: CGFloat = 18

    var body: some View {
        SVGShape("M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z",
                 viewBox: CGSize(width: 24, height: 24))
            .fill(style: FillStyle(eoFill: false))
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}
