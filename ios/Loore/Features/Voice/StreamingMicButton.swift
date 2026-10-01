import SwiftUI

/// Record into the writing form (web `StreamingMicButton`): Record → `m:ss` (stop)
/// → Finalizing… → the transcript lands in the form. After an interruption the
/// button reads Resume and a "Stop & save" button appears. "Save audio" shares
/// the recording so far.
struct StreamingMicButton: View {
    let model: NodeFormModel

    @Environment(AppState.self) private var app
    @State private var dictation: DictationController?
    @State private var sharing = false

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
            HStack(spacing: 6) {
                Button { press(state: state, interrupted: interrupted) } label: {
                    HStack(spacing: 6) { label(state: state, interrupted: interrupted, idleOffline: idleOffline) }
                }
                .buttonStyle(.looreOutline)
                .disabled(blocked)
                .opacity(disabled || idleOffline ? 0.35 : 1)
                .accessibilityLabel(spokenLabel(state: state, interrupted: interrupted, idleOffline: idleOffline))
                .accessibilityValue(state == .recording && !interrupted ? AudioTimeFormat.clock(dictation?.elapsed ?? 0) : "")
                .accessibilityHint(model.audioDisabledReason ?? (idleOffline ? "You're offline — reconnect to record audio." : ""))
                .accessibilityIdentifier("nodeForm.record")

                if interrupted && state == .recording {
                    Button { controller?.stop() } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "stop.fill").font(.system(size: 11)).accessibilityHidden(true)
                            Text("Stop & save")
                        }
                    }
                    .buttonStyle(.looreOutline)
                    .accessibilityHint("Stop and save what was recorded so far")
                }
                if controller?.recordingFile != nil {
                    Button { sharing = true } label: {
                        HStack(spacing: 4) {
                            Image(systemName: "arrow.down.circle").font(.system(size: 12)).accessibilityHidden(true)
                            Text("Save audio")
                        }
                    }
                    .buttonStyle(.looreOutline)
                    .accessibilityIdentifier("nodeForm.saveAudio")
                }
            }
        }
        .sheet(isPresented: $sharing) {
            if let file = controller?.recordingFile { ShareSheet(items: [file]) }
        }
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

    @ViewBuilder
    private func label(state: DictationController.State, interrupted: Bool, idleOffline: Bool) -> some View {
        switch state {
        case .idle, .initializing:
            Image(systemName: "mic.fill").font(.system(size: 13))
            Text(idleOffline ? "Offline" : "Record")
        case .recording:
            if interrupted {
                Image(systemName: "play.fill").font(.system(size: 12))
                Text("Resume")
            } else {
                Image(systemName: "stop.fill").font(.system(size: 12))
                Text(AudioTimeFormat.clock(dictation?.elapsed ?? 0)).monospacedDigit()
            }
        case .finalizing:
            ProgressView().controlSize(.mini)
            Text("Finalizing...")
        case .error:
            Image(systemName: "exclamationmark.circle").font(.system(size: 13))
            Text("Error - Retry")
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
