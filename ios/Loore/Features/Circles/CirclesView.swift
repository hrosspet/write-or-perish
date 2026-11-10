import SwiftUI
import Observation

/// "Your circles" state: the user's lists of people (web `CirclesPage`).
@MainActor
@Observable
final class CirclesModel {
    @ObservationIgnored private let api: APIClient
    private(set) var circles: [PeopleCircle]?
    private(set) var error: String?
    var openId: Int?
    private(set) var busy = false

    static let failed = "That did not work. Try again."

    init(api: APIClient) {
        self.api = api
    }

    private func message(_ error: Error) -> String {
        (error as? APIError)?.userMessage(fallback: Self.failed) ?? Self.failed
    }

    func load() async {
        do {
            let answer: CirclesResponse = try await api.get(APIPath.circles)
            circles = answer.circles
            error = nil
            if openId == nil || !answer.circles.contains(where: { $0.id == openId }) {
                openId = answer.circles.first?.id
            }
        } catch {
            self.error = "Your circles could not be loaded. Pull down to try again."
        }
    }

    private func replace(_ circle: PeopleCircle) {
        guard let index = circles?.firstIndex(where: { $0.id == circle.id }) else { return }
        circles?[index] = circle
    }

    /// Each returns nil on success, else the message to show.
    func create(name: String) async -> String? {
        do {
            let answer: CircleEnvelope = try await api.post(APIPath.circles, json: .object(["name": .string(name)]))
            circles = (circles ?? []) + [answer.circle]
            openId = answer.circle.id
            return nil
        } catch { return message(error) }
    }

    func rename(_ circle: PeopleCircle, to name: String) async -> String? {
        do {
            let answer: CircleEnvelope = try await api.put(APIPath.circle(circle.id), json: .object(["name": .string(name)]))
            replace(answer.circle)
            return nil
        } catch { return message(error) }
    }

    func delete(_ circle: PeopleCircle) async -> String? {
        busy = true
        defer { busy = false }
        do {
            _ = try await api.delete(APIPath.circle(circle.id), as: EmptyResponse.self)
            circles?.removeAll { $0.id == circle.id }
            if openId == circle.id { openId = nil }
            return nil
        } catch { return message(error) }
    }

    /// Looks the typed name up before adding: the dialog then states the count.
    func previewMember(_ circle: PeopleCircle, username: String) async -> Result<CircleMemberPreview, CircleAudienceState.PreviewFailure> {
        do {
            let preview: CircleMemberPreview = try await api.post(
                APIPath.circleMemberPreview(circle.id), json: .object(["username": .string(username)]))
            if preview.alreadyMember {
                return .failure(.init(message: "\(preview.username) is already in \(circle.name)."))
            }
            return .success(preview)
        } catch { return .failure(.init(message: message(error))) }
    }

    func addMember(_ circle: PeopleCircle, username: String, includeExisting: Bool) async -> String? {
        busy = true
        defer { busy = false }
        do {
            let answer: CircleEnvelope = try await api.post(APIPath.circleMembers(circle.id), json: .object([
                "username": .string(username), "include_existing": .bool(includeExisting),
            ]))
            replace(answer.circle)
            return nil
        } catch { return message(error) }
    }

    func removeMember(_ circle: PeopleCircle, username: String) async -> String? {
        busy = true
        defer { busy = false }
        do {
            let answer: CircleEnvelope = try await api.delete(APIPath.circleMember(circle.id, username: username))
            replace(answer.circle)
            return nil
        } catch { return message(error) }
    }
}

/// "Your circles" (web `CirclesPage`): create, rename and delete a circle; add
/// a person by their exact Loore name (no search, no list of users); remove a
/// person. Linked from Account and from "Edit circles" in the picker.
struct CirclesView: View {
    @Environment(AppState.self) private var app
    @State private var model: CirclesModel?
    @State private var creating = false
    @State private var newName = ""
    @State private var createError: String?

    static let intro = "A circle is a list of people you choose. Only you see your circles and who is in them. The people in a circle see the entries you share with it, with your name."
    static let empty = "You have no circles yet. Make one, add people by their Loore name, and choose it under \"Who can read this\" when you write."

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
        .navigationTitle("Your circles")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard app.capabilities.circlesEnabled, model == nil else { return }
            let m = CirclesModel(api: app.api)
            model = m
            await m.load()
        }
    }

    private func page(_ model: CirclesModel) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Your circles")
                        .font(LooreFont.serif(36, .medium, relativeTo: .largeTitle))
                        .foregroundStyle(LooreColor.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                    Text(Self.intro)
                        .font(LooreFont.sans(15, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Shared with you") { app.open(.sharedWithYou) }
                        .buttonStyle(.plain)
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.accent)
                        .frame(minHeight: 32)
                }
                if let error = model.error {
                    Text(error).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.error)
                }
                if model.circles == nil && model.error == nil {
                    Text("Loading…").font(LooreFont.sans(12.8, .light)).foregroundStyle(LooreColor.textSecondary)
                }
                if let circles = model.circles, circles.isEmpty {
                    Text(Self.empty)
                        .font(LooreFont.sans(15, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .lineSpacing(4)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(model.circles ?? []) { circle in
                    CircleCard(model: model, circle: circle, open: model.openId == circle.id)
                }
                if model.circles != nil {
                    if creating {
                        createForm(model)
                    } else {
                        Button("New circle") { creating = true }
                            .buttonStyle(.looreOutline)
                            .accessibilityIdentifier("circles.new")
                    }
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 24)
            .padding(.bottom, 48)
            .looreReadableWidth(760)
        }
        .scrollDismissesKeyboard(.interactively)
        .refreshable { await model.load() }
    }

    private func createForm(_ model: CirclesModel) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            CircleEyebrow(text: "Name of the new circle")
            TextField("", text: $newName, prompt: loorePrompt("Close friends"))
                .circleField()
                .submitLabel(.done)
                .accessibilityLabel("Name of the new circle")
                .accessibilityIdentifier("circles.newName")
            FlowLayout(spacing: 10, lineSpacing: 10) {
                Button("Make circle") {
                    Task {
                        createError = await model.create(name: newName)
                        if createError == nil {
                            newName = ""
                            creating = false
                        }
                    }
                }
                .buttonStyle(.looreFilled)
                .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty)
                .accessibilityIdentifier("circles.make")
                Button("Cancel") {
                    creating = false
                    createError = nil
                }
                .buttonStyle(.looreOutline)
            }
            if let createError {
                Text(createError).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.error)
            }
        }
        .circleCard()
    }
}

/// 0.7rem uppercase label, secondary (the web page's eyebrow).
private struct CircleEyebrow: View {
    let text: String

    var body: some View {
        Text(text.uppercased())
            .font(LooreFont.sans(11.2, .regular))
            .tracking(1.6)
            .foregroundStyle(LooreColor.textSecondary)
    }
}

private extension View {
    func circleCard(padding: CGFloat = 22) -> some View {
        self.padding(.vertical, padding)
            .padding(.horizontal, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: LooreRadius.large))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
    }

    func circleField() -> some View {
        self.textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .font(LooreFont.sans(16, .light))
            .foregroundStyle(LooreColor.textPrimary)
            .padding(.horizontal, 14)
            .frame(minHeight: 46)
            .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.borderHover))
    }
}

/// A quiet text action ("Remove", "Rename or delete", "Open"), 44pt tall.
private struct CircleQuietButton: View {
    let title: String
    var color: Color = LooreColor.textSecondary
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(LooreFont.sans(14, .light))
                .foregroundStyle(color)
                .frame(minHeight: 44)
                .padding(.horizontal, 6)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

private struct CircleCard: View {
    let model: CirclesModel
    let circle: PeopleCircle
    let open: Bool

    @State private var name = ""
    @State private var adding = false
    @State private var addError: String?
    @State private var editing = false
    @State private var newName = ""
    @State private var editError: String?
    @State private var dialog: Dialog?

    enum Dialog: Equatable {
        case add(CircleMemberPreview)
        case remove(String)
        case delete
    }

    static let nobody = "Nobody yet. Add a person to share with this circle."
    static let lookupHint = "Loore finds a person only by their exact name; there is no list of users to browse."

    var body: some View {
        Group {
            if open { opened } else { closed }
        }
        .looreDialog(isPresented: Binding(get: { dialog != nil }, set: { if !$0 { dialog = nil } })) {
            switch dialog {
            case .add(let preview)?:
                AddCircleMemberDialog(name: preview.username, circleName: circle.name, sharedCount: preview.sharedCount,
                                      busy: model.busy,
                                      onChoice: { include in
                                          Task {
                                              let failure = await model.addMember(circle, username: preview.username,
                                                                                  includeExisting: include)
                                              dialog = nil
                                              if let failure { addError = failure } else { name = "" }
                                          }
                                      },
                                      onCancel: { dialog = nil })
            case .remove(let username)?:
                CircleConfirmDialog(
                    title: "Remove \(username) from \(circle.name)?",
                    message: CirclesWording.removeMemberBody(name: username, circleName: circle.name),
                    confirmLabel: "Remove \(username)", busy: model.busy,
                    onConfirm: {
                        Task {
                            addError = await model.removeMember(circle, username: username)
                            dialog = nil
                        }
                    },
                    onCancel: { dialog = nil })
            case .delete?:
                CircleConfirmDialog(
                    title: "Delete \(circle.name)?",
                    message: CirclesWording.deleteCircleBody(name: circle.name, sharedCount: circle.sharedCount),
                    confirmLabel: "Delete circle", busy: model.busy,
                    onConfirm: {
                        Task {
                            editError = await model.delete(circle)
                            dialog = nil
                        }
                    },
                    onCancel: { dialog = nil })
            case nil:
                EmptyView()
            }
        }
    }

    private var closed: some View {
        HStack(alignment: .center, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(circle.name)
                    .font(LooreFont.serif(24, .medium, relativeTo: .title2))
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Text(circle.members.isEmpty ? Self.nobody : circle.members.map(\.username).joined(separator: ", "))
                    .font(LooreFont.sans(14, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            CircleQuietButton(title: "Open", color: LooreColor.accent) { model.openId = circle.id }
                .accessibilityIdentifier("circles.open.\(circle.id)")
        }
        .circleCard(padding: 18)
    }

    private var opened: some View {
        VStack(alignment: .leading, spacing: 16) {
            AdaptiveStack(spacing: 16, alignment: .firstTextBaseline) {
                Text(circle.name)
                    .font(LooreFont.serif(26, .medium, relativeTo: .title2))
                    .foregroundStyle(LooreColor.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                CircleQuietButton(title: "Rename or delete") {
                    editing.toggle()
                    newName = circle.name
                    editError = nil
                }
                .accessibilityIdentifier("circles.renameOrDelete")
            }
            if editing { renameForm }
            members
            addForm
        }
        .circleCard()
    }

    private var renameForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            CircleEyebrow(text: "Name")
            TextField("", text: $newName)
                .circleField()
                .accessibilityLabel("Name")
            FlowLayout(spacing: 10, lineSpacing: 10) {
                Button("Save name") {
                    Task {
                        editError = await model.rename(circle, to: newName)
                        if editError == nil { editing = false }
                    }
                }
                .buttonStyle(.loorePrimary)
                Button("Delete circle") { dialog = .delete }
                    .buttonStyle(.looreOutline)
                    .accessibilityIdentifier("circles.delete")
            }
            if let editError {
                Text(editError).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.error)
            }
        }
    }

    private var members: some View {
        VStack(alignment: .leading, spacing: 0) {
            if circle.members.isEmpty {
                Text(Self.nobody)
                    .font(LooreFont.sans(14, .light))
                    .foregroundStyle(LooreColor.textSecondary)
            }
            ForEach(circle.members, id: \.username) { member in
                HairlineDivider()
                HStack(spacing: 12) {
                    Text(member.username)
                        .font(LooreFont.sans(16, .regular))
                        .foregroundStyle(LooreColor.textPrimary)
                    if member.onlyNew {
                        Text("new entries only")
                            .font(LooreFont.sans(12.8, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                    }
                    Spacer(minLength: 8)
                    CircleQuietButton(title: "Remove") { dialog = .remove(member.username) }
                        .disabled(model.busy)
                        .accessibilityLabel("Remove \(member.username)")
                }
                .frame(minHeight: 48)
            }
            if !circle.members.isEmpty { HairlineDivider() }
        }
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            CircleEyebrow(text: "Add a person by their Loore name")
            HStack(spacing: 10) {
                TextField("", text: $name, prompt: loorePrompt("@name"))
                    .circleField()
                    .submitLabel(.done)
                    .onSubmit(startAdd)
                    .onChange(of: name) { _, _ in addError = nil }
                    .accessibilityLabel("Add a person by their Loore name")
                    .accessibilityIdentifier("circles.addName")
                Button("Add", action: startAdd)
                    .buttonStyle(.loorePrimary)
                    .disabled(adding || name.trimmingCharacters(in: .whitespaces).isEmpty)
                    .accessibilityIdentifier("circles.add")
            }
            if let addError {
                Text(addError).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.error)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("circles.addError")
            } else {
                Text(Self.lookupHint)
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func startAdd() {
        let typed = name.trimmingCharacters(in: .whitespaces)
        guard !typed.isEmpty, !adding else { return }
        adding = true
        addError = nil
        Task {
            defer { adding = false }
            switch await model.previewMember(circle, username: typed) {
            case .success(let preview): dialog = .add(preview)
            case .failure(let failure): addError = failure.message
            }
        }
    }
}
