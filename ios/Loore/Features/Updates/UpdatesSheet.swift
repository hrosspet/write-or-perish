import SwiftUI

/// "While you were away" (web `UpdatesModal`, map E §9): unread notifications,
/// then developer polls, then changelog sections, shown once per launch.
/// "Got it" dismisses an item for good, "Later" keeps it for next time; following
/// a link records a skip. Closes itself when nothing remains.
struct UpdatesSheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var notifications: [UpdateNotification]
    @State private var polls: [PendingPoll]
    @State private var changelog: [ChangelogSection]

    init(payload: UpdatesPayload) {
        _notifications = State(initialValue: payload.notifications)
        _polls = State(initialValue: payload.polls)
        _changelog = State(initialValue: payload.changelog)
    }

    private var remaining: Int { notifications.count + polls.count + changelog.count }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("While you were away")
                    .font(LooreFont.serif(26.4, .regular, relativeTo: .title))
                    .foregroundStyle(LooreColor.textPrimary)
                    .padding(.bottom, 4)
                    .accessibilityAddTraits(.isHeader)
                Text("What's new in Loore since your last visit.")
                    .font(LooreFont.sans(12.5, .light))
                    .foregroundStyle(LooreColor.textMuted)

                ForEach(notifications) { notification in
                    NotificationItemView(notification: notification,
                                         onDone: { notifications.removeAll { $0.id == notification.id } },
                                         onCloseAndOpen: closeAndOpen)
                }
                ForEach(polls) { poll in
                    PollItemView(poll: poll, onDone: { polls.removeAll { $0.id == poll.id } })
                }
                ForEach(changelog) { section in
                    ChangelogItemView(section: section,
                                      onDone: { changelog.removeAll { $0.id == section.id } },
                                      onCloseAndOpen: closeAndOpen)
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 28)
            .padding(.bottom, 32)
            .frame(maxWidth: 560, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(LooreColor.bgCard.ignoresSafeArea())
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
        .presentationBackground(LooreColor.bgCard)
        .onChange(of: remaining) { _, count in
            if count == 0 { dismiss() }
        }
    }

    /// Following an in-app link: close the whole sheet, then navigate (the web
    /// closes the modal first so the navigation is visible).
    private func closeAndOpen(_ link: String) {
        guard let route = AppRoute.parse(link, environment: app.environment) else { return }
        dismiss()
        // After the sheet is gone: a web page is itself a sheet on the same root.
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            app.open(route)
        }
    }
}

// MARK: - Items

private let itemTitleFont = LooreFont.serif(22.4, .regular, relativeTo: .title2)
private let eyebrowTitleFont = LooreFont.serif(20, .regular, relativeTo: .title3)

private struct ItemEyebrow: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.sans(11.5, .light))
            .tracking(0.9)
            .foregroundStyle(LooreColor.textMuted)
    }
}

private struct ItemContainer<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HairlineDivider().padding(.bottom, 20)
            content()
        }
        .padding(.top, 20)
    }
}

private struct ItemButtons<Content: View>: View {
    @ViewBuilder var content: () -> Content

    var body: some View {
        HStack(spacing: 10) {
            Spacer(minLength: 0)
            content()
        }
        .padding(.top, 16)
    }
}

private struct UpdatesButtonStyle: ButtonStyle {
    var accent = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(LooreFont.sans(13.6, accent ? .regular : .light))
            .foregroundStyle(accent ? LooreColor.bgDeep : LooreColor.textSecondary)
            .padding(.vertical, 8)
            .padding(.horizontal, 16)
            .background(accent ? LooreColor.accent : Color.clear, in: RoundedRectangle(cornerRadius: LooreRadius.control))
            .overlay {
                if !accent {
                    RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border)
                }
            }
            .opacity(configuration.isPressed ? 0.75 : (isEnabled ? 1 : 0.5))
    }
}

private struct NotificationItemView: View {
    let notification: UpdateNotification
    let onDone: () -> Void
    let onCloseAndOpen: (String) -> Void
    @Environment(AppState.self) private var app

    static let eyebrows: [String: String] = [
        "fix_ready": "Your issue has been fixed",
        "issue_declined": "Your issue — closed without a fix",
        "x_disconnected": "Action needed",
    ]

    /// "v7 · Aug 9, 2026" for `profile_ready`.
    static func stamp(_ meta: UpdateNotification.Meta?) -> String? {
        guard let meta, let version = meta.version else { return nil }
        let when = LooreDateFormat.date(meta.createdAt)
        return when.isEmpty ? "v\(version)" : "v\(version) · \(when)"
    }

    var body: some View {
        let eyebrow = Self.eyebrows[notification.type]
        ItemContainer {
            if let eyebrow { ItemEyebrow(text: eyebrow) }
            if let stamp = Self.stamp(notification.meta) { ItemEyebrow(text: stamp) }
            Text(notification.title)
                .font(eyebrow == nil ? itemTitleFont : eyebrowTitleFont)
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.top, 6)
            if let body = notification.body, !body.isEmpty {
                Text(body)
                    .font(LooreFont.sans(14.4, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .lineSpacing(4)
                    .padding(.top, 14)
            }
            ItemButtons {
                Button("Later") { mark("skip") }.buttonStyle(UpdatesButtonStyle())
                if let link = notification.link, !link.isEmpty {
                    Button("Take a look") { takeALook(link) }.buttonStyle(UpdatesButtonStyle())
                }
                Button("Got it") { mark("read") }.buttonStyle(UpdatesButtonStyle(accent: true))
            }
        }
    }

    private func mark(_ action: String) {
        app.api.fireAndForget(APIRequest(.post, APIPath.notification(notification.id, action: action)))
        onDone()
    }

    /// Looking is not acknowledging: record a skip. External URLs open over the
    /// sheet and it stays; in-app paths close it and navigate.
    private func takeALook(_ link: String) {
        app.api.fireAndForget(APIRequest(.post, APIPath.notification(notification.id, action: "skip")))
        if link.lowercased().hasPrefix("http://") || link.lowercased().hasPrefix("https://"),
           case .external(let url) = AppRoute.parse(link, environment: app.environment) {
            UIApplication.shared.open(url)
        } else {
            onCloseAndOpen(link)
        }
    }
}

private struct ChangelogItemView: View {
    let section: ChangelogSection
    let onDone: () -> Void
    let onCloseAndOpen: (String) -> Void
    @Environment(AppState.self) private var app

    var body: some View {
        ItemContainer {
            if let date = section.date { ItemEyebrow(text: date) }
            Text(section.title)
                .font(itemTitleFont)
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.top, 6)
            SimpleMarkdownText(markdown: section.body, font: LooreFont.sans(14.4, .light))
                .padding(.top, 14)
                .environment(\.openURL, OpenURLAction { url in
                    followLink(url.absoluteString)
                    return .handled
                })
            ItemButtons {
                Button("Later") { mark("skip") }.buttonStyle(UpdatesButtonStyle())
                Button("Got it") { mark("read") }.buttonStyle(UpdatesButtonStyle(accent: true))
            }
        }
    }

    private func mark(_ action: String) {
        app.api.fireAndForget(APIRequest(.post, APIPath.changelog(section.id, action: action)))
        onDone()
    }

    /// Following a body link is not acknowledging: record a skip, close, navigate.
    private func followLink(_ link: String) {
        if case .external(let url) = AppRoute.parse(link, environment: app.environment) {
            UIApplication.shared.open(url)
            return
        }
        app.api.fireAndForget(APIRequest(.post, APIPath.changelog(section.id, action: "skip")))
        onCloseAndOpen(link)
    }
}

/// A developer poll (two-phase opt-in): asking for an AI draft and sending an
/// answer are separate, explicit steps; nothing leaves until "Send to developer".
private struct PollItemView: View {
    let poll: PendingPoll
    let onDone: () -> Void
    @Environment(AppState.self) private var app
    @State private var response: PollResponse?
    @State private var text: String
    @State private var writing: Bool
    @State private var busy = false
    @State private var error: String?

    static let dataSourceLabels = [
        "derived": "your profile, recent summary and intentions",
        "recent_window": "your recent writing (as much as fits its context window)",
    ]

    init(poll: PendingPoll, onDone: @escaping () -> Void) {
        self.poll = poll
        self.onDone = onDone
        _response = State(initialValue: poll.response)
        _text = State(initialValue: poll.response?.content ?? "")
        _writing = State(initialValue: !(poll.response?.content ?? "").isEmpty)
    }

    private var drafting: Bool { response?.status == "drafting" }
    private var draftModel: String { poll.draftTerms?.model ?? "the AI" }
    private var draftSource: String {
        Self.dataSourceLabels[poll.draftTerms?.dataSource ?? ""] ?? Self.dataSourceLabels["derived"]!
    }

    var body: some View {
        ItemContainer {
            ItemEyebrow(text: "A question from the developer")
            Text(poll.question)
                .font(eyebrowTitleFont)
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.top, 6)
            note("Answering is optional. If you ask for a draft, \(draftModel) will read \(draftSource) to write one for you to edit — and nothing is sent until you press Send.")
                .padding(.top, 14)
            if drafting {
                note("\(draftModel) is drafting an answer in the background — this can take a few minutes. You can close this and come back later.",
                     color: LooreColor.accent)
                    .padding(.top, 12)
            }
            if let error {
                note(error, color: LooreColor.error).padding(.top, 12)
            }
            if writing && !drafting {
                if response?.generatedBy != nil && response?.content == text {
                    note("Drafted by \(draftModel) from \(draftSource) — please review and edit before sending.")
                        .padding(.top, 10)
                }
                TextField("", text: $text, prompt: loorePrompt("Your answer…"), axis: .vertical)
                    .lineLimit(5...12)
                    .font(LooreFont.sans(14.4, .light))
                    .foregroundStyle(LooreColor.textPrimary)
                    .padding(.vertical, 10)
                    .padding(.horizontal, 12)
                    .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: LooreRadius.small))
                    .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
                    .padding(.top, 10)
            }
            ItemButtons {
                Button("No thanks", action: decline).buttonStyle(UpdatesButtonStyle()).disabled(busy)
                Button("Later", action: onDone).buttonStyle(UpdatesButtonStyle()).disabled(busy)
                if !writing && !drafting {
                    Button("Write my own") { writing = true }.buttonStyle(UpdatesButtonStyle()).disabled(busy)
                    Button("Draft with AI", action: requestDraft).buttonStyle(UpdatesButtonStyle(accent: true)).disabled(busy)
                }
                if writing && !drafting {
                    Button("Send to developer", action: send)
                        .buttonStyle(UpdatesButtonStyle(accent: true))
                        .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || busy)
                }
            }
        }
        .task(id: drafting) {
            guard drafting else { return }
            await pollDraft()
        }
    }

    private func note(_ text: String, color: Color = LooreColor.textMuted) -> some View {
        Text(text)
            .font(LooreFont.sans(12.5, .light))
            .foregroundStyle(color)
            .lineSpacing(3)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// While the AI draft generates (a batch job, minutes): poll every 5 s.
    private func pollDraft() async {
        await Poller.run(
            options: .init(interval: 5, maxDuration: nil),
            fetch: { try await app.api.get(APIPath.poll(poll.id), poll: true, as: PendingPoll.self) },
            isTerminal: { ($0.response?.status ?? "drafting") != "drafting" },
            onUpdate: { latest in
                guard let r = latest.response, r.status != "drafting" else { return }
                response = r
                if r.status == "draft" {
                    text = r.content
                    writing = true
                } else if r.status == "draft_failed" {
                    error = "Drafting didn't work this time — you can still write your own answer."
                    writing = true
                }
            }
        )
    }

    private func requestDraft() {
        error = nil
        busy = true
        Task {
            do {
                let envelope: PollResponseEnvelope = try await app.api.post(APIPath.pollDraft(poll.id))
                response = envelope.response
            } catch let apiError as APIError {
                error = apiError.userMessage(fallback: "Couldn't start the draft — you can still write your own answer.")
                writing = true
            } catch {
                self.error = "Couldn't start the draft — you can still write your own answer."
                writing = true
            }
            busy = false
        }
    }

    private func send() {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        error = nil
        busy = true
        Task {
            do {
                let _: PollResponseEnvelope = try await app.api.put(APIPath.pollResponse(poll.id), json: ["content": .string(text)])
                let _: PollResponseEnvelope = try await app.api.post(APIPath.pollSend(poll.id))
                onDone()
            } catch let apiError as APIError {
                error = apiError.userMessage(fallback: "Sending failed — try again?")
            } catch {
                self.error = "Sending failed — try again?"
            }
            busy = false
        }
    }

    private func decline() {
        app.api.fireAndForget(APIRequest(.post, APIPath.pollDecline(poll.id)))
        onDone()
    }
}
