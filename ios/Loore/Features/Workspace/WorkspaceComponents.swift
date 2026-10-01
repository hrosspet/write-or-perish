import SwiftUI

// Pieces shared by Profile, Todo, Artifacts and Prompts (map E §1): the
// ArtifactsNav bubble row, the "● vN · date" chip, the "history" link, the
// meta line, the accent divider and the raw-markdown editor.

/// The Artifacts tab root: one workspace whose document switches in place
/// (web `/profile`, `/todo`, `/artifacts/:kind` sharing `ArtifactsNav`).
struct WorkspaceView: View {
    /// When set, the view shows this document instead of the router's (a
    /// workspace route pushed on another tab).
    var pinned: WorkspaceDocument?
    @Environment(AppState.self) private var app
    @State private var local: WorkspaceDocument?

    var body: some View {
        let document = local ?? pinned ?? app.router.workspace
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
        .navigationBarTitleDisplayMode(.inline)
    }

    private func select(_ document: WorkspaceDocument) {
        if pinned != nil {
            local = document
        } else {
            app.router.workspace = document
        }
    }
}

/// The wrapped row of bubbles (web `ArtifactsNav`): Profile, Intentions, Todo,
/// the built-in kinds, custom kinds by title, then "+".
struct ArtifactsNavRow: View {
    let active: WorkspaceDocument
    let onSelect: (WorkspaceDocument) -> Void
    @Environment(AppState.self) private var app

    var body: some View {
        let artifacts = app.artifacts.artifacts
        let byKind = Dictionary(artifacts.map { ($0.kind, $0) }, uniquingKeysWith: { first, _ in first })
        let pinned = ArtifactKinds.builtinKindOrder.filter { $0 != "intentions" }.compactMap { byKind[$0] }
        let custom = artifacts.filter { !ArtifactKinds.isBuiltinKind($0.kind) }
            .sorted { ArtifactKinds.titleSortsBefore($0.title.isEmpty ? $0.kind : $0.title,
                                                    $1.title.isEmpty ? $1.kind : $1.title) }
        FlowLayout(spacing: 8, lineSpacing: 8) {
            bubble("Profile", document: .profile)
            if let intentions = byKind["intentions"] { artifactBubble(intentions) }
            bubble("Todo", document: .todo)
            ForEach(pinned) { artifactBubble($0) }
            ForEach(custom) { artifactBubble($0) }
            bubble("+", document: .create)
                .accessibilityLabel("Create a new artifact")
        }
        .padding(.bottom, 20)
        .task { await app.artifacts.refresh(api: app.api) }
        .onChange(of: app.signals.artifactsChanged) { _, _ in
            Task { await app.artifacts.refresh(api: app.api) }
        }
    }

    private func artifactBubble(_ artifact: ArtifactDoc) -> some View {
        bubble(artifact.title.isEmpty ? artifact.kind : artifact.title, document: .artifact(artifact.kind))
            .accessibilityHint(artifact.description ?? "")
    }

    private func bubble(_ title: String, document: WorkspaceDocument) -> some View {
        let isActive = active == document
        return Button { if !isActive || document == .create { onSelect(document) } } label: {
            Text(title)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(isActive ? LooreColor.textPrimary : LooreColor.textMuted)
                .padding(.vertical, 6)
                .padding(.horizontal, 14)
                .background(isActive ? LooreColor.bgCard : Color.clear, in: Capsule())
                .overlay(Capsule().strokeBorder(isActive ? LooreColor.accent : LooreColor.border, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isActive ? .isSelected : [])
        .accessibilityIdentifier("artifactsNav.\(title)")
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
                .accessibilityIdentifier(identifier)
        }
        .frame(minHeight: minHeight)
        .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
            .strokeBorder(focused ? LooreColor.accent : LooreColor.border, lineWidth: 1))
    }

    private var font: Font {
        monospaced ? .system(size: 13, weight: .regular, design: .monospaced) : LooreFont.sans(13.6, .light)
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
