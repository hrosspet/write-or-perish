import AVFoundation
import SwiftUI

/// Voice mode (web `VoicePage`, route `/voice?resume=&parent=`): record → Thinking →
/// the reply plays by itself, then Continue. The conversation lives in
/// `app.audio.voice`, so it survives tab switches; popping the screen ends it,
/// like the web page's unmount.
struct VoiceView: View {
    let parentId: Int?
    let resumeLLMId: Int?

    @Environment(AppState.self) private var app
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var interrupted: InterruptedDraft?
    @State private var recoveryChecked = false
    @State private var resumeAfterRecovery = false

    private var voice: VoiceTurnController { app.audio.voice }
    private var player: ChunkQueuePlayer { app.audio.player }
    private var online: Bool { NetworkStatus.shared.isOnline }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            ScrollView {
                if typeSize.isAccessibilitySize {
                    // Large text: the button scrolls with the page instead of covering its heading.
                    textModeButton
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .padding(.top, 12)
                        .padding(.trailing, 20)
                }
                content
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, LooreSpacing.lg)
                    .padding(.vertical, 40)
                    .containerRelativeFrame(.vertical, alignment: .center) { length, _ in length }
            }
            .scrollBounceBehavior(.basedOnSize)
            if !typeSize.isAccessibilitySize {
                textModeButton
                    .padding(.top, 12)
                    .padding(.trailing, 20)
            }
        }
        .loorePageBackground(glow: true)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear(perform: appear)
        .onDisappear(perform: disappear)
        .task { await checkInterrupted() }
    }

    // MARK: Phases

    @ViewBuilder private var content: some View {
        if !recoveryChecked {
            Color.clear.frame(height: 1)
        } else if let draft = interrupted, voice.phase != .recording {
            recoveryBanner(draft)
        } else {
            switch voice.phase {
            case .ready, .recording: recordingPhase
            case .processing: processingPhase
            case .playback: playbackPhase
            }
        }
    }

    private var recordingPhase: some View {
        let recording = voice.phase == .recording
        return VStack(spacing: 0) {
            Text("What's on your mind?")
                .font(LooreFont.serif(19.2, .light, italic: true, relativeTo: .title2))
                .foregroundStyle(LooreColor.textMuted)
                .padding(.bottom, 40)
            EcgView(active: recording && !voice.isPaused, showScanline: recording && !voice.isPaused,
                    dim: !recording)
                .id(recording)
            if recording {
                WaveformBars(animated: !voice.isStopping && !voice.isPaused)
                Text(AudioTimeFormat.recording(voice.elapsed) + (voice.isPaused ? " · paused" : ""))
                    .font(LooreFont.sans(19.2, .light))
                    .tracking(1.9)
                    .foregroundStyle(LooreColor.textSecondary)
                    .monospacedDigit()
                    .padding(.top, 16)
                    .padding(.bottom, 32)
                    .accessibilityIdentifier("voice.elapsed")
                if voice.isInterrupted {
                    Text("Recording paused — another app took the microphone. Everything up to the interruption is saved.")
                        .font(LooreFont.sans(13.6, .light))
                        .foregroundStyle(LooreColor.error)
                        .multilineTextAlignment(.center)
                        .lineSpacing(4)
                        .frame(maxWidth: 320)
                        .padding(.bottom, 24)
                }
            } else {
                OfflineNotice()
                if voice.hasError {
                    PulsingDot(color: LooreColor.error).padding(.bottom, 16)
                        // The web shows only the red dot; VoiceOver users get words for it.
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("Something went wrong.")
                }
            }
            recordControl
        }
        .multilineTextAlignment(.center)
    }

    @ViewBuilder private var recordControl: some View {
        if voice.phase == .ready {
            VoiceRoundButton(enabled: online, action: startRecording) {
                RecordGlyph(enabled: online)
            }
            .accessibilityLabel("Record")
            .accessibilityIdentifier("voice.record")
        } else if voice.isPaused && !voice.isStopping {
            VoiceRoundButton(action: { voice.resumeRecording() }) {
                Image(systemName: "play.fill").font(.system(size: 22)).foregroundStyle(LooreColor.accent)
            }
            .accessibilityLabel("Resume recording")
            .accessibilityIdentifier("voice.resume")
        } else {
            VoiceRoundButton(dimmed: voice.isStopping ? 0.5 : 1, action: { if !voice.isStopping { voice.stop() } }) {
                if voice.isStopping {
                    ProgressView().tint(LooreColor.accent)
                } else {
                    RoundedRectangle(cornerRadius: 2).fill(LooreColor.accent).frame(width: 14, height: 14)
                }
            }
            .accessibilityLabel("Stop and send")
            .accessibilityIdentifier("voice.stop")
        }
    }

    private var processingPhase: some View {
        VStack(spacing: 0) {
            EcgView(active: true, showScanline: false)
            PulsingDot()
            Text("Thinking...")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
                .padding(.top, 16)
                .accessibilityIdentifier("voice.thinking")
            Button { voice.cancelProcessing() } label: {
                Text("✕")
                    .font(LooreFont.sans(12.8, .regular))
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
            }
            .buttonStyle(.plain)
            .opacity(0.5)
            .padding(.top, 32)
            .accessibilityLabel("Cancel")
            .accessibilityIdentifier("voice.cancel")
        }
    }

    private var playbackPhase: some View {
        VStack(spacing: 0) {
            EcgView(active: player.isPlaying, showScanline: false, dim: !player.isPlaying)
            WaveformBars(animated: player.isPlaying).padding(.bottom, 24)
            VoicePlayerControls(player: player)
            VoiceChapterList(player: player)
            VoiceProposalSlot(content: voice.replyContent, nodeId: voice.lastReplyNodeId,
                              toolCallsMeta: voice.toolCallsMeta,
                              onContentChange: { voice.setReplyContent($0) },
                              onApplied: { voice.updateToolMeta($0, $1) })
            Spacer().frame(height: 32)
            OfflineNotice()
            VoiceRoundButton(size: 56, enabled: online, dimmed: 0.7, action: continueConversation) {
                RecordGlyph(enabled: online).scaleEffect(0.84)
            }
            .accessibilityLabel(online ? "Continue" : "You're offline")
            .accessibilityIdentifier("voice.continue")
        }
    }

    private func recoveryBanner(_ draft: InterruptedDraft) -> some View {
        VStack(spacing: 0) {
            EcgView(active: false, showScanline: false, dim: true)
            Text("Unfinished \(draft.label ?? "Voice") recording")
                .font(LooreFont.serif(16, .light, italic: true, relativeTo: .title3))
                .foregroundStyle(LooreColor.textSecondary)
                .multilineTextAlignment(.center)
                .padding(.bottom, 32)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) { recoveryButtons(draft) }
                VStack(spacing: 12) { recoveryButtons(draft) }
            }
        }
    }

    @ViewBuilder private func recoveryButtons(_ draft: InterruptedDraft) -> some View {
        Button("Continue recording") {
            interrupted = nil
            voice.resumeInterrupted(draft)
        }
        .buttonStyle(RecoveryButtonStyle(accent: true))
        .accessibilityIdentifier("voice.recovery.continue")
        Button("Discard") { discard(draft) }
            .buttonStyle(RecoveryButtonStyle(accent: false))
            .accessibilityIdentifier("voice.recovery.discard")
    }

    private var textModeButton: some View {
        Button {
            if let id = voice.lastReplyNodeId {
                app.open(.thread(id: id, awaitLLM: nil))
            } else {
                app.open(.textMode)
            }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: "keyboard").font(.system(size: 11)).accessibilityHidden(true)
                Text("Text Mode").font(LooreFont.sans(12.5, .light))
            }
            .foregroundStyle(LooreColor.textMuted)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(LooreColor.border, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityHint("Continue in Text Mode")
    }

    // MARK: Actions

    private func appear() {
        app.audio.voiceScreenVisible = true
        if let resumeLLMId, voice.state == .idle {
            voice.resumeReply(nodeId: resumeLLMId, parentId: parentId)
        } else if voice.state == .idle, let parentId {
            voice.setThreadParent(parentId)
        }
    }

    private func disappear() {
        app.audio.voiceScreenVisible = false
        // Popped (not a tab switch): end the conversation, like the web's unmount.
        let stillOpen = app.router.paths.values.contains { path in
            path.contains { if case .voice = $0 { return true } else { return false } }
        }
        if !stillOpen { voice.tearDown() }
    }

    private func startRecording() {
        // Block before any recording starts: a long recording stopped only at
        // the end would be lost work (web #85).
        if app.spendCapped {
            app.notifySpendBlocked()
            app.toasts.show(SpendCap.toastMessage(.record), duration: 8)
            return
        }
        Task {
            if !app.audio.usesDebugAudioFile {
                guard await ensureMicrophone() else { return }
                await LocalNotifier.requestAuthorizationIfNeeded()
            }
            voice.start()
        }
    }

    private func continueConversation() {
        if app.spendCapped {
            app.notifySpendBlocked()
            app.toasts.show(SpendCap.toastMessage(.record), duration: 8)
            return
        }
        voice.continueConversation()
    }

    private func ensureMicrophone() async -> Bool {
        switch AudioSessionController.recordPermission {
        case .granted: return true
        case .undetermined:
            if await AudioSessionController.requestRecordPermission() { return true }
        default: break
        }
        app.toasts.show("Microphone access is off for Loore. Turn it on in Settings → Loore → Microphone, then try again.",
                        duration: 8)
        return false
    }

    /// `GET /api/drafts/interrupted` once per visit (web `useInterruptedRecovery`).
    private func checkInterrupted() async {
        defer {
            recoveryChecked = true
            autoStartIfRequested()
        }
        guard voice.state == .idle, resumeLLMId == nil else { return }
        if let drafts: [InterruptedDraft] = try? await app.api.get(APIPath.interruptedDrafts),
           let first = drafts.first {
            interrupted = first
            if player.isPlaying {
                player.pause()
                resumeAfterRecovery = true
            }
        }
    }

    private func discard(_ draft: InterruptedDraft) {
        interrupted = nil
        Task { _ = try? await app.api.data(for: APIRequest(.delete, APIPath.streamingDiscard(draft.sessionId))) }
        if resumeAfterRecovery {
            resumeAfterRecovery = false
            player.play()
        }
    }

    /// Debug: `-LooreDebugVoiceAutoStart YES` with `-LooreDebugAudioFile` records at once.
    private func autoStartIfRequested() {
        #if DEBUG
        guard app.launch.debugAudioFile != nil, interrupted == nil, voice.state == .idle,
              UserDefaults.standard.bool(forKey: "LooreDebugVoiceAutoStart") else { return }
        startRecording()
        #endif
    }
}

private struct RecoveryButtonStyle: ButtonStyle {
    var accent: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.6, .regular))
            .foregroundStyle(accent ? LooreColor.accent : LooreColor.textMuted)
            .padding(.horizontal, 24)
            .padding(.vertical, 10)
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(accent ? LooreColor.accent : LooreColor.border, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.7 : 1)
    }
}

/// Skip −10 s, play/pause (replay from 0 at the end), skip +10 s, progress bar
/// with chapter ticks, time and duration with the generating dot (web VoicePage).
struct VoicePlayerControls: View {
    let player: ChunkQueuePlayer

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 16) {
                Button { player.skip(by: -10) } label: {
                    Image(systemName: "gobackward.10").font(.system(size: 18)).foregroundStyle(LooreColor.accent)
                        .padding(8)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Skip back 10 seconds")
                Button {
                    if player.isPlaying {
                        player.pause()
                    } else if player.atEnd {
                        player.seek(to: 0)
                        player.play()
                    } else {
                        player.play()
                    }
                } label: {
                    Image(systemName: player.isPlaying ? "pause.fill" : "play.fill")
                        .font(.system(size: 18))
                        .foregroundStyle(LooreColor.accent)
                        .offset(x: player.isPlaying ? 0 : 2)
                        .frame(width: 48, height: 48)
                        .overlay(Circle().stroke(LooreColor.accent, lineWidth: 2))
                        .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(player.isPlaying ? "Pause" : "Play")
                .accessibilityIdentifier("voice.playPause")
                Button { player.skip(by: 10) } label: {
                    Image(systemName: "goforward.10").font(.system(size: 18)).foregroundStyle(LooreColor.accent)
                        .padding(8)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Skip forward 10 seconds")
            }
            .padding(.bottom, 16)

            AudioProgressBar(player: player, height: 6, showTicks: true)
                .frame(maxWidth: 300)
                .padding(.bottom, 4)
            HStack {
                Text(AudioTimeFormat.clock(player.cumulativeTime))
                Spacer()
                HStack(spacing: 4) {
                    Text(AudioTimeFormat.clock(player.totalDuration))
                    if player.generatingTTS { PulsingDot(size: 6) }
                }
                .accessibilityElement(children: .combine)
                .accessibilityValue(player.generatingTTS ? "Audio still generating" : "")
            }
            .font(LooreFont.sans(12, .light))
            .foregroundStyle(LooreColor.textMuted)
            .monospacedDigit()
            .frame(maxWidth: 300)
            .padding(.bottom, 8)
            .accessibilityIdentifier("voice.time")
        }
    }
}

/// A tappable progress bar with chapter ticks (Voice page; the mini-player uses it without ticks).
struct AudioProgressBar: View {
    let player: ChunkQueuePlayer
    var height: CGFloat = 6
    var showTicks = false
    var track: Color = LooreColor.bgCard

    var body: some View {
        GeometryReader { proxy in
            let total = player.totalDuration
            let fraction = total > 0 ? min(1, player.cumulativeTime / total) : 0
            ZStack(alignment: .leading) {
                Capsule().fill(track)
                Capsule().fill(LooreColor.accent).frame(width: proxy.size.width * fraction)
                if showTicks && total > 0 && player.chapters.count > 1 {
                    ForEach(Array(player.chapters.dropFirst().enumerated()), id: \.offset) { _, chapter in
                        Rectangle()
                            .fill(LooreColor.bgDeep)
                            .frame(width: 1)
                            .offset(x: proxy.size.width * player.math.chapterStart(chapter) / total)
                    }
                }
            }
            .clipShape(Capsule())
            .contentShape(Rectangle())
            .onTapGesture { location in
                guard total > 0 else { return }
                player.seek(to: max(0, min(1, location.x / proxy.size.width)) * total)
            }
        }
        .frame(height: height)
        .accessibilityElement()
        .accessibilityLabel("Playback position")
        .accessibilityValue("\(AudioTimeFormat.clock(player.cumulativeTime)) of \(AudioTimeFormat.clock(player.totalDuration))")
        .accessibilityAdjustableAction { direction in
            player.skip(by: direction == .increment ? 10 : -10)
        }
    }
}

/// One movement per chain node: Roman numeral + first words; the one under
/// the playhead is lit; tap to jump and play (web VoicePage chapters).
struct VoiceChapterList: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    let player: ChunkQueuePlayer

    var body: some View {
        if player.chapters.count > 1 {
            let active = player.currentChapterIndex
            VStack(spacing: 3) {
                ForEach(Array(player.chapters.enumerated()), id: \.offset) { i, chapter in
                    Button {
                        player.seek(to: player.math.chapterStart(chapter))
                        if !player.isPlaying { player.play() }
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(ChapterTitle.numeral(i))
                                .foregroundStyle(active == i ? LooreColor.accent : LooreColor.textMuted)
                            Text(chapter.title)
                                .foregroundStyle(active == i ? LooreColor.textSecondary : LooreColor.textMuted)
                                .lineLimit(typeSize.isAccessibilitySize ? 3 : 1)
                                .truncationMode(.tail)
                                .frame(maxWidth: 280, alignment: .leading)
                        }
                        .font(LooreFont.sans(11.5, .light))
                        .tracking(0.2)
                        .padding(.vertical, 2)
                        .opacity(active == i ? 1 : 0.5)
                    }
                    .buttonStyle(.plain)
                }
            }
            .frame(maxWidth: 420)
            .padding(.top, 2)
            .padding(.bottom, 10)
        }
    }
}

/// The reply's proposal card (web `ProposalInline` with `tool_calls_meta`,
/// `size="roomy"` on the Voice page): M2's `ProposalCard` (compact) under the
/// player when the final reply carries proposal sections.
struct VoiceProposalSlot: View {
    let content: String?
    let nodeId: Int?
    let toolCallsMeta: [ToolCallMeta]?
    var onContentChange: (String) -> Void = { _ in }
    var onApplied: (String, [String: JSONValue]) -> Void = { _, _ in }

    var body: some View {
        if let content, let nodeId, ProposalParser.hasProposalSections(content) {
            ProposalCard(content: content, nodeId: nodeId, toolCallsMeta: toolCallsMeta,
                         onContentChange: onContentChange, onApplied: onApplied)
                .frame(maxWidth: 520)
                .padding(.top, 8)
        }
    }
}
