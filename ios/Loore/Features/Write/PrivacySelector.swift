import SwiftUI

/// "PRIVACY LEVEL" and "AI USAGE" selects (web `PrivacySelector`, craft mode).
///
/// With Circles on for the user the privacy part is "Who can read this":
/// Only me / Circles / Everyone (stored as private / circles / public), and
/// Circles opens the audience picker. With Circles off it is the select as
/// before, its Circles option not selectable.
struct PrivacySelectorView: View {
    @Binding var privacy: PrivacyLevel
    @Binding var aiUsage: AIUsage
    var disabled = false
    /// Replying under a public node: privacy is fixed and shown as text.
    var lockPrivacy = false
    /// Circles. `audience`: what the author picks for an entry of their own.
    /// `threadAudience`: the people of the shared thread a reply is written
    /// into (the picker is then a note naming them). `allowCircles` false
    /// leaves the choice out (imports: Only me / Everyone). `lockPublic`: an
    /// existing reply in someone's shared thread, where Everyone is not its
    /// writer's to choose.
    var audience: CircleAudienceState?
    var threadAudience: [String]?
    var allowCircles = true
    var lockPublic = false
    var onEditCircles: (() -> Void)?

    @Environment(AppState.self) private var app

    private var circlesOn: Bool { app.capabilities.circlesEnabled }

    static let whoCanReadLabel = "Who can read this"

    /// The three choices with Circles on (two for imports).
    static func choices(allowCircles: Bool) -> [(PrivacyLevel, String)] {
        var list: [(PrivacyLevel, String)] = [(.private, "Only me")]
        if allowCircles { list.append((.circles, "Circles")) }
        list.append((.public, "Everyone"))
        return list
    }

    static func threadNote(_ names: [String]) -> String {
        let who = CirclesWording.joinNames(names)
        return who.isEmpty ? "The people this thread is shared with."
            : "\(who) will see it, with your name. Nobody else can."
    }

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
        case .circles: return "Circles is coming soon. Until then this note is only visible to you."
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
                if lockPrivacy, let threadAudience {
                    // A reply in a shared thread is read by the thread's people.
                    FieldLabel(text: Self.whoCanReadLabel)
                    FieldDescription(text: Self.threadNote(threadAudience))
                } else if lockPrivacy {
                    FieldLabel(text: circlesOn ? Self.whoCanReadLabel : "Privacy Level")
                    FieldDescription(text: circlesOn ? "Everyone — inherited from the thread."
                                     : "Public — inherited from the thread.")
                } else if circlesOn {
                    whoCanRead
                } else {
                    FieldLabel(text: "Privacy Level")
                    // Circles is not selectable until it is on for the account;
                    // an entry already saved with it keeps showing it.
                    SelectField(options: Self.privacyOptions, selection: $privacy, disabled: disabled,
                                unavailable: [.circles])
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

extension PrivacySelectorView {
    /// "Who can read this": the choices as buttons, then what the choice means.
    @ViewBuilder fileprivate var whoCanRead: some View {
        FieldLabel(text: Self.whoCanReadLabel)
        HStack(spacing: 8) {
            ForEach(Self.choices(allowCircles: allowCircles), id: \.0) { choice in
                let selected = privacy == choice.0
                let off = choice.0 == .public && lockPublic
                Button {
                    privacy = choice.0
                } label: {
                    Text(choice.1)
                        .font(LooreFont.sans(15, selected ? .regular : .light))
                        .foregroundStyle(selected ? LooreColor.textPrimary : LooreColor.textSecondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(selected ? LooreColor.bgCard : LooreColor.bgSurface,
                                    in: RoundedRectangle(cornerRadius: LooreRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: LooreRadius.control)
                            .strokeBorder(selected ? LooreColor.accent : LooreColor.border))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(disabled || off)
                .opacity(off ? 0.4 : 1)
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityIdentifier("privacy.choice.\(choice.0.rawString)")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Self.whoCanReadLabel)
        switch privacy {
        case .circles where allowCircles:
            if let threadAudience {
                FieldDescription(text: "\(CirclesWording.joinNames(threadAudience)) can read this reply. Nobody else can.")
            } else if let audience {
                CircleAudiencePicker(state: audience, disabled: disabled, onEditCircles: onEditCircles)
            }
        case .private:
            FieldDescription(text: "Only you can read this.")
        case .public:
            FieldDescription(text: "Anyone can read this, also people who are not signed in.")
        default:
            EmptyView()
        }
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
    /// Options shown greyed out, not selectable (the web's `<option disabled>`).
    var unavailable: Set<Value> = []

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
                .disabled(unavailable.contains(option.0))
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
                    .accessibilityHidden(true)
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
