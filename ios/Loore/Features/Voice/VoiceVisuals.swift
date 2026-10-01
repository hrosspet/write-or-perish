import SwiftUI

/// The Voice page's ECG line (web `EcgAnimation`, viewBox 280×168): drawn in
/// once, then the peak breathes and a scanline sweeps while active.
struct EcgView: View {
    var active: Bool
    var showScanline = true
    var dim = false

    private static let line = "M 24,81.6 L 64,81.6 L 84,74.4 L 100,86.4 L 132,16.8 L 168,127.2 L 192,50.4 L 208,81.6 L 224,81.6 L 256,81.6"
    private static let pulse = "M 100,86.4 L 132,16.8 L 168,127.2 L 192,50.4"
    private static let box = CGSize(width: 280, height: 168)

    @State private var drawn: CGFloat = 0
    @State private var breathe = false
    @State private var scan = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .leading) {
            SVGShape(Self.line, viewBox: Self.box)
                .trim(from: 0, to: active ? drawn : 1)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 8, lineCap: .round, lineJoin: .round))
                .opacity(active ? 0.15 : 0.1)
                .blur(radius: 4)
            SVGShape(Self.line, viewBox: Self.box)
                .trim(from: 0, to: active ? drawn : 1)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
            SVGShape(Self.pulse, viewBox: Self.box)
                .trim(from: 0, to: active ? drawn : 1)
                .stroke(LooreColor.accent, style: StrokeStyle(lineWidth: 5, lineCap: .round, lineJoin: .round))
                .opacity(active ? (breathe ? 0.6 : 0.25) : 0.2)
                .shadow(color: LooreColor.accent.opacity(active && breathe ? 0.8 : 0.3), radius: breathe ? 14 : 6)
            if showScanline && active && !reduceMotion {
                GeometryReader { proxy in
                    LinearGradient(colors: [.clear, LooreColor.accent, .clear], startPoint: .top, endPoint: .bottom)
                        .frame(width: 3)
                        .blur(radius: 1)
                        .opacity(scan ? 0.3 : 0)
                        .offset(x: scan ? proxy.size.width : 0)
                }
            }
        }
        .frame(width: 280, height: 168)
        .opacity(dim ? 0.4 : 1)
        .animation(.easeOut(duration: 0.4), value: dim)
        .padding(.bottom, 32)
        .onAppear(perform: animate)
        .onChange(of: active) { _, _ in animate() }
        .accessibilityHidden(true)
    }

    private func animate() {
        guard active else { return }
        if reduceMotion {
            drawn = 1
            return
        }
        drawn = 0
        withAnimation(.timingCurve(0.22, 1, 0.36, 1, duration: 1.5).delay(0.6)) { drawn = 1 }
        breathe = false
        withAnimation(.easeInOut(duration: 1.5).repeatForever(autoreverses: true).delay(2.1)) { breathe = true }
        scan = false
        withAnimation(.easeInOut(duration: 3).repeatForever(autoreverses: false).delay(2.1)) { scan = true }
    }
}

/// 24 thin accent bars (web `WaveformBars`).
struct WaveformBars: View {
    var animated: Bool
    @State private var up = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private let peaks: [CGFloat] = (0..<24).map { _ in 12 + CGFloat.random(in: 0...20) }

    var body: some View {
        HStack(alignment: .center, spacing: 3) {
            ForEach(0..<24, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1)
                    .fill(LooreColor.accent)
                    .opacity(0.6)
                    .frame(width: 2, height: animated && up ? peaks[i] : 4)
                    .animation(animated && !reduceMotion
                               ? .easeInOut(duration: 1.2).repeatForever(autoreverses: true).delay(Double(i) * 0.05)
                               : .default, value: up)
            }
        }
        .frame(height: 32)
        .onAppear { up = animated }
        .onChange(of: animated) { _, on in up = on }
        .accessibilityHidden(true)
    }
}

/// Web `PulsingDot`.
struct PulsingDot: View {
    var color: Color = LooreColor.accent
    var size: CGFloat = 8
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .opacity(dim ? 0.3 : 1)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.75).repeatForever(autoreverses: true)) { dim = true }
            }
            .accessibilityHidden(true)
    }
}

/// The Voice page's round buttons: 72 pt record/stop/resume, 56 pt continue, 48 pt play.
struct VoiceRoundButton<Label: View>: View {
    var size: CGFloat = 72
    var enabled = true
    var dimmed: Double = 1
    let action: () -> Void
    @ViewBuilder var label: () -> Label

    var body: some View {
        Button(action: action) {
            label()
                .frame(width: size, height: size)
                .overlay(Circle().stroke(enabled ? LooreColor.accent : LooreColor.textMuted, lineWidth: 2))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? dimmed : 0.4)
    }
}

/// The record glyph (a filled 8 pt-radius dot in a 24 pt box).
struct RecordGlyph: View {
    var enabled = true
    var body: some View {
        Circle().fill(enabled ? LooreColor.accent : LooreColor.textMuted).frame(width: 16, height: 16)
    }
}

/// Web `OfflineBanner`.
struct OfflineNotice: View {
    var body: some View {
        if !NetworkStatus.shared.isOnline {
            Text("You're offline")
                .font(LooreFont.sans(13.6, .regular))
                .foregroundStyle(LooreColor.textMuted)
                .opacity(0.8)
                .padding(.bottom, 16)
        }
    }
}
