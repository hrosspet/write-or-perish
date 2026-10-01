import SwiftUI

/// The global player (web `GlobalAudioPlayer`, the mobile floating card), docked
/// above the tab bar on every screen except Voice (design doc §5): title, chapter
/// menu (> 1 chapter), play/pause, Stop (position 0, stays visible, #161),
/// −10 s / +10 s, rate 1 → 1.25 → 1.5 → 2, `m:ss / m:ss` with the generating
/// dot, a tap-to-seek bar, and ✕ Close (full teardown).
struct MiniPlayerView: View {
    @Environment(AppState.self) private var app

    var body: some View {
        if app.audio.showsMiniPlayer {
            card
                .padding(.horizontal, 12)
                .padding(.bottom, 8)
                .transition(.move(edge: .bottom).combined(with: .opacity))
        }
    }

    private var player: ChunkQueuePlayer { app.audio.player }

    private var card: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(player.title.isEmpty ? "Audio" : player.title)
                    .font(LooreFont.sans(13, .regular))
                    .foregroundStyle(LooreColor.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .accessibilityIdentifier("miniPlayer.title")
                if player.chapters.count > 1 {
                    chapterMenu
                }
                Spacer(minLength: 4)
                Button { player.close() } label: {
                    Text("✕").font(LooreFont.sans(16, .regular)).foregroundStyle(LooreColor.textMuted).padding(4)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close player")
                .accessibilityIdentifier("miniPlayer.close")
            }
            HStack(spacing: 10) {
                controlButton(player.isPlaying ? "pause.fill" : "play.fill", label: player.isPlaying ? "Pause" : "Play",
                              color: LooreColor.accent, size: 16) {
                    if player.isPlaying { player.pause() } else if player.atEnd { player.seek(to: 0); player.play() } else { player.play() }
                }
                .accessibilityIdentifier("miniPlayer.playPause")
                controlButton("stop.fill", label: "Stop", size: 13) { player.stop() }
                controlButton("gobackward.10", label: "Skip back 10 seconds", size: 14) { player.skip(by: -10) }
                controlButton("goforward.10", label: "Skip forward 10 seconds", size: 14) { player.skip(by: 10) }
                Button { player.cycleRate() } label: {
                    Text(rateText)
                        .font(LooreFont.sans(11, .medium))
                        .foregroundStyle(LooreColor.textPrimary)
                        .frame(minWidth: 38)
                        .padding(.vertical, 2)
                        .overlay(RoundedRectangle(cornerRadius: 4).stroke(LooreColor.border, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Change playback speed, now \(rateText)")
                HStack(spacing: 4) {
                    Text("\(AudioTimeFormat.clock(player.cumulativeTime)) / \(AudioTimeFormat.clock(player.totalDuration))")
                        .font(LooreFont.sans(11, .light))
                        .foregroundStyle(LooreColor.textMuted)
                        .monospacedDigit()
                        .lineLimit(1)
                        .fixedSize()
                    if player.generatingTTS { PulsingDot(size: 6) }
                }
                AudioProgressBar(player: player, height: 5, track: LooreColor.bgSurface)
                    .frame(minWidth: 40)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: LooreRadius.large).fill(LooreColor.bgCard))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).stroke(LooreColor.border, lineWidth: 1))
        .shadow(color: LooreColor.shadow.opacity(0.35), radius: 12, y: 4)
        .frame(maxWidth: 480)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("miniPlayer")
    }

    private var rateText: String {
        let r = player.rate
        return (r == r.rounded() ? String(Int(r)) : String(r)) + "x"
    }

    private var chapterMenu: some View {
        Menu {
            ForEach(Array(player.chapters.enumerated()), id: \.offset) { i, chapter in
                Button(chapter.title) {
                    player.seek(to: player.math.chapterStart(chapter))
                    if !player.isPlaying { player.play() }
                }
            }
        } label: {
            HStack(spacing: 2) {
                Text(player.currentChapterIndex.map { player.chapters[$0].title } ?? "Chapters")
                    .lineLimit(1)
                Image(systemName: "chevron.down").font(.system(size: 9))
            }
            .font(LooreFont.sans(11, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .frame(maxWidth: 140)
        }
        .accessibilityLabel("Chapter")
    }

    private func controlButton(_ symbol: String, label: String, color: Color = LooreColor.textPrimary,
                               size: CGFloat, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: size))
                .foregroundStyle(color)
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}
