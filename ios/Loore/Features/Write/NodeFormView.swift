import SwiftUI
import UniformTypeIdentifiers

/// The writing form (web `NodeForm`): text, server draft, craft-mode privacy
/// and AI-usage selectors, Send / Discard draft / Record / Upload, and the
/// dialog chain before a save. The host owns what happens after a send.
struct NodeFormView: View {
    let config: NodeFormConfig
    let onSuccess: (NodeFormResult) -> Void
    @Environment(AppState.self) private var app
    @Environment(\.scenePhase) private var scenePhase
    @State private var model: NodeFormModel?

    var body: some View {
        Group {
            if let model {
                NodeFormBody(model: model)
            } else {
                Color.clear.frame(height: config.compact ? 90 : 120)
            }
        }
        .onAppear {
            if model == nil {
                let m = NodeFormModel(config: config, app: app)
                m.onSuccess = onSuccess
                model = m
                // Not tied to a view task: a re-render must not cancel the draft load.
                Task { await m.start() }
            }
            model?.onSuccess = onSuccess
            model?.formAppeared()
        }
        .onDisappear {
            model?.formDisappeared()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background { model?.drafts.flushInBackground() }
        }
    }
}

private struct NodeFormBody: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    @Bindable var model: NodeFormModel
    @Environment(AppState.self) private var app
    @FocusState private var focused: Bool
    @State private var pickingFile = false

    private var config: NodeFormConfig { model.config }

    /// Test identifiers: "nodeForm.text.edit", ".inline" (thread) or ".new".
    private var idSuffix: String { config.isEdit ? "edit" : config.compact ? "inline" : "new" }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            editor
            if !config.hidePowerFeatures {
                PrivacySelectorView(privacy: $model.privacy, aiUsage: $model.aiUsage, disabled: model.loading,
                                    lockPrivacy: model.lockPrivacy)
            }
            if model.showsAgenticToggles {
                LabeledPillToggle(label: "Agentic Reply", isOn: $model.useAgenticPrompt,
                                  onText: "AI will include your profile, recent entries, todos and preferences as context.",
                                  offText: "No agentic context. AI replies only see this entry.",
                                  disabled: model.loading)
                LabeledPillToggle(label: "Auto-generate", isOn: $model.useAutoGenerate,
                                  onText: "AI will reply automatically after you submit.",
                                  offText: "No AI reply. You can click LLM Response on the created node later.",
                                  disabled: model.loading)
            }
            statusLines
            if let file = model.uploadedFile {
                HStack {
                    Text("\(file.name) (\(String(format: "%.2f", Double(file.size) / 1024 / 1024)) MB)")
                        .lineLimit(1)
                    Spacer()
                    Button("Remove") { model.uploadedFile = nil }
                        .buttonStyle(LooreButtonStyle(kind: .outline, font: LooreFont.sans(13, .regular)))
                }
                .font(LooreFont.sans(13.6, .regular))
                .foregroundStyle(LooreColor.textSecondary)
                .padding(.vertical, 8)
                .padding(.horizontal, 12)
                .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
                .padding(.top, 8)
            }
            buttons.padding(.top, 8)
        }
        .looreDialog(isPresented: Binding(get: { model.dialog != nil }, set: { if !$0 { model.cancelDialog() } })) {
            switch model.dialog {
            case .tts?:
                RegenerateTtsDialog(onChoice: model.answerTts, onCancel: model.cancelDialog)
            case .scope?:
                ApplyToRepliesDialog(
                    privacyChanged: config.edit?.initialPrivacy != nil && model.privacy != config.edit?.initialPrivacy,
                    aiUsageChanged: config.edit?.initialAIUsage != nil && model.aiUsage != config.edit?.initialAIUsage,
                    onChoice: model.answerScope, onCancel: model.cancelDialog)
            case .publicReply?:
                PublicReplyDialog(onConfirm: model.answerPublicReply, onCancel: model.cancelDialog)
            case .split?:
                SplitContentDialog(charCount: model.pendingPaste?.jsLength ?? model.content.jsLength,
                                   onConfirm: model.confirmSplit, onCancel: model.cancelSplit)
            case nil:
                EmptyView()
            }
        }
        .fileImporter(isPresented: $pickingFile, allowedContentTypes: [.audio, .mpeg4Movie, .movie]) { result in
            if case .success(let url) = result { model.pickFile(url) }
        }
    }

    private var editor: some View {
        let binding = Binding<String>(
            get: { model.content },
            set: { new in
                let old = model.content
                model.content = new
                model.contentChanged(from: old, to: new)
            })
        return TextField("", text: binding,
                         prompt: loorePrompt(config.placeholder ?? "What's present for you right now..."),
                         axis: .vertical)
            .lineLimit(config.compact ? 3...12 : 6...18)
            .font(LooreFont.sans(16.8, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .lineSpacing(5)
            .focused($focused)
            .disabled(!config.isEdit && model.uploadedFile != nil)
            .padding(.vertical, 14)
            .padding(.horizontal, 16)
            .frame(minHeight: config.compact ? 90 : min(400, max(120, UIScreen.main.bounds.height * 0.4)),
                   alignment: .topLeading)
            .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
                .strokeBorder(focused ? LooreColor.accentDim : LooreColor.border))
            .contentShape(Rectangle())
            .onTapGesture { focused = true }
            .accessibilityLabel(config.placeholder ?? "What's present for you right now...")
            .accessibilityIdentifier("nodeForm.text.\(idSuffix)")
    }

    @ViewBuilder private var statusLines: some View {
        VStack(alignment: .leading, spacing: 4) {
            if model.isRecoveringAudio {
                Text("Transcribing recovered audio...")
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .padding(.vertical, 8)
                    .padding(.horizontal, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
            }
            if !model.isOnline {
                Text("You're offline").font(LooreFont.sans(13.6, .regular)).foregroundStyle(LooreColor.textMuted)
            }
            if let error = model.error {
                Text(error).font(LooreFont.sans(14.4, .regular)).foregroundStyle(LooreColor.accent)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.content.jsTrimmed.isEmpty {
                if model.drafts.isSaving {
                    Text("Saving...").foregroundStyle(LooreColor.accent)
                } else if let saved = model.drafts.lastSaved {
                    Text("Draft saved \(DraftAutosaver.timeAgo(saved))").foregroundStyle(LooreColor.textMuted)
                }
            }
        }
        .font(LooreFont.sans(13.6, .light))
        .padding(.top, 4)
    }

    private var buttons: some View {
        // Large text: the buttons wrap onto more rows instead of running off the screen.
        let layout = typeSize.isAccessibilitySize
            ? AnyLayout(FlowLayout(spacing: 8, lineSpacing: 8))
            : AnyLayout(HStackLayout(spacing: 8))
        return layout {
            Button {
                focused = false
                Task { await model.submit() }
            } label: {
                Text(model.sendLabel)
            }
            .buttonStyle(.loorePrimary)
            .disabled(model.loading || !model.isOnline || model.isRecording)
            .keyboardShortcut(.return, modifiers: .command)
            .accessibilityIdentifier("nodeForm.send.\(idSuffix)")
            if model.hasDraft {
                Button("Discard draft") { model.discardDraft() }
                    .buttonStyle(.looreOutline)
                    .disabled(model.loading)
            }
            if !config.isEdit {
                DictationButton(model: model)
                if !config.hideAudioUpload {
                    Button("Upload") {
                        if model.uploadPressed() { pickingFile = true }
                    }
                    .buttonStyle(.looreOutline)
                    .disabled(model.isRecording || model.aiUsage == .off || !model.isOnline)
                    .accessibilityHint(model.audioDisabledReason ?? "")
                }
            }
            Spacer(minLength: 0)
        }
    }
}

/// Dictation into the writing form (M3): the streaming recorder in
/// `Features/Voice/StreamingMicButton.swift` drives `model.dictation*`.
struct DictationButton: View {
    let model: NodeFormModel

    var body: some View {
        StreamingMicButton(model: model)
    }
}
