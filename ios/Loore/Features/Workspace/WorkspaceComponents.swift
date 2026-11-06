import SwiftUI

// Pieces shared by Profile, Todo, Artifacts and Prompts (map E §1): the
// ArtifactsNav bubble row, the "● vN · date" chip, the "history" link, the
// meta line, the accent divider and the raw-markdown editor.

/// The Artifacts tab root: one workspace whose document switches in place
/// (web `/profile`, `/todo`, `/artifacts/:kind` sharing `ArtifactsNav`).
/// It also carries out the bubbles' menus (#603): Edit opens a document's edit
/// form, Delete asks, deletes and opens the next bubble.
struct WorkspaceView: View {
    /// When set, the view shows this document instead of the router's (a
    /// workspace route pushed on another tab).
    var pinned: WorkspaceDocument?
    @Environment(AppState.self) private var app
    @State private var local: WorkspaceDocument?
    @State private var actions = WorkspaceActions()

    var body: some View {
        Group {
            switch document {
            case .profile:
                ProfilePage(onSelect: select)
            case .todo:
                TodoPage(onSelect: select)
            case .artifact(let kind):
                ArtifactsPage(kind: kind, creating: false, onSelect: select)
                    .id("artifact-\(kind)")
            case .create:
                ArtifactsPage(kind: nil, creating: true, onSelect: select)
                    .id("artifact-create")
            }
        }
        .environment(actions)
        .navigationBarTitleDisplayMode(.inline)
        .looreDialog(isPresented: Binding(get: { actions.deleteTarget != nil },
                                          set: { if !$0 { actions.deleteTarget = nil } })) {
            if let target = actions.deleteTarget {
                DeleteConfirmDialog(mode: .artifact(title: WorkspaceMenu.label(target),
                                                    unsavedEdits: actions.unsavedKind == target.kind),
                                    onConfirm: { _ in delete(target) },
                                    onCancel: { actions.deleteTarget = nil })
            }
        }
    }

    private var document: WorkspaceDocument {
        local ?? pinned ?? app.router.workspace
    }

    private func select(_ document: WorkspaceDocument) {
        // An edit chosen for another document does not outlive this switch.
        if actions.editRequest != document { actions.editRequest = nil }
        if pinned != nil {
            local = document
        } else {
            app.router.workspace = document
        }
    }

    /// Deletes every version; then the bubble leaves the row and, when it was
    /// open, the next bubble opens in the same update (so the page never shows
    /// "This artifact was deleted." in between). An error leaves everything as it was.
    private func delete(_ target: ArtifactDoc) {
        actions.deleteTarget = nil
        let title = WorkspaceMenu.label(target)
        Task {
            switch await app.artifacts.delete(target.kind, api: app.api) {
            case .failed(let message):
                app.toasts.show(message, duration: 4)
            case .deleted:
                let row = WorkspaceMenu.bubbles(app.artifacts.artifacts)
                let next = WorkspaceMenu.selection(afterDeleting: target.kind, active: document, row: row)
                withAnimation(LooreMotion.quick) {
                    app.artifacts.markDeleted(target.kind)
                    if let next { select(next) }
                }
                app.toasts.show("\(title) deleted", duration: 3)
                app.signals.post(.artifactsChanged)
            }
        }
    }
}

/// The wrapped row of bubbles (web `ArtifactsNav`): Profile, Intentions, Todo,
/// the built-in kinds, custom kinds by title, then "+". A long press opens a
/// bubble's menu (#603, `WorkspaceMenu.items`); a tap selects.
struct ArtifactsNavRow: View {
    let active: WorkspaceDocument
    let onSelect: (WorkspaceDocument) -> Void
    @Environment(AppState.self) private var app
    @Environment(WorkspaceActions.self) private var actions: WorkspaceActions?

    var body: some View {
        FlowLayout(spacing: 8, lineSpacing: 8) {
            ForEach(WorkspaceMenu.bubbles(app.artifacts.artifacts)) { bubble in
                withMenu(bubble)
            }
        }
        .padding(.bottom, 20)
        .task { await app.artifacts.refresh(api: app.api) }
        .onChange(of: app.signals.artifactsChanged) { _, _ in
            Task { await app.artifacts.refresh(api: app.api) }
        }
    }

    @ViewBuilder private func withMenu(_ bubble: WorkspaceBubble) -> some View {
        let items = actions == nil ? [] : WorkspaceMenu.items(for: bubble)
        if items.isEmpty {
            button(bubble)
        } else {
            button(bubble)
                // The lifted preview is the capsule, not its bounding rectangle.
                .contentShape(.contextMenuPreview, Capsule())
                .contextMenu {
                    Button { edit(bubble) } label: { Label("Edit", systemImage: "pencil") }
                    if items.contains(.delete), let artifact = bubble.artifact {
                        Button(role: .destructive) { requestDelete(artifact) } label: {
                            Label("Delete", systemImage: "trash")
                        }
                    }
                }
                .accessibilityAction(named: "Edit") { edit(bubble) }
                .modifier(DeleteAccessibilityAction(artifact: items.contains(.delete) ? bubble.artifact : nil,
                                                    perform: requestDelete))
        }
    }

    private func button(_ bubble: WorkspaceBubble) -> some View {
        let document = bubble.document
        let isActive = active == document
        return Button { if !isActive || document == .create { onSelect(document) } } label: {
            Text(bubble.title)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(isActive ? LooreColor.textPrimary : LooreColor.textMuted)
                .padding(.vertical, 6)
                .padding(.horizontal, 14)
                // The page colour rather than clear: the same in the row, and the
                // menu's lifted preview does not show the system platter.
                .background(isActive ? LooreColor.bgCard : LooreColor.bgDeep, in: Capsule())
                .overlay(Capsule().strokeBorder(isActive ? LooreColor.accent : LooreColor.border, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isActive ? .isSelected : [])
        .accessibilityLabel(document == .create ? "Create a new artifact" : bubble.title)
        .accessibilityHint(bubble.artifact?.description ?? "")
        .accessibilityIdentifier("artifactsNav.\(bubble.title)")
    }

    /// Opens the document's edit form: selects it first when another one is open
    /// (through the page's unsaved-changes guard).
    private func edit(_ bubble: WorkspaceBubble) {
        actions?.editRequest = bubble.document
        if bubble.document != active { onSelect(bubble.document) }
    }

    private func requestDelete(_ artifact: ArtifactDoc) {
        actions?.deleteTarget = artifact
    }
}

/// VoiceOver's "Delete" action on a bubble that can be deleted.
private struct DeleteAccessibilityAction: ViewModifier {
    let artifact: ArtifactDoc?
    let perform: (ArtifactDoc) -> Void

    func body(content: Content) -> some View {
        if let artifact {
            content.accessibilityAction(named: "Delete") { perform(artifact) }
        } else {
            content
        }
    }
}

/// H1 (serif 2rem, 300) plus the header extras, wrapping like the web's flex row.
struct DocTitleRow<Extras: View>: View {
    let title: String
    @ViewBuilder var extras: () -> Extras

    var body: some View {
        FlowLayout(spacing: 16, lineSpacing: 6) {
            Text(title)
                .font(LooreFont.pageTitle)
                .foregroundStyle(LooreColor.textPrimary)
                .accessibilityAddTraits(.isHeader)
            extras()
        }
        .padding(.bottom, 8)
    }
}

/// "● v{N} · {date}" (green dot): tapping enters edit mode, or saves while editing.
struct VersionChip: View {
    let text: String
    var dotColor: Color = LooreColor.success
    var textColor: Color = LooreColor.textMuted
    var accessibilityHint = "Edit"
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Circle().fill(dotColor).frame(width: 6, height: 6)
                Text(text)
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(textColor)
            }
            .frame(minHeight: 32)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint(accessibilityHint)
        .accessibilityIdentifier("doc.versionChip")
    }
}

/// The underlined "history" link.
struct HistoryLink: View {
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text("history")
                .font(LooreFont.sans(12, .light))
                .underline()
                .foregroundStyle(LooreColor.textMuted.opacity(0.7))
                .frame(minHeight: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Version history")
        .accessibilityIdentifier("doc.history")
    }
}

/// The muted meta line under the header (sans 0.75rem, 70 %).
struct DocMetaLine: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.meta)
            .foregroundStyle(LooreColor.textMuted.opacity(0.7))
            .fixedSize(horizontal: false, vertical: true)
            .padding(.bottom, 16)
    }
}

/// The 1 pt accent divider under a document header.
struct DocDivider: View {
    var body: some View {
        Rectangle()
            .fill(LooreColor.accentDim.opacity(0.3))
            .frame(height: 1)
            .padding(.bottom, 24)
            .accessibilityHidden(true)
    }
}

/// The raw-markdown editor (web 400px textarea: `bg-input`, radius 8, sans .85rem 300).
struct DocEditor: View {
    @Binding var text: String
    var minHeight: CGFloat = 400
    var placeholder: String?
    var monospaced = false
    var identifier = "doc.editor"
    /// What VoiceOver calls the editor (its placeholder is not read as a label).
    var label = "Document text"
    @FocusState private var focused: Bool

    var body: some View {
        ZStack(alignment: .topLeading) {
            if text.isEmpty, let placeholder {
                Text(placeholder)
                    .font(font)
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.horizontal, 16 + 5)
                    .padding(.vertical, 16 + 8)
                    .allowsHitTesting(false)
            }
            TextEditor(text: $text)
                .font(font)
                .foregroundStyle(LooreColor.textPrimary)
                .lineSpacing(monospaced ? 3 : 4.5)
                .scrollContentBackground(.hidden)
                .padding(12)
                .focused($focused)
                .accessibilityLabel(label)
                .accessibilityIdentifier(identifier)
        }
        .frame(minHeight: minHeight)
        .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
            .strokeBorder(focused ? LooreColor.accent : LooreColor.border, lineWidth: 1))
    }

    private var font: Font {
        monospaced ? LooreFont.mono(13) : LooreFont.sans(13.6, .light)
    }
}

/// Save (filled accent, "Saving...") and Cancel (outline, muted), as on the
/// web's document pages. ⌘↩ saves with a hardware keyboard.
struct DocEditButtons: View {
    let saving: Bool
    var saveDisabled = false
    let onSave: () -> Void
    let onCancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Button(saving ? "Saving..." : "Save", action: onSave)
                .buttonStyle(.looreFilled)
                .disabled(saving || saveDisabled)
                .keyboardShortcut(.return, modifiers: .command)
                .accessibilityIdentifier("doc.save")
            Button("Cancel", action: onCancel)
                .buttonStyle(.looreOutline)
                .keyboardShortcut(.cancelAction)
                .accessibilityIdentifier("doc.cancel")
        }
        .padding(.top, 12)
    }
}

/// Page scaffold for workspace documents: the nav row, then the page, with the
/// web's 24 pt gutters and reading width.
struct WorkspaceScroll<Content: View>: View {
    let active: WorkspaceDocument
    let onSelect: (WorkspaceDocument) -> Void
    @ViewBuilder var content: () -> Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ArtifactsNavRow(active: active, onSelect: onSelect)
                content()
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(800)
        }
        .scrollDismissesKeyboard(.interactively)
        .loorePageBackground()
    }
}

/// The pulsing accent line (web `animation: pulse 2s ease-in-out infinite`).
struct PulsingText: View {
    let text: String
    var color: Color = LooreColor.accent
    var pulsing = true
    @State private var dim = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Text(text)
            .font(LooreFont.sans(13.6, .light))
            .foregroundStyle(color)
            .opacity(pulsing && dim ? 0.4 : 1)
            .onAppear {
                guard pulsing, !reduceMotion else { return }
                withAnimation(.easeInOut(duration: 1).repeatForever(autoreverses: true)) { dim = true }
            }
    }
}
