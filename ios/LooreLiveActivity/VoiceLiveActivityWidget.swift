import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct LooreLiveActivityBundle: WidgetBundle {
    var body: some Widget {
        VoiceLiveActivityWidget()
    }
}

/// The voice conversation on the lock screen and in the Dynamic Island (#397):
/// while recording only Pause/Resume and Stop; once Loore replies, Record, and
/// in a Glean-card conversation a labeled Glean button beside it (#475). The
/// reply's own play/pause and ±10 s stay in the Now Playing controls.
struct VoiceLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: VoiceActivityAttributes.self) { context in
            LockScreenView(state: context.state)
                .activityBackgroundTint(Palette.background)
                .activitySystemActionForegroundColor(Palette.accent)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    LooreMark(size: 30).padding(.leading, 6).padding(.top, 6)
                }
                DynamicIslandExpandedRegion(.center) {
                    StatusText(state: context.state)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Controls(state: context.state, size: 40, showsGlean: false).padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    // The Glean button gets its own row here: the trailing region is narrow.
                    if let glean = context.state.glean {
                        GleanButtonView(glean: glean, height: 36)
                            .frame(maxWidth: .infinity, alignment: .trailing)
                            .padding(.trailing, 4)
                    }
                }
            } compactLeading: {
                LooreMark(size: 20)
            } compactTrailing: {
                CompactStatus(state: context.state)
            } minimal: {
                LooreMark(size: 18)
            }
            .keylineTint(Palette.accent)
        }
    }
}

/// The app's dark tokens (the extension cannot read the app's `LooreColor`).
enum Palette {
    static let background = Color(red: 0x0E / 255, green: 0x0D / 255, blue: 0x0B / 255)
    static let accent = Color(red: 0xC4 / 255, green: 0x95 / 255, blue: 0x6A / 255)
    static let text = Color(red: 0xED / 255, green: 0xE8 / 255, blue: 0xDD / 255)
    static let textSecondary = Color(red: 0xA8 / 255, green: 0x9F / 255, blue: 0x91 / 255)
}

private typealias Phase = VoiceActivityAttributes.ContentState.Phase

private struct LockScreenView: View {
    let state: VoiceActivityAttributes.ContentState

    var body: some View {
        HStack(spacing: 14) {
            LooreMark(size: 34)
            StatusText(state: state)
            Spacer(minLength: 8)
            Controls(state: state, size: 46)
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 16)
    }
}

/// Title and the line under it (the recording clock, or what is happening).
private struct StatusText: View {
    let state: VoiceActivityAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(.headline, weight: .medium))
                .foregroundStyle(Palette.text)
                .lineLimit(1)
                // Room for the Glean button beside Record (#475).
                .minimumScaleFactor(0.8)
            detail
                .font(.system(.subheadline).monospacedDigit())
                .foregroundStyle(Palette.textSecondary)
                .lineLimit(2)
        }
    }

    private var title: String {
        switch state.phase {
        case .ready: return state.gleaned ? "Gleaning ready" : "Voice"
        case .starting: return "Starting…"
        case .recording: return "Recording"
        case .paused: return "Paused"
        case .interrupted: return "Recording paused"
        case .sending: return "Sending…"
        case .thinking: return "Thinking…"
        case .replying: return "Loore is replying"
        case .finished: return "Reply finished"
        case .gleaning: return "Gleaning…"
        }
    }

    @ViewBuilder private var detail: some View {
        switch state.phase {
        case .recording:
            if let start = state.clockStart {
                Text(timerInterval: start...Date.distantFuture, countsDown: false)
            }
        case .paused:
            Text(clock(state.elapsed))
        case .interrupted:
            Text("\(clock(state.elapsed)) · a call or another app took the microphone")
        case .ready where state.gleaned:
            Text("Open Loore to read it")
        case .gleaning:
            Text("Loore · Glean")
        case .ready, .starting, .sending, .thinking, .replying, .finished:
            Text("Loore · Voice")
        }
    }
}

/// The Dynamic Island's compact right side.
private struct CompactStatus: View {
    let state: VoiceActivityAttributes.ContentState

    var body: some View {
        switch state.phase {
        case .recording:
            if let start = state.clockStart {
                Text(timerInterval: start...Date.distantFuture, countsDown: false)
                    .monospacedDigit()
                    .frame(maxWidth: 52)
                    .foregroundStyle(Palette.accent)
            }
        case .paused, .interrupted:
            Image(systemName: "pause.fill").foregroundStyle(Palette.accent)
        case .starting, .sending, .thinking, .gleaning:
            Image(systemName: "ellipsis").foregroundStyle(Palette.accent)
        case .replying:
            Image(systemName: "waveform").foregroundStyle(Palette.accent)
        case .finished, .ready:
            Image(systemName: "mic.fill").foregroundStyle(Palette.accent)
        }
    }
}

/// Recording: Pause (or Resume) and Stop. Replying or done: Record, after the
/// Glean button in a Glean-card conversation. Gleaning: nothing to press.
private struct Controls: View {
    let state: VoiceActivityAttributes.ContentState
    let size: CGFloat
    /// The Dynamic Island shows Glean in its own row.
    var showsGlean = true

    var body: some View {
        HStack(spacing: 10) {
            if showsGlean, let glean = state.glean {
                GleanButtonView(glean: glean, height: size)
            }
            switch state.phase {
            case .recording:
                RoundButton(intent: PauseVoiceRecordingIntent(), symbol: "pause.fill",
                            label: "Pause recording", filled: false, size: size)
                RoundButton(intent: StopVoiceRecordingIntent(), symbol: "stop.fill",
                            label: "Stop and send", filled: true, size: size)
            case .paused, .interrupted:
                RoundButton(intent: ResumeVoiceRecordingIntent(), symbol: "play.fill",
                            label: "Resume recording", filled: false, size: size)
                RoundButton(intent: StopVoiceRecordingIntent(), symbol: "stop.fill",
                            label: "Stop and send", filled: true, size: size)
            case .replying, .finished, .ready:
                RoundButton(intent: RecordVoiceReplyIntent(), symbol: "mic.fill",
                            label: state.phase == .ready ? "Record" : "Record a reply", filled: true, size: size)
            case .starting, .sending, .thinking, .gleaning:
                EmptyView()
            }
        }
    }
}

/// The labeled Glean button (#475): a word, not an icon, so it never reads as
/// a second record button. Dimmed and not pressable while the reply comes.
private struct GleanButtonView: View {
    let glean: VoiceActivityAttributes.ContentState.GleanButton
    let height: CGFloat

    var body: some View {
        if glean == .ready {
            Button(intent: GleanVoiceIntent()) { label }
                .buttonStyle(.plain)
                .accessibilityLabel("Glean")
        } else {
            label
                .opacity(0.45)
                .accessibilityLabel("Glean, after Loore's reply")
        }
    }

    private var label: some View {
        Text("Glean")
            .font(.system(.subheadline, weight: .semibold))
            .foregroundStyle(Palette.accent)
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 14)
            .frame(height: height)
            .background(Capsule().fill(Palette.accent.opacity(0.14)))
            .overlay(Capsule().strokeBorder(Palette.accent, lineWidth: 1.5))
            .contentShape(Capsule())
    }
}

private struct RoundButton<Intent: AppIntent>: View {
    let intent: Intent
    let symbol: String
    let label: String
    let filled: Bool
    let size: CGFloat

    var body: some View {
        Button(intent: intent) {
            Image(systemName: symbol)
                .font(.system(size: size * 0.38, weight: .semibold))
                .foregroundStyle(filled ? Palette.background : Palette.accent)
                .frame(width: size, height: size)
                .background(Circle().fill(filled ? Palette.accent : Color.clear))
                .overlay(Circle().strokeBorder(Palette.accent, lineWidth: filled ? 0 : 1.5))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// The Loore mark (the app's `LooreLogo`: an ECG line with a stronger peak).
struct LooreMark: View {
    var size: CGFloat

    private static let line: [CGPoint] = [
        CGPoint(x: 43.9, y: 285.3), CGPoint(x: 117.0, y: 285.3), CGPoint(x: 153.6, y: 263.3),
        CGPoint(x: 182.9, y: 299.9), CGPoint(x: 241.4, y: 87.8), CGPoint(x: 307.2, y: 424.2),
        CGPoint(x: 351.1, y: 190.2), CGPoint(x: 380.3, y: 285.3), CGPoint(x: 409.6, y: 285.3),
        CGPoint(x: 468.1, y: 285.3),
    ]
    private static let peak: [CGPoint] = [
        CGPoint(x: 182.9, y: 299.9), CGPoint(x: 241.4, y: 87.8), CGPoint(x: 307.2, y: 424.2),
        CGPoint(x: 351.1, y: 190.2),
    ]

    var body: some View {
        let scale = size / 512
        ZStack {
            Polyline(points: Self.line)
                .stroke(Palette.accent, style: StrokeStyle(lineWidth: 27 * scale, lineCap: .round, lineJoin: .round))
            Polyline(points: Self.peak)
                .stroke(Palette.accent.opacity(0.55),
                        style: StrokeStyle(lineWidth: 41 * scale, lineCap: .round, lineJoin: .round))
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// Points in a 512×512 box, scaled to the shape's rect.
private struct Polyline: Shape {
    let points: [CGPoint]

    func path(in rect: CGRect) -> Path {
        let sx = rect.width / 512, sy = rect.height / 512
        var path = Path()
        for (i, p) in points.enumerated() {
            let q = CGPoint(x: rect.minX + p.x * sx, y: rect.minY + p.y * sy)
            if i == 0 { path.move(to: q) } else { path.addLine(to: q) }
        }
        return path
    }
}

private func clock(_ seconds: Int) -> String {
    let h = seconds / 3600, m = (seconds % 3600) / 60, s = seconds % 60
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
}
