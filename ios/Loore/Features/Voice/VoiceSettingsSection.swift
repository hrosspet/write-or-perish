import SwiftUI

/// Account → Voice (design doc §9.4): the thinking-cue volume, stored per device.
/// The app plays a soft cue between Stop and the reply's first words; it keeps
/// the audio running so iOS does not pause Loore while the phone is locked.
struct VoiceSettingsSection: View {
    @State private var level = ThinkingCueLevel.current
    @Environment(AppState.self) private var app

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("VOICE")
                .font(LooreFont.eyebrow)
                .tracking(1.2)
                .foregroundStyle(LooreColor.textMuted)
            Text("Sound while Loore thinks")
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.textPrimary)
            HStack(spacing: 8) {
                ForEach(ThinkingCueLevel.allCases) { option in
                    Button {
                        level = option
                        ThinkingCueLevel.current = option
                        preview(option)
                    } label: {
                        Text(option.title)
                            .font(LooreFont.sans(13.6, level == option ? .regular : .light))
                            .foregroundStyle(level == option ? LooreColor.accent : LooreColor.textSecondary)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 7)
                            .overlay(RoundedRectangle(cornerRadius: LooreRadius.control)
                                .stroke(level == option ? LooreColor.accent : LooreColor.border, lineWidth: 1))
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(level == option ? .isSelected : [])
                    .accessibilityIdentifier("account.voiceCue.\(option.rawValue)")
                }
            }
            if level == .off {
                Text(ThinkingCueLevel.offWarning)
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.warning)
                    .lineSpacing(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// A short sample of the chosen level (not while a voice turn uses the audio).
    private func preview(_ option: ThinkingCueLevel) {
        guard option != .off, !(app.audio.hasVoiceController && app.audio.voice.isActive) else { return }
        app.audio.session.activateForPlayback()
        app.audio.sounds.startCue(level: option)
        Task {
            try? await Task.sleep(nanoseconds: 2_500_000_000)
            app.audio.sounds.stopCue()
            if !app.audio.player.isPlaying { app.audio.session.deactivate() }
        }
    }
}
