import SwiftUI

// The web's writing and deletion dialogs, same copy (map D §4.5–§4.9, §7).
// Present each with `.looreDialog(isPresented:)`.

/// Per-entry character cap (`SplitContentDialog.NODE_CHAR_CAP`).
let nodeCharCap = 100_000

/// "Regenerate audio?" (edit of an entry with generated TTS whose text changed).
struct RegenerateTtsDialog: View {
    let onChoice: (Bool) -> Void
    let onCancel: () -> Void

    var body: some View {
        LooreDialogCard(title: "Regenerate audio?") {
            DialogBodyText(text: "This entry has generated audio that won't match your edits. Keep the existing audio, or regenerate it (it'll be created fresh the next time you play it).")
            VStack(spacing: 8) {
                ChoiceButton(title: "Regenerate audio",
                             subtitle: "Removes the outdated audio; new audio is generated on next play.",
                             tone: .accent) { onChoice(true) }
                ChoiceButton(title: "Keep existing audio", subtitle: "Saves your edits; the current audio stays as-is.",
                             tone: .primary) { onChoice(false) }
                ChoiceButton(title: "Cancel", action: onCancel)
            }
        }
    }
}

/// "Apply to replies too?" (privacy / AI usage changed on a node with replies).
struct ApplyToRepliesDialog: View {
    let privacyChanged: Bool
    let aiUsageChanged: Bool
    let onChoice: (Bool) -> Void
    let onCancel: () -> Void

    private var what: String {
        if privacyChanged && aiUsageChanged { return "privacy and AI usage" }
        return privacyChanged ? "privacy" : "AI usage"
    }

    var body: some View {
        LooreDialogCard(title: "Apply to replies too?") {
            DialogBodyText(text: "You changed the \(what) of a node that has replies.")
            VStack(spacing: 8) {
                ChoiceButton(title: "This node only", subtitle: "Replies keep their current settings.",
                             tone: .primary) { onChoice(false) }
                ChoiceButton(title: "This node and all my replies",
                             subtitle: "Every reply of yours below it, including AI responses you requested. Other users' replies are kept (they own them).",
                             tone: .accent) { onChoice(true) }
                ChoiceButton(title: "Cancel", action: onCancel)
            }
        }
    }
}

/// "Long entry": above the cap, a new entry can be split into connected entries.
struct SplitContentDialog: View {
    let charCount: Int
    let onConfirm: () -> Void
    let onCancel: () -> Void

    static func parts(_ count: Int) -> Int { max(2, Int((Double(count) / Double(nodeCharCap)).rounded(.up))) }

    var body: some View {
        let parts = Self.parts(charCount)
        LooreDialogCard(title: "Long entry") {
            DialogBodyText(text: "This text is \(jsLocaleNumber(charCount)) characters — above the \(jsLocaleNumber(nodeCharCap))-character limit for a single entry. It can be saved as ~\(parts) connected entries: they read as one continuous text and stay together in the thread.")
            VStack(spacing: 8) {
                ChoiceButton(title: "Split into ~\(parts) connected entries",
                             subtitle: "Split happens on line breaks — no line is cut in half.",
                             tone: .accent, action: onConfirm)
                ChoiceButton(title: "Cancel", action: onCancel)
            }
        }
    }
}

/// "This reply will be public" (a reply under a public node), with "Don't show this again".
struct PublicReplyDialog: View {
    let onConfirm: () -> Void
    let onCancel: () -> Void
    @State private var dontShowAgain = false

    var body: some View {
        LooreDialogCard(title: "This reply will be public") {
            DialogBodyText(text: "You're responding in a public thread, so your reply will be visible to everyone — including people who aren't signed in. To think about this privately instead, quote it into one of your own threads.")
            Button {
                dontShowAgain.toggle()
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: dontShowAgain ? "checkmark.square" : "square")
                        .font(.system(size: 15, weight: .light))
                        .accessibilityHidden(true)
                    Text("Don't show this again")
                }
                .accessibilityAddTraits(.isToggle)
                .accessibilityValue(dontShowAgain ? "On" : "Off")
                .font(LooreFont.sans(13.1, .light))
                .foregroundStyle(LooreColor.textMuted)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(dontShowAgain ? "Checked" : "Not checked")
            HStack(spacing: 10) {
                Spacer()
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button("Publish reply") {
                    if dontShowAgain { UserDefaults.standard.set("true", forKey: DefaultsKey.publicReplyAck) }
                    onConfirm()
                }
                .buttonStyle(.looreFilled)
            }
        }
    }
}

/// The delete dialog's variants (web `DeleteConfirmDialog`).
struct DeleteConfirmDialog: View {
    enum Mode: Equatable {
        case single(hasChildren: Bool)
        case thread
        /// A saved reference (References list and page).
        case reference
        /// The follow-up when the delete would leave only the system prompt.
        case prompt(listedIn: String)
    }

    let mode: Mode
    /// withDescendants (single/thread) or includePrompt (prompt).
    let onConfirm: (Bool) -> Void
    let onCancel: () -> Void

    var body: some View {
        switch mode {
        case .prompt(let listedIn):
            LooreDialogCard(title: "Delete the system prompt too?") {
                DialogBodyText(text: "After this, nothing is left in the session but its system prompt, and \(listedIn) would list the session by the prompt's text.")
                VStack(spacing: 8) {
                    ChoiceButton(title: "Also delete the system prompt", subtitle: "The whole session leaves \(listedIn).",
                                 tone: .accent) { onConfirm(true) }
                    ChoiceButton(title: "Keep the system prompt", subtitle: "You can continue the session from it.",
                                 tone: .primary) { onConfirm(false) }
                    ChoiceButton(title: "Cancel", action: onCancel)
                }
            }
        case .reference:
            LooreDialogCard(title: "Delete reference?") {
                DialogBodyText(text: "It leaves your saved references and search. The original stays where it is.")
                VStack(spacing: 8) {
                    ChoiceButton(title: "Delete", tone: .accent) { onConfirm(false) }
                        .accessibilityIdentifier("reference.confirmDelete")
                    ChoiceButton(title: "Cancel", action: onCancel)
                }
            }
        case .thread:
            LooreDialogCard(title: "Delete entire thread?") {
                DialogBodyText(text: "Your content in this thread will be removed. Your nodes remain as placeholders so other users' replies stay reachable (they own them).")
                VStack(spacing: 8) {
                    ChoiceButton(title: "Delete thread", tone: .accent) { onConfirm(true) }
                    ChoiceButton(title: "Cancel", action: onCancel)
                }
            }
        case .single(let hasChildren) where hasChildren:
            LooreDialogCard(title: "Delete node?") {
                DialogBodyText(text: "This node has replies. Choose how to handle them:")
                VStack(spacing: 8) {
                    ChoiceButton(title: "Delete this node only", subtitle: "Replies stay; this node becomes a placeholder.",
                                 tone: .primary) { onConfirm(false) }
                    ChoiceButton(title: "Delete this node and all my replies",
                                 subtitle: "Other users' replies are kept (they own them).", tone: .accent) { onConfirm(true) }
                    ChoiceButton(title: "Cancel", action: onCancel)
                }
            }
        case .single:
            LooreDialogCard(title: "Delete node?") {
                DialogBodyText(text: "Content and edit history will be removed.")
                VStack(spacing: 8) {
                    ChoiceButton(title: "Delete", tone: .accent) { onConfirm(false) }
                    ChoiceButton(title: "Cancel", action: onCancel)
                }
            }
        }
    }
}

/// "Rename thread" (Log cards): 120 characters; empty clears the name.
struct RenameThreadDialog: View {
    let currentName: String
    let fallbackTitle: String
    let saving: Bool
    let onSave: (String) -> Void
    let onCancel: () -> Void
    @State private var value = ""
    @FocusState private var focused: Bool

    private var unchanged: Bool { value.jsTrimmed == currentName.jsTrimmed }

    var body: some View {
        LooreDialogCard(title: "Rename thread") {
            TextField("", text: $value, prompt: loorePrompt(fallbackTitle.isEmpty ? "Thread name" : fallbackTitle))
                .textFieldStyle(LooreTextFieldStyle(isFocused: focused))
                .focused($focused)
                .disabled(saving)
                .submitLabel(.done)
                .onSubmit(save)
                .onChange(of: value) { _, new in
                    if new.jsLength > 120 { value = new.jsPrefix(120) }
                }
                .accessibilityLabel("Thread name")
            Text("Shown on this thread's Log card. Leave it empty to show the entry's own title.")
                .font(LooreFont.sans(13.1, .light))
                .foregroundStyle(LooreColor.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Spacer()
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button(saving ? "Saving…" : "Save", action: save)
                    .buttonStyle(.loorePrimary)
                    .disabled(saving || unchanged)
            }
        }
        .onAppear {
            value = currentName
            focused = true
        }
    }

    private func save() {
        guard !saving, !unchanged else { return }
        onSave(value.jsTrimmed)
    }
}

/// "Edit prompt for this thread only?" (editing a prompt-rooted system node).
struct PromptEditConfirmDialog: View {
    let onConfirm: () -> Void
    let onCancel: () -> Void
    let onOpenPrompts: () -> Void

    var body: some View {
        LooreDialogCard(title: "Edit prompt for this thread only?") {
            DialogBodyText(text: "This changes the system prompt in this conversation only. Future threads won't be affected, and updates to your prompt template won't reach this thread anymore.")
            HStack(spacing: 4) {
                Text("To edit the template for all future threads, go to")
                Button("Prompts", action: onOpenPrompts)
                    .buttonStyle(.plain)
                    .foregroundStyle(LooreColor.accent)
                    .underline()
                Text(".")
            }
            .font(LooreFont.sans(13.1, .light))
            .foregroundStyle(LooreColor.textMuted)
            HStack(spacing: 12) {
                Spacer()
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button("Edit for this thread", action: onConfirm).buttonStyle(.loorePrimary)
                Spacer()
            }
        }
    }
}
