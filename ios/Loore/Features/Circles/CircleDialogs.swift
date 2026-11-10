import SwiftUI

// The dialogs of Circles, in the shape of the app's other dialogs (web
// `CircleDialogs.js`, same copy). Present each with `.looreDialog(isPresented:)`.

/// A dialog's main line: sans 300, 1rem, primary.
private struct CircleDialogBody: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.sans(16, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .lineSpacing(5)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct CircleDialogNote: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.sans(14, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Before a reply is sent into a shared thread: "Send this reply? <names> will
/// see it, with your name." With `askAI` it stands before "Ask my AI", whose
/// answer is a reply in the thread too. "Don't ask again in this thread" is
/// remembered by the caller (per thread, per user, on this device).
struct CircleReplyDialog: View {
    let names: [String]
    var askAI = false
    /// `dontAskAgain`
    let onConfirm: (Bool) -> Void
    let onCancel: () -> Void
    @State private var dontAskAgain = false

    static func title(askAI: Bool) -> String { askAI ? "Ask your AI here?" : "Send this reply?" }

    static func body(names: [String], askAI: Bool) -> String {
        let who = CirclesWording.joinNames(names)
        return askAI
            ? "Your AI reads this thread and answers in it. \(who) will see the answer, with your name."
            : "\(who) will see it, with your name."
    }

    static func confirmLabel(askAI: Bool) -> String { askAI ? "Ask my AI" : "Send reply" }

    var body: some View {
        LooreDialogCard(title: Self.title(askAI: askAI)) {
            CircleDialogBody(text: Self.body(names: names, askAI: askAI))
            Button {
                dontAskAgain.toggle()
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: dontAskAgain ? "checkmark.square" : "square")
                        .font(.system(size: 20, weight: .light))
                        .foregroundStyle(dontAskAgain ? LooreColor.accent : LooreColor.textSecondary)
                        .accessibilityHidden(true)
                    Text("Don't ask again in this thread")
                }
                .font(LooreFont.sans(14, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(.isToggle)
            .accessibilityValue(dontAskAgain ? "Checked" : "Not checked")
            .accessibilityIdentifier("circles.dontAskAgain")
            HStack(spacing: 10) {
                Spacer()
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button(Self.confirmLabel(askAI: askAI)) { onConfirm(dontAskAgain) }
                    .buttonStyle(.looreFilled)
                    .accessibilityIdentifier("circles.replyConfirm")
            }
        }
    }
}

/// Adding a person to a circle: Cancel, "Only new entries", "Add <name>" (= also
/// the entries already shared with the circle; the real count is stated).
struct AddCircleMemberDialog: View {
    let name: String
    let circleName: String
    let sharedCount: Int
    var busy = false
    /// `includeExisting`
    let onChoice: (Bool) -> Void
    let onCancel: () -> Void

    var body: some View {
        LooreDialogCard(title: "Add \(name) to \(circleName)?") {
            CircleDialogBody(text: CirclesWording.addMemberBody(name: name, circle: circleName, sharedCount: sharedCount))
            CircleDialogNote(text: CirclesWording.addMemberNote(name: name))
            FlowLayout(spacing: 10, lineSpacing: 10) {
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button("Only new entries") { onChoice(false) }
                    .buttonStyle(.looreOutline)
                    .accessibilityIdentifier("circles.onlyNew")
                Button("Add \(name)") { onChoice(true) }
                    .buttonStyle(.looreFilled)
                    .accessibilityIdentifier("circles.addConfirm")
            }
            .disabled(busy)
        }
    }
}

/// A plain yes/no (delete a circle, remove or hide a person).
struct CircleConfirmDialog: View {
    let title: String
    let message: String
    let confirmLabel: String
    var busy = false
    let onConfirm: () -> Void
    let onCancel: () -> Void

    var body: some View {
        LooreDialogCard(title: title) {
            CircleDialogBody(text: message)
            FlowLayout(spacing: 10, lineSpacing: 10) {
                Button("Cancel", action: onCancel).buttonStyle(.looreOutline)
                Button(confirmLabel, action: onConfirm)
                    .buttonStyle(.looreFilled)
                    .accessibilityIdentifier("circles.confirm")
            }
            .disabled(busy)
        }
    }
}

// MARK: - Marks

/// Two overlapping rings, stroke only: the mark of a shared entry.
struct CirclesIcon: View {
    var size: CGFloat = 18
    var color: Color = LooreColor.accent

    var body: some View {
        Canvas { context, canvas in
            let scale = canvas.width / 20
            let radius = 5 * scale
            for cx in [7.5, 12.5] {
                let rect = CGRect(x: cx * scale - radius, y: 10 * scale - radius, width: radius * 2, height: radius * 2)
                context.stroke(Path(ellipseIn: rect), with: .color(color), lineWidth: 1.4 * scale)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// A ring around a dot: the banner's mark.
struct SharedMark: View {
    var size: CGFloat = 20
    var color: Color = LooreColor.accent

    var body: some View {
        Canvas { context, canvas in
            let scale = canvas.width / 20
            for radius in [7.5, 2.5] {
                let r = radius * scale
                let rect = CGRect(x: 10 * scale - r, y: 10 * scale - r, width: r * 2, height: r * 2)
                context.stroke(Path(ellipseIn: rect), with: .color(color), lineWidth: 1.4 * scale)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// The circles icon after a shared entry's author and time. A tap lists
/// everyone who can read the entry and its replies: people by name, never the
/// circle's name.
struct CircleReadersButton: View {
    let audience: NodeAudience
    let me: String?
    @State private var showing = false

    var body: some View {
        Button {
            showing.toggle()
        } label: {
            CirclesIcon()
                .frame(minWidth: 32, minHeight: 28)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Who can read this")
        .accessibilityIdentifier("thread.readers")
        .popover(isPresented: $showing, attachmentAnchor: .rect(.bounds), arrowEdge: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text(CirclesWording.readersListTitle)
                    .font(LooreFont.sans(12, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                ForEach(CirclesWording.readerLines(audience, me: me), id: \.self) { line in
                    Text(line)
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                }
            }
            .padding(.vertical, 12)
            .padding(.horizontal, 14)
            .frame(minWidth: 230, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            .presentationCompactAdaptation(.popover)
            .presentationBackground(LooreColor.bgCardHover)
            .accessibilityIdentifier("thread.readersList")
        }
    }
}

/// The banner over a shared entry, for a reader.
struct SharedEntryBanner: View {
    let audience: NodeAudience

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            SharedMark().padding(.top, 2)
            Text(CirclesWording.sharedBannerText(audience))
                .font(LooreFont.sans(14, .light))
                .foregroundStyle(LooreColor.textPrimary)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.vertical, 14)
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.borderHover))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("thread.sharedBanner")
    }
}
