import SwiftUI
import Observation

/// What the author picked for an entry of their own, and who that resolves to
/// (web `CircleAudiencePicker` state + NodeForm's `audience` / `audienceInfo`).
///
/// Names are looked up when the field is left or a separator is typed, not on
/// every keystroke: the lookup finds exact names only, and half a name is a
/// name nobody has.
@MainActor
@Observable
final class CircleAudienceState {
    @ObservationIgnored private let api: APIClient
    /// The user's circles; nil until loaded.
    private(set) var circles: [PeopleCircle]?
    private(set) var loadError: String?
    var circleIds: [Int] = []
    var namesText = ""
    /// The names that have been looked up (finished names only).
    private(set) var committed: [String] = []
    private(set) var readers: [String] = []
    private(set) var error: String?
    private(set) var pending = false
    /// The last check before saving refused the text: it quotes an entry one
    /// of these people cannot read (the server's answer, for the dialog).
    private(set) var quotesRefusal: QuotesNotPublic?
    /// The audience the entry was saved with (edit): nil = not shared before.
    private(set) var savedKey: String?
    /// An entry of the author's own with something above it in its thread:
    /// "Share also the discussion leading up to this node?" is offered, off
    /// by default. Sharing an entry does not share what is above it.
    private(set) var offersSharePath = false
    var sharePath = false
    /// The path above the entry is shared now (edit).
    private(set) var pathWasShared = false
    /// The entry being edited: a circle it is already shared with keeps the
    /// time of that share, so a person added since with "Only new entries"
    /// does not read it and is not named. nil for a new entry.
    var entryId: Int?
    @ObservationIgnored private var request = 0
    @ObservationIgnored private var previewTask: Task<Void, Never>?
    @ObservationIgnored private var loaded = false

    init(api: APIClient) {
        self.api = api
    }

    static let noReadersMessage = "Nobody could read this entry yet. Choose a circle with people in it, or name a person."
    /// The check before saving found readers the sentence did not name.
    static let readersChangedMessage = "Who can read this has changed. Check the names above, then share again."
    static let previewFailed = "The readers could not be worked out. Try again."

    var names: [String] { CirclesWording.splitNames(namesText) }

    /// Order-free identity of a choice, to tell whether it changed.
    static func key(circleIds: [Int], names: [String]) -> String {
        circleIds.sorted().map(String.init).joined(separator: ",") + "|"
            + names.map { $0.lowercased() }.sorted().joined(separator: ",")
    }

    var key: String { Self.key(circleIds: circleIds, names: names) }
    var changed: Bool { savedKey != key }
    var nothingChosen: Bool { circleIds.isEmpty && committed.isEmpty }

    /// The JSON fields that go with `privacy_level: "circles"`. `share_path`
    /// goes with them as true when the toggle is on, as false only when the
    /// path was shared and the toggle was turned off (that takes the path
    /// back to only the author), and not at all otherwise.
    var fields: [String: JSONValue] {
        var fields: [String: JSONValue] = ["circle_ids": .array(circleIds.map(JSONValue.int)),
                                           "circle_usernames": .array(names.map(JSONValue.string))]
        if offersSharePath {
            if sharePath {
                fields["share_path"] = .bool(true)
            } else if pathWasShared {
                fields["share_path"] = .bool(false)
            }
        }
        return fields
    }

    /// The entry has a discussion above it: offer the toggle, on when that
    /// path is already shared (edit).
    func offerSharePath(shared: Bool = false) {
        offersSharePath = true
        sharePath = shared
        pathWasShared = shared
    }

    /// The line under the picker: an error, a prompt to choose, or the readers.
    var sentence: String {
        if let loadError { return loadError }
        if let error { return error }
        if nothingChosen {
            return "Choose a circle or name a person. Until then nobody but you can read this entry."
        }
        if !readers.isEmpty { return CirclesWording.readersSentence(readers) }
        if pending { return "Looking up who can read this…" }
        return "Nobody is in this circle yet. Add a person to it, or name someone."
    }

    var sentenceIsError: Bool { loadError != nil || error != nil }

    func shareLabel() -> String {
        CirclesWording.shareButtonLabel(circles: circles ?? [], circleIds: circleIds, names: names, readers: readers)
    }

    /// Loads the user's circles (again after "Edit circles").
    func load(force: Bool = false) async {
        guard force || !loaded else { return }
        loaded = true
        do {
            let answer: CirclesResponse = try await api.get(APIPath.circles)
            circles = answer.circles
            loadError = nil
            // A circle deleted meanwhile is no longer a choice.
            let known = Set(answer.circles.map(\.id))
            if circleIds.contains(where: { !known.contains($0) }) {
                circleIds.removeAll { !known.contains($0) }
                schedulePreview()
            }
        } catch {
            if circles == nil { circles = [] }
            loadError = "Your circles could not be loaded."
        }
    }

    /// The entry's saved audience (edit).
    func seed(circleIds: [Int], usernames: [String]) {
        self.circleIds = circleIds
        namesText = usernames.map { "@\($0)" }.joined(separator: ", ")
        committed = names
        savedKey = key
        schedulePreview()
    }

    func toggle(_ id: Int) {
        if let index = circleIds.firstIndex(of: id) {
            circleIds.remove(at: index)
        } else {
            circleIds.append(id)
        }
        schedulePreview()
    }

    func namesChanged(_ text: String) {
        namesText = text
        if CirclesWording.endsAName(text) { commitNames() }
    }

    /// The field was left: every typed name counts as finished.
    func commitNames() {
        let now = names
        guard now != committed else { return }
        committed = now
        schedulePreview()
    }

    private func schedulePreview() {
        previewTask?.cancel()
        request += 1
        let mine = request
        guard !nothingChosen else {
            readers = []
            error = nil
            pending = false
            return
        }
        pending = true
        let ids = circleIds, typed = committed
        previewTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 200_000_000)
            guard !Task.isCancelled, let self else { return }
            let result = await self.preview(circleIds: ids, names: typed)
            guard mine == self.request else { return }
            switch result {
            case .success(let found):
                self.readers = found
                self.error = nil
            case .failure(let failure):
                self.readers = []
                self.error = failure.message
            }
            self.pending = false
        }
    }

    struct PreviewFailure: Error {
        var message: String
        /// The text quotes an entry one of these people cannot read (409
        /// `quotes_not_shared`, asked only with the text before saving).
        var quotes: QuotesNotPublic? = nil
    }

    private func preview(circleIds: [Int], names: [String],
                         content: String? = nil, parentId: Int? = nil) async -> Result<[String], PreviewFailure> {
        do {
            var body: [String: JSONValue] = [
                "circle_ids": .array(circleIds.map(JSONValue.int)),
                "usernames": .array(names.map(JSONValue.string)),
            ]
            if let entryId { body["node_id"] = .int(entryId) }
            if let content {
                body["content"] = .string(content)
                // A new entry's place in its thread, and whether the
                // discussion leading up to it is shared with it: that
                // discussion is checked too.
                if let parentId, entryId == nil { body["parent_id"] = .int(parentId) }
                if offersSharePath && sharePath { body["share_path"] = .bool(true) }
            }
            let answer: AudiencePreview = try await api.post(APIPath.circlesAudiencePreview, json: .object(body))
            return .success(answer.readers)
        } catch {
            return .failure(PreviewFailure(
                message: (error as? APIError)?.userMessage(fallback: Self.previewFailed) ?? Self.previewFailed,
                quotes: QuotesNotPublic(error)))
        }
    }

    /// The check before saving, with every typed name (also one not yet
    /// looked up): nil when someone would read the entry and the sentence on
    /// the screen names exactly them, else the plain message the form shows.
    /// Nobody is shared with unless the sentence named them: when the readers
    /// differ from the ones it named (a name not yet looked up, a lookup not
    /// back yet), the sentence is brought up to date and the save stops. The
    /// server refuses the no-readers cases too. With `content`, the text is
    /// checked as well: a quote of an entry one of these people cannot read
    /// stops the save, and `quotesRefusal` holds the server's answer.
    func refusalBeforeSaving(content: String? = nil, parentId: Int? = nil) async -> String? {
        let shown = readers
        committed = names
        quotesRefusal = nil
        previewTask?.cancel()
        request += 1
        switch await preview(circleIds: circleIds, names: committed, content: content, parentId: parentId) {
        case .success(let found):
            readers = found
            error = nil
            pending = false
            if found.isEmpty { return Self.noReadersMessage }
            return Self.sameNames(shown, found) ? nil : Self.readersChangedMessage
        case .failure(let failure):
            pending = false
            quotesRefusal = failure.quotes
            return failure.message
        }
    }

    /// The same people, in any order, case ignored.
    static func sameNames(_ a: [String], _ b: [String]) -> Bool {
        a.map { $0.lowercased() }.sorted() == b.map { $0.lowercased() }.sorted()
    }
}

/// The panel under "Who can read this" when Circles is chosen: the author's
/// circles as toggles, a field for Loore names "for this entry only", and the
/// sentence that names the readers before saving (web `CircleAudiencePicker`).
struct CircleAudiencePicker: View {
    @Bindable var state: CircleAudienceState
    var disabled = false
    /// "Edit circles": nil opens "Your circles" in a sheet over the form, so
    /// the entry being written stays where it is.
    var onEditCircles: (() -> Void)?
    @FocusState private var namesFocused: Bool
    @State private var editingCircles = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            FlowLayout(spacing: 8, lineSpacing: 8) {
                if state.circles == nil {
                    Text("Loading your circles…")
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .frame(minHeight: 44)
                }
                ForEach(state.circles ?? []) { circle in
                    chip(circle)
                }
                if let circles = state.circles, circles.isEmpty, state.loadError == nil {
                    Text("You have no circles yet.")
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .frame(minHeight: 44)
                }
                Button(state.circles?.isEmpty == true ? "Make a circle" : "Edit circles") {
                    if let onEditCircles { onEditCircles() } else { editingCircles = true }
                }
                .buttonStyle(.plain)
                .font(LooreFont.sans(14, .light))
                .foregroundStyle(LooreColor.accent)
                .frame(minHeight: 44)
                .padding(.horizontal, 8)
                .accessibilityIdentifier("circles.edit")
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Or name people for this entry only")
                    .font(LooreFont.sans(12.8, .light))
                    .foregroundStyle(LooreColor.textSecondary)
                TextField("", text: Binding(get: { state.namesText }, set: { state.namesChanged($0) }),
                          prompt: loorePrompt("@name, @name"))
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($namesFocused)
                    .submitLabel(.done)
                    .onSubmit { state.commitNames() }
                    .onChange(of: namesFocused) { _, focused in
                        if !focused { state.commitNames() }
                    }
                    .disabled(disabled)
                    .font(LooreFont.sans(16, .light))
                    .foregroundStyle(LooreColor.textPrimary)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 46)
                    .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.small))
                    .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.borderHover))
                    .accessibilityLabel("Or name people for this entry only")
                    .accessibilityIdentifier("circles.names")
            }
            Text(state.sentence)
                .font(LooreFont.serif(20.8, .regular, relativeTo: .title3))
                .foregroundStyle(state.sentenceIsError ? LooreColor.error : LooreColor.textPrimary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("circles.sentence")
            if state.offersSharePath {
                VStack(alignment: .leading, spacing: 4) {
                    Button {
                        state.sharePath.toggle()
                    } label: {
                        HStack(spacing: 12) {
                            Image(systemName: state.sharePath ? "checkmark.square" : "square")
                                .font(.system(size: 20, weight: .light))
                                .foregroundStyle(state.sharePath ? LooreColor.accent : LooreColor.textSecondary)
                                .accessibilityHidden(true)
                            Text(CirclesWording.sharePathLabel)
                                .multilineTextAlignment(.leading)
                        }
                        .font(LooreFont.sans(14, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(disabled)
                    .accessibilityAddTraits(.isToggle)
                    .accessibilityValue(state.sharePath ? "On" : "Off")
                    .accessibilityIdentifier("circles.sharePath")
                    Text(CirclesWording.sharePathNote)
                        .font(LooreFont.sans(12.8, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Text("You can change this later, or take the entry back to only you.")
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: LooreRadius.small))
        .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
        .padding(.top, 4)
        .task { await state.load() }
        .sheet(isPresented: $editingCircles, onDismiss: { Task { await state.load(force: true) } }) {
            NavigationStack {
                CirclesView()
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { editingCircles = false }
                        }
                    }
            }
        }
    }

    private func chip(_ circle: PeopleCircle) -> some View {
        let selected = state.circleIds.contains(circle.id)
        return Button {
            state.toggle(circle.id)
        } label: {
            HStack(spacing: 8) {
                if selected {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .semibold))
                        .accessibilityHidden(true)
                }
                Text(circle.name).lineLimit(1)
            }
            .font(LooreFont.sans(15, selected ? .regular : .light))
            .foregroundStyle(selected ? LooreColor.bgDeep : LooreColor.textPrimary)
            .padding(.horizontal, 16)
            .frame(minHeight: 44)
            .background(selected ? LooreColor.accent : Color.clear, in: Capsule())
            .overlay(Capsule().strokeBorder(selected ? LooreColor.accent : LooreColor.borderHover))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("circles.chip.\(circle.id)")
    }
}
