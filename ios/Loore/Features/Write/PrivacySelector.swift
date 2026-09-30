import SwiftUI

/// "PRIVACY LEVEL" and "AI USAGE" selects (web `PrivacySelector`, craft mode).
struct PrivacySelectorView: View {
    @Binding var privacy: PrivacyLevel
    @Binding var aiUsage: AIUsage
    var disabled = false
    /// Replying under a public node: privacy is fixed and shown as text.
    var lockPrivacy = false

    static let privacyOptions: [(PrivacyLevel, String)] = [
        (.private, "🔒 Private · Only I can see this"),
        (.circles, "👥 Circles · Shared with specific groups (coming soon)"),
        (.public, "🌐 Public · Anyone can see this"),
    ]
    static let aiOptions: [(AIUsage, String)] = [
        (.off, "🚫 None · No AI access"),
        (.chat, "💬 Chat · AI can use for responses"),
        (.train, "🧠 Train · AI can use for training"),
    ]

    static func privacyDescription(_ p: PrivacyLevel) -> String {
        switch p {
        case .private: return "This note is private and only visible to you."
        case .circles: return "This note will be shared with your selected circles (feature coming soon)."
        case .public: return "This note will be visible to all users."
        case .unknown: return ""
        }
    }

    static func aiDescription(_ a: AIUsage) -> String {
        switch a {
        case .off: return "AI will not access this note."
        case .chat: return "AI can read this note to generate responses, but won't use it for training."
        case .train: return "AI can use this note for training data to improve the model."
        case .unknown: return ""
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                FieldLabel(text: "Privacy Level")
                if lockPrivacy {
                    FieldDescription(text: "Public — inherited from the thread.")
                } else {
                    SelectField(options: Self.privacyOptions, selection: $privacy, disabled: disabled)
                    FieldDescription(text: Self.privacyDescription(privacy))
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                FieldLabel(text: "AI Usage")
                SelectField(options: Self.aiOptions, selection: $aiUsage, disabled: disabled)
                FieldDescription(text: Self.aiDescription(aiUsage))
            }
        }
        .padding(.vertical, 12)
    }
}

/// 0.7rem uppercase label with 0.14em tracking.
struct FieldLabel: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.sans(11.2, .regular))
            .tracking(1.6)
            .foregroundStyle(LooreColor.textMuted)
    }
}

struct FieldDescription: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.sans(12.8, .light))
            .foregroundStyle(LooreColor.textMuted)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// A web-style select: a full-width field that opens a menu.
struct SelectField<Value: Hashable>: View {
    let options: [(Value, String)]
    @Binding var selection: Value
    var disabled = false

    var body: some View {
        Menu {
            ForEach(options, id: \.0) { option in
                Button {
                    selection = option.0
                } label: {
                    if option.0 == selection {
                        Label(option.1, systemImage: "checkmark")
                    } else {
                        Text(option.1)
                    }
                }
            }
        } label: {
            HStack {
                Text(options.first(where: { $0.0 == selection })?.1 ?? "")
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 8)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 11, weight: .regular))
                    .foregroundStyle(LooreColor.textMuted)
            }
            .font(LooreFont.sans(14.4, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .padding(.vertical, 10)
            .padding(.horizontal, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(disabled ? LooreColor.bgSurface : LooreColor.bgInput, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
            .contentShape(Rectangle())
        }
        .disabled(disabled)
    }
}

/// "AGENTIC REPLY" / "AUTO-GENERATE" pill toggles of the Write New Entry form.
struct LabeledPillToggle: View {
    let label: String
    @Binding var isOn: Bool
    let onText: String
    let offText: String
    var disabled = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                isOn.toggle()
            } label: {
                HStack(spacing: 12) {
                    FieldLabel(text: label)
                    LoorePillSwitch(isOn: isOn)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(disabled)
            .accessibilityLabel(label)
            .accessibilityValue(isOn ? "On" : "Off")
            FieldDescription(text: isOn ? onText : offText)
        }
        .padding(.vertical, 12)
    }
}
