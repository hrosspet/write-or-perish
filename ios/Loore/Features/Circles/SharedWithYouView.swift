import SwiftUI
import Observation

@MainActor
@Observable
final class SharedWithYouModel {
    @ObservationIgnored private let api: APIClient
    private(set) var people: [SharedWithMe.Person]?
    private(set) var hidden: [String] = []
    private(set) var nextBefore: Int?
    private(set) var error: String?
    private(set) var busy = false

    static let failed = "That did not work. Try again."

    init(api: APIClient) {
        self.api = api
    }

    func load(before: Int? = nil) async {
        do {
            let query = before.map { [URLQueryItem(name: "before", value: String($0))] } ?? []
            let answer: SharedWithMe = try await api.get(APIPath.circlesSharedWithMe, query: query)
            hidden = answer.hidden
            nextBefore = answer.nextBefore
            if before != nil, let current = people {
                people = SharedWithMe.merge(current, older: answer.people)
            } else {
                people = answer.people
            }
            error = nil
        } catch {
            self.error = "This page could not be loaded. Pull down to try again."
        }
    }

    /// "Don't show <name>'s entries": the notes and the entries stop; the
    /// person is not told.
    func hide(_ username: String) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await api.post(APIPath.circlesMutes, json: .object(["username": .string(username)]),
                                   as: EmptyResponse.self)
            await load()
        } catch {
            self.error = (error as? APIError)?.userMessage(fallback: Self.failed) ?? Self.failed
        }
    }

    func show(_ username: String) async {
        do {
            _ = try await api.delete(APIPath.circlesMute(username: username), as: EmptyResponse.self)
            await load()
        } catch {
            self.error = (error as? APIError)?.userMessage(fallback: Self.failed) ?? Self.failed
        }
    }
}

/// "Shared with you" (web `SharedWithYouPage`): the entries people chose to
/// show the user, grouped by person, newest first. Nothing here counts or
/// tracks reading: no unread marks, no numbers. At the bottom, quietly, the
/// people whose entries the user chose not to see, with the way back.
struct SharedWithYouView: View {
    @Environment(AppState.self) private var app
    @State private var model: SharedWithYouModel?
    @State private var hiding: String?

    static let intro = "Entries that people chose to show you. They stay here; nothing is added to your own log or your profile."
    static let nothing = "Nothing is shared with you right now."
    static let nobodySees = "Nobody sees whether you have read an entry."
    static let hiddenNote = "You get no notes and see no entries from them. They are not told."

    static func hideTitle(_ name: String) -> String { "Stop showing \(CirclesWording.possessive(name)) entries?" }
    static func hideBody(_ name: String) -> String {
        "You will get no notes about \(CirclesWording.possessive(name)) entries and will not see them here. \(CirclesWording.at(name)) is not told what you choose. You can undo this at the bottom of this page."
    }
    static func hideConfirm(_ name: String) -> String { "Don't show \(CirclesWording.possessive(name)) entries" }

    var body: some View {
        Group {
            if !app.capabilities.circlesEnabled {
                Text("Not available.")
                    .font(LooreFont.sans(14.4, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let model {
                page(model)
            } else {
                LoadingLine()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .loorePageBackground()
        .navigationTitle("Shared with you")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard app.capabilities.circlesEnabled else { return }
            if model == nil { model = SharedWithYouModel(api: app.api) }
            await model?.load()
        }
        .looreDialog(isPresented: Binding(get: { hiding != nil }, set: { if !$0 { hiding = nil } })) {
            if let name = hiding {
                CircleConfirmDialog(title: Self.hideTitle(name), message: Self.hideBody(name),
                                    confirmLabel: Self.hideConfirm(name), busy: model?.busy ?? false,
                                    onConfirm: {
                                        Task {
                                            await model?.hide(name)
                                            hiding = nil
                                        }
                                    },
                                    onCancel: { hiding = nil })
            }
        }
    }

    private func page(_ model: SharedWithYouModel) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 30) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Shared with you")
                        .font(LooreFont.serif(36, .medium, relativeTo: .largeTitle))
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                    Text(Self.intro)
                        .font(LooreFont.sans(15, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Your circles") { app.open(.circles) }
                        .buttonStyle(.plain)
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.accent)
                        .frame(minHeight: 32)
                }
                if let error = model.error {
                    Text(error).font(LooreFont.sans(14, .light)).foregroundStyle(LooreColor.error)
                }
                if model.people == nil && model.error == nil {
                    Text("Loading…").font(LooreFont.sans(14, .light)).foregroundStyle(LooreColor.textSecondary)
                }
                if let people = model.people, people.isEmpty {
                    Text(Self.nothing)
                        .font(LooreFont.sans(15, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                }
                ForEach(model.people ?? []) { person in
                    section(person)
                }
                if let before = model.nextBefore {
                    Button("Show older entries") { Task { await model.load(before: before) } }
                        .buttonStyle(.looreOutline)
                        .accessibilityIdentifier("shared.older")
                }
                VStack(alignment: .leading, spacing: 18) {
                    Text(Self.nobodySees)
                        .font(LooreFont.serif(18, .regular, italic: true, relativeTo: .body))
                        .foregroundStyle(LooreColor.textSecondary)
                    if !model.hidden.isEmpty { hiddenList(model) }
                }
                .padding(.top, 12)
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(760)
        }
        .refreshable { await model.load() }
    }

    private func section(_ person: SharedWithMe.Person) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 0) {
                AdaptiveStack(spacing: 12, alignment: .firstTextBaseline) {
                    Text(CirclesWording.at(person.username))
                        .font(LooreFont.sans(15, .regular))
                        .foregroundStyle(LooreColor.accent)
                        .accessibilityAddTraits(.isHeader)
                    Spacer(minLength: 8)
                    Button {
                        hiding = person.username
                    } label: {
                        Text("Stop showing \(CirclesWording.possessive(person.username)) entries")
                            .font(LooreFont.sans(13, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("shared.hide.\(person.username)")
                }
                HairlineDivider()
            }
            ForEach(person.entries) { entry in
                Button {
                    NodePrefetch.shared.openThread(entry.id, app: app)
                } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(entry.title.isEmpty ? "Untitled entry" : entry.title)
                            .font(LooreFont.serif(24, .medium, relativeTo: .title2))
                            .foregroundStyle(LooreColor.textPrimary)
                            .multilineTextAlignment(.leading)
                        if !entry.preview.isEmpty {
                            Text(entry.preview)
                                .font(LooreFont.sans(15, .light))
                                .foregroundStyle(LooreColor.textSecondary)
                                .lineSpacing(4)
                                .multilineTextAlignment(.leading)
                        }
                        Text(CirclesWording.entryMeta(entry))
                            .font(LooreFont.sans(13, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("shared.entry.\(entry.id)")
            }
        }
    }

    private func hiddenList(_ model: SharedWithYouModel) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HairlineDivider().padding(.bottom, 10)
            Text("People you have hidden".uppercased())
                .font(LooreFont.sans(11.2, .regular))
                .tracking(1.6)
                .foregroundStyle(LooreColor.textSecondary)
                .accessibilityAddTraits(.isHeader)
            Text(Self.hiddenNote)
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(model.hidden, id: \.self) { name in
                AdaptiveStack(spacing: 12) {
                    Text(CirclesWording.at(name)).font(LooreFont.sans(15, .light)).foregroundStyle(LooreColor.textPrimary)
                    Spacer(minLength: 8)
                    Button {
                        Task { await model.show(name) }
                    } label: {
                        Text("Show \(CirclesWording.possessive(name)) entries again")
                            .font(LooreFont.sans(13, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("shared.show.\(name)")
                }
            }
        }
    }
}
