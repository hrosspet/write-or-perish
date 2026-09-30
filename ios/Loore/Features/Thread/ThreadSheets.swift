import SwiftUI

/// The thread's modals: Reply and Edit Text forms, the prompt-edit confirm,
/// and the delete dialogs (map D §4.7–4.8).
struct ThreadSheets: ViewModifier {
    @Bindable var model: ThreadModel
    @Binding var autoGenerate: Bool
    @Environment(AppState.self) private var app

    func body(content: Content) -> some View {
        content
            .sheet(item: $model.replyTarget) { target in
                NodeFormSheet(title: "Reply",
                              config: NodeFormConfig(parentId: target.id, hidePowerFeatures: !app.capabilities.craftMode)) { result in
                    model.replyTarget = nil
                    if let id = result.id { app.open(.thread(id: id, awaitLLM: nil)) }
                } onClose: {
                    model.replyTarget = nil
                }
            }
            .sheet(isPresented: $model.showEditForm, onDismiss: { model.editTarget = nil }) {
                if let target = model.editTarget {
                    NodeFormSheet(title: "Edit Text", config: NodeFormConfig(
                        edit: .init(nodeId: target.id, initialContent: target.content, initialPrivacy: target.privacy,
                                    initialAIUsage: target.aiUsage, detachPrompt: target.hasPromptArtifact,
                                    hasGeneratedTTS: target.hasTTS, hasChildren: target.hasChildren))) { result in
                        Task { await model.editSaved(result, autoGenerate: $autoGenerate) }
                    } onClose: {
                        model.showEditForm = false
                    }
                }
            }
            .looreDialog(isPresented: $model.showPromptEditConfirm) {
                PromptEditConfirmDialog(
                    onConfirm: {
                        model.showPromptEditConfirm = false
                        model.showEditForm = true
                    },
                    onCancel: {
                        model.showPromptEditConfirm = false
                        model.editTarget = nil
                    },
                    onOpenPrompts: {
                        model.showPromptEditConfirm = false
                        model.editTarget = nil
                        app.open(.prompts)
                    })
            }
            .looreDialog(isPresented: Binding(get: { model.deleteTarget != nil },
                                               set: { if !$0 { model.deleteTarget = nil } })) {
                DeleteConfirmDialog(mode: .single(hasChildren: model.deleteTarget?.hasChildren ?? false),
                                    onConfirm: { model.confirmDelete(withDescendants: $0) },
                                    onCancel: { model.deleteTarget = nil })
            }
            .looreDialog(isPresented: $model.showPromptDeleteDialog) {
                DeleteConfirmDialog(mode: .prompt(listedIn: model.threadRootIsPublic ? "your public page" : "your Log"),
                                    onConfirm: { model.confirmPromptDelete(includePrompt: $0) },
                                    onCancel: {
                                        model.showPromptDeleteDialog = false
                                        model.pendingPromptDelete = nil
                                    })
            }
    }
}

/// A writing form in a sheet (web `NodeFormModal`): serif title, × close,
/// swipe down to dismiss. Unsaved text survives in the server draft.
struct NodeFormSheet: View {
    let title: String
    let config: NodeFormConfig
    let onSuccess: (NodeFormResult) -> Void
    let onClose: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title)
                        .font(LooreFont.serif(22.4, .light, relativeTo: .title2))
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                    Spacer()
                    Button(action: onClose) {
                        Text("×").font(LooreFont.sans(24, .light)).foregroundStyle(LooreColor.textMuted)
                            .frame(width: 36, height: 36)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Close")
                    .keyboardShortcut(.cancelAction)
                }
                NodeFormView(config: config, onSuccess: onSuccess)
            }
            .padding(LooreSpacing.lg)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(LooreColor.bgCard.ignoresSafeArea())
        .presentationDragIndicator(.visible)
    }
}

/// The focal footer's speaker (listen) and download buttons (web `SpeakerIcon` +
/// `DownloadAudioIcon`, M3): shown with voice mode or on a public node, at 35 %
/// and inert when AI usage is none; audio plays in the global mini-player.
struct NodeAudioControls: View {
    let nodeId: Int
    let content: String
    let isPublic: Bool
    let aiUsage: AIUsage
    let hasTTS: Bool
    /// New TTS was generated from the speaker (web sets `has_tts` on the node).
    var onTtsGenerated: (() -> Void)?

    var body: some View {
        SpeakerButton(target: .node(nodeId), content: content, isPublic: isPublic, aiUsage: aiUsage,
                      onTtsGenerated: onTtsGenerated)
        DownloadAudioButton(nodeId: nodeId, isPublic: isPublic, aiUsage: aiUsage)
    }
}
