import SwiftUI

/// The speaker icon on nodes, profiles and saved references (web `SpeakerIcon`).
/// Shown when the user has voice mode or the node is public; for nodes whose
/// `ai_usage` is `none` it shows at 35 % and does nothing. The audio plays in
/// the global player (the mini-player above the tab bar).
struct SpeakerButton: View {
    let target: ListenTarget
    var content: String?
    var isPublic = false
    var aiUsage: AIUsage?
    /// Called once new TTS finished generating (the edit dialog then knows the node has audio).
    var onTtsGenerated: (() -> Void)?

    @Environment(AppState.self) private var app

    private var noAIAccess: Bool {
        if case .node = target, aiUsage == .off { return true }
        return false
    }

    var body: some View {
        if app.user?.voiceModeEnabled == true || isPublic {
            let loading = app.audio.isLoading(target)
            let playing = app.audio.isPlaying(target)
            Button {
                guard !noAIAccess, !loading else { return }
                app.audio.listen(to: target, content: content, onTtsGenerated: onTtsGenerated)
            } label: {
                Group {
                    if loading {
                        ProgressView().controlSize(.mini).tint(LooreColor.textSecondary)
                    } else {
                        Image(systemName: "speaker.wave.2.fill")
                            .font(.system(size: 13))
                            .foregroundStyle(playing ? LooreColor.accent : LooreColor.textSecondary)
                    }
                }
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(noAIAccess ? 0.35 : 1)
            .disabled(noAIAccess)
            .accessibilityLabel(noAIAccess ? "Audio off — No AI access" : loading ? "Generating audio" : "Play audio")
            .accessibilityIdentifier("speaker.\(target.id)")
        }
    }
}

/// Download a node's audio (web `DownloadAudioIcon`): recording, merged MP3, or
/// TTS, handed to the share sheet (save to Files, AirDrop, …).
struct DownloadAudioButton: View {
    let nodeId: Int
    var isPublic = false
    var aiUsage: AIUsage?

    @Environment(AppState.self) private var app
    @State private var loading = false
    @State private var file: URL?

    var body: some View {
        if app.user?.voiceModeEnabled == true || isPublic {
            let noAIAccess = aiUsage == .off
            Button {
                guard !noAIAccess, !loading else { return }
                loading = true
                Task {
                    defer { loading = false }
                    file = try? await AudioDownloader.download(nodeId: nodeId, api: app.api)
                }
            } label: {
                Group {
                    if loading {
                        ProgressView().controlSize(.mini).tint(LooreColor.textSecondary)
                    } else {
                        Image(systemName: "arrow.down.circle")
                            .font(.system(size: 13))
                            .foregroundStyle(LooreColor.textSecondary)
                    }
                }
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(noAIAccess ? 0.35 : 1)
            .disabled(noAIAccess)
            .accessibilityLabel(noAIAccess ? "Download off — No AI access" : "Download audio")
            .accessibilityIdentifier("download.\(nodeId)")
            .sheet(isPresented: Binding(get: { file != nil }, set: { if !$0 { file = nil } })) {
                if let file { ShareSheet(items: [file]) { try? FileManager.default.removeItem(at: file) } }
            }
        }
    }
}
