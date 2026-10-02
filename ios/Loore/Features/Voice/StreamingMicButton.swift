import SwiftUI

/// Record into the writing form (web `StreamingMicButton`): Record → `m:ss` (stop)
/// → Finalizing… → the transcript lands in the form. After an interruption the
/// button becomes Resume and a "Stop & save" button appears. "Save audio" shares
/// the recording so far. Apart from the running clock and "Retry" the buttons
/// are icons with spoken labels (#398: the words broke over two lines on a phone).
struct StreamingMicButton: View {
    let model: NodeFormModel

    @Environment(AppState.self) private var app
    @State private var dictation: DictationController?
    @State private var sharing = false
    @State private var sharedFile: URL?

    private var online: Bool { NetworkStatus.shared.isOnline }

    var body: some View {
        let controller = dictation
        let state = controller?.state ?? .idle
        let interrupted = controller?.isInterrupted == true
        let idleOffline = !online && state == .idle
        let disabled = model.loading || model.uploadedFile != nil || model.aiUsage == .off
        let blocked = disabled || idleOffline || state == .initializing || state == .finalizing

        VStack(alignment: .leading, spacing: 4) {
            if !online && state == .recording {
                notice("Offline — recording continues, uploads will retry when connection returns", color: LooreColor.accent)
            }
            if interrupted && state == .recording {
                notice("Recording paused — another app took the microphone. Audio up to the interruption is saved.",
                       color: LooreColor.error)
            }
            HStack(spacing: 8) {
                Button { press(state: state, interrupted: interrupted) } label: {
                    HStack(spacing: 6) { label(state: state, interrupted: interrupted, idleOffline: idleOffline) }
                }
                .buttonStyle(LooreButtonStyle(kind: .outline, iconOnly: !showsWords(state: state, interrupted: interrupted)))
                .disabled(blocked)
                .opacity(disabled || idleOffline ? 0.35 : 1)
                .accessibilityLabel(spokenLabel(state: state, interrupted: interrupted, idleOffline: idleOffline))
                .accessibilityValue(state == .recording && !interrupted ? AudioTimeFormat.clock(dictation?.elapsed ?? 0) : "")
                .accessibilityHint(model.audioDisabledReason ?? (idleOffline ? "You're offline — reconnect to record audio." : ""))
                .accessibilityIdentifier("nodeForm.record")

                if interrupted && state == .recording {
                    Button { controller?.stop() } label: { ButtonIcon(systemName: "stop.fill") }
                        .buttonStyle(.looreIcon)
                        .accessibilityLabel("Stop & save")
                        .accessibilityHint("Stop and save what was recorded so far")
                }
                if controller?.hasRecording == true {
                    Button {
                        sharedFile = controller?.writeRecordingFile()
                        sharing = sharedFile != nil
                    } label: {
                        ButtonIcon(systemName: "arrow.down.circle")
                    }
                    .buttonStyle(.looreIcon)
                    .accessibilityLabel("Save audio")
                    .accessibilityIdentifier("nodeForm.saveAudio")
                }
            }
        }
        .sheet(isPresented: $sharing, onDismiss: removeSharedFile) {
            if let file = sharedFile { ShareSheet(items: [file]) }
        }
    }

    /// The saved copy is deleted once the share sheet closes (M2).
    private func removeSharedFile() {
        if let sharedFile { PrivateFiles.remove(sharedFile) }
        sharedFile = nil
    }

    /// What VoiceOver says for the record button (its icons are hidden; the
    /// recording state shows only a clock).
    private func spokenLabel(state: DictationController.State, interrupted: Bool, idleOffline: Bool) -> String {
        switch state {
        case .idle, .initializing: return idleOffline ? "Offline" : "Record"
        case .recording: return interrupted ? "Resume recording" : "Stop recording"
        case .finalizing: return "Finalizing"
        case .error: return "Error. Retry recording"
        }
    }

    /// The running clock and "Retry" are words; every other state is an icon.
    private func showsWords(state: DictationController.State, interrupted: Bool) -> Bool {
        (state == .recording && !interrupted) || state == .error
    }

    @ViewBuilder
    private func label(state: DictationController.State, interrupted: Bool, idleOffline: Bool) -> some View {
        switch state {
        case .idle, .initializing:
            ButtonIcon(systemName: idleOffline ? "mic.slash" : "mic")
        case .recording:
            if interrupted {
                ButtonIcon(systemName: "play.fill")
            } else {
                Image(systemName: "stop.fill").font(.system(size: 12)).accessibilityHidden(true)
                Text(AudioTimeFormat.clock(dictation?.elapsed ?? 0)).monospacedDigit()
            }
        case .finalizing:
            ZStack {
                ButtonIcon(systemName: "mic").hidden()
                ProgressView().controlSize(.mini)
            }
        case .error:
            Image(systemName: "exclamationmark.circle").font(.system(size: 13)).accessibilityHidden(true)
            Text("Retry")
        }
    }

    private func notice(_ text: String, color: Color) -> some View {
        Text(text)
            .font(LooreFont.sans(12, .light))
            .foregroundStyle(color)
            .lineSpacing(3)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 6).fill(LooreColor.bgCard))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(color, lineWidth: 1))
            .fixedSize(horizontal: false, vertical: true)
    }

    private func press(state: DictationController.State, interrupted: Bool) {
        switch state {
        case .idle:
            let controller = dictation ?? makeController()
            controller.start(parentId: model.config.parentId, privacy: model.privacy.rawString,
                             aiUsage: model.aiUsage.rawString)
        case .recording:
            interrupted ? dictation?.resume() : dictation?.stop()
        case .error:
            dictation?.clearError()
        case .initializing, .finalizing:
            break
        }
    }

    private func makeController() -> DictationController {
        let controller = DictationController(app: app)
        let model = model
        controller.callbacks = .init(
            started: { model.dictationStarted() },
            transcript: { model.dictationTranscript($0) },
            finished: { model.dictationFinished(sessionId: $0, transcript: $1) },
            failed: { message, capped in
                if message == nil && !capped { model.isRecording = false } else { model.dictationFailed(message, spendCapped: capped) }
            })
        dictation = controller
        return controller
    }
}
