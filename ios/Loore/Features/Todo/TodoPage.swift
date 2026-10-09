import SwiftUI

/// The todo checklist (web `TodoPage`, map E §1.3): sections, nested items
/// (collapsed by default), tick/untick, per-row "+", quick-add to Today, raw
/// markdown editing, version history.
///
/// Every in-place edit (`PATCH`) re-fetches the todo first and applies the same
/// text-keyed edit to the fresh content (design doc §10), sending the revision
/// it edited (TodoModel); the list is also re-fetched after a "todo changed"
/// signal and when the app comes back.
///
/// The editor's Save is checked against the version the editor was opened on
/// (#476). When the list changed meanwhile (a todo merge, a tick elsewhere), the
/// page shows the choice instead of dropping those changes: save the user's
/// text anyway, or show the newest list with the user's text kept below the
/// editor to copy from.
struct TodoPage: View {
    let onSelect: (WorkspaceDocument) -> Void

    @Environment(AppState.self) private var app
    @Environment(\.scenePhase) private var scenePhase
    @State private var model = TodoModel()
    @State private var editing = false
    @State private var editContent = ""
    @State private var saving = false
    /// The version the editor was opened on (nil for Create).
    @State private var editBase: TodoDoc?
    /// The newest version, after a Save was refused because the list changed.
    @State private var conflict: TodoDoc?
    /// The user's text after "Show the newest list", kept to copy from.
    @State private var keptText: String?
    @State private var quickAddOpen = false
    @State private var quickAddText = ""
    @State private var quickAddSaving = false
    @State private var addingKey: String?
    @State private var showHistory = false
    @FocusState private var quickAddFocused: Bool

    static let createTemplate = "## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n"

    var body: some View {
        WorkspaceScroll(active: .todo, onSelect: onSelect) {
            if !model.loading { content }
        }
        .task {
            model.app = app
            await fetchTodo()
        }
        .onChange(of: app.signals.todoChanged) { _, _ in Task { await fetchTodo() } }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active, !editing { Task { await fetchTodo() } }
        }
        .sheet(isPresented: $showHistory) {
            VersionHistorySheet(source: historySource)
        }
        .alert("Your todo list changed after you opened the editor",
               isPresented: Binding(get: { conflict != nil }, set: { if !$0 { conflict = nil } }),
               presenting: conflict) { newest in
            Button("Save mine anyway") { save(over: newest) }
            Button("Show the newest list") { showNewest(newest) }
            Button("Keep editing", role: .cancel) {}
        } message: { _ in
            Text("Your text wasn't saved. Saving it anyway replaces those changes; the newer version stays in history.")
        }
    }

    private var todo: TodoDoc? { model.todo }

    @ViewBuilder private var content: some View {
        DocTitleRow(title: "Todo") {
            if let todo {
                HStack(spacing: 12) {
                    VersionChip(text: "v\(todo.versionNumber.map(String.init) ?? "") · \(LooreDateFormat.date(todo.createdAt))") {
                        if editing { save() } else { openEditor(on: todo) }
                    }
                    HistoryLink { showHistory = true }
                    if !editing { quickAddToggle }
                }
            }
        }
        if todo != nil && quickAddOpen && !editing {
            TextField("", text: $quickAddText, prompt: loorePrompt("Add a task to Today and press Enter"))
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.vertical, 8)
                .padding(.horizontal, 12)
                .background(LooreColor.bgInput, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                .focused($quickAddFocused)
                .submitLabel(.done)
                .onSubmit(quickAdd)
                .disabled(quickAddSaving)
                .padding(.bottom, 8)
                .accessibilityIdentifier("todo.quickAddField")
        }
        if let todo {
            DocMetaLine(text: "Last updated by \(TodoSections.updatedByLabel(todo.generatedBy)) · \(LooreDateFormat.date(todo.createdAt))")
        }
        DocDivider()

        if todo == nil && !editing {
            emptyState
        }
        if editing {
            DocEditor(text: $editContent, identifier: "todo.editor", label: "Todo list")
            DocEditButtons(saving: saving, onSave: save, onCancel: {
                editing = false
                keptText = nil
                if let todo { editContent = todo.content }
            })
            if let keptText {
                keptTextView(keptText)
            }
        } else if let todo {
            checklist(todo)
        }
    }

    private var quickAddToggle: some View {
        Button {
            quickAddOpen.toggle()
            if quickAddOpen { quickAddFocused = true } else { quickAddText = "" }
        } label: {
            Text(quickAddOpen ? "×" : "+")
                .font(LooreFont.sans(16, .light))
                .foregroundStyle(quickAddOpen ? LooreColor.accent : LooreColor.textMuted)
                .frame(width: 24, height: 24)
                .overlay(Circle().strokeBorder(quickAddOpen ? LooreColor.accent : LooreColor.border))
                .frame(width: 36, height: 36)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(quickAddOpen ? "Close quick-add" : "Quick-add task")
        .accessibilityHint(quickAddOpen ? "" : "Quick-add task to Today")
        .accessibilityIdentifier("todo.quickAdd")
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Text("Your todo list is the concrete counterpart to intentions — specific, finishable tasks; as you write and talk, the AI notices completions, new items, and priorities, and proposes updates that apply only when you confirm.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(6.3)
            Text("No todo list yet. Create one to track your tasks.")
                .font(LooreFont.sans(14.4, .regular))
                .foregroundStyle(LooreColor.textMuted)
            Button("Create Todo") {
                editContent = Self.createTemplate
                editBase = nil
                keptText = nil
                editing = true
            }
            .buttonStyle(.looreFilled)
            .accessibilityIdentifier("todo.create")
        }
        .multilineTextAlignment(.center)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    private func checklist(_ todo: TodoDoc) -> some View {
        let sections = TodoSections.parse(todo.content)
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(sections.enumerated()), id: \.offset) { _, section in
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 8) {
                        Text(section.title.uppercased())
                            .font(LooreFont.sans(10.9, .medium))
                            .tracking(1.96)
                            .foregroundStyle(LooreColor.accent.opacity(0.6))
                        Text("\(TodoSections.countAll(section.items))")
                            .font(LooreFont.sans(10.4, .light))
                            .tracking(0.5)
                            .foregroundStyle(LooreColor.textMuted)
                    }
                    .padding(.bottom, 19)
                    ForEach(Array(section.items.enumerated()), id: \.offset) { _, item in
                        TodoItemRow(item: item, depth: 0, addingKey: $addingKey, onToggle: toggle, onInsertAfter: insertAfter)
                    }
                    if section.items.isEmpty {
                        Text("No items")
                            .font(LooreFont.sansOblique(12.8, .light))
                            .foregroundStyle(LooreColor.textMuted)
                            .padding(.vertical, 4)
                    }
                }
                .padding(.bottom, 40)
            }
        }
    }

    // MARK: Loading and saving

    private func fetchTodo() async {
        let fresh = await model.fetch()
        if let fresh, !editing { editContent = fresh.content }
    }

    private func openEditor(on todo: TodoDoc) {
        editContent = todo.content
        editBase = todo
        keptText = nil
        editing = true
    }

    /// `PUT /api/todo/` (Save in edit mode and the first create): a new version,
    /// checked against the version the editor was opened on.
    private func save() {
        save(over: editBase)
    }

    /// Saves the editor's text over `base`: the version the editor was opened
    /// on, or the newest one for "Save mine anyway". A refusal shows the choice.
    private func save(over base: TodoDoc?) {
        guard !saving, !editContent.jsTrimmed.isEmpty else { return }
        saving = true
        Task {
            switch await model.save(editContent, over: base) {
            case .saved:
                editing = false
                keptText = nil
            case .changed(let newest):
                conflict = newest
            case .failed:
                break
            }
            saving = false
        }
    }

    /// "Show the newest list": the editor gets the newest list, the next Save is
    /// checked against it, and the user's text stays below to copy from.
    private func showNewest(_ newest: TodoDoc) {
        keptText = editContent
        editContent = newest.content
        editBase = newest
    }

    private func keptTextView(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Your text, not saved. Copy what you need into the list above, then Save.")
                .font(LooreFont.sans(12.8, .light))
                .foregroundStyle(LooreColor.textMuted)
            Text(text)
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(12)
                .overlay(RoundedRectangle(cornerRadius: LooreRadius.small)
                    .strokeBorder(LooreColor.border, style: StrokeStyle(lineWidth: 1, dash: [4])))
                .accessibilityLabel("Your text, not saved")
                .accessibilityIdentifier("todo.keptText")
            HStack(spacing: 8) {
                Button("Copy") {
                    UIPasteboard.general.string = text
                    app.toasts.show("Copied your text")
                }
                .buttonStyle(.looreOutline)
                Button("Discard") { keptText = nil }
                    .buttonStyle(.looreOutline)
            }
        }
        .padding(.top, 20)
    }

    private func toggle(_ item: TodoSections.Item) {
        Task { await model.toggle(item) }
    }

    private func insertAfter(_ item: TodoSections.Item, _ text: String) {
        Task { await model.insertAfter(item, text: text) }
    }

    private func quickAdd() {
        let task = quickAddText.jsTrimmed
        guard !task.isEmpty, !quickAddSaving, todo != nil else { return }
        quickAddSaving = true
        quickAddText = ""
        Task {
            if !(await model.quickAdd(task)) { quickAddText = task }
            quickAddSaving = false
            quickAddFocused = true
        }
    }

    private var historySource: VersionHistorySource {
        let api = app.api
        return VersionHistorySource(
            title: "Todo History",
            loadVersions: { try await (api.get(APIPath.todoVersions) as VersionList).versions },
            loadContent: { version in
                guard let id = version.id.rowId else { return "" }
                return try await (api.get(APIPath.todoVersion(id)) as VersionContent).content
            },
            revert: { version in
                guard let id = version.id.rowId else { return }
                let envelope: TodoEnvelope = try await api.post(APIPath.todoRevert(id))
                await MainActor.run {
                    model.adopt(envelope.todo)
                    if let reverted = envelope.todo { editContent = reverted.content }
                }
            })
    }
}

/// One checklist row (web `TodoItem`): round check, inline markdown, "▶ n"
/// disclosure for children (collapsed by default), "+" to add a row below.
private struct TodoItemRow: View {
    let item: TodoSections.Item
    let depth: Int
    @Binding var addingKey: String?
    let onToggle: (TodoSections.Item) -> Void
    let onInsertAfter: (TodoSections.Item, String) -> Void

    @State private var collapsed = true
    @State private var addText = ""
    @FocusState private var addFocused: Bool

    private var adding: Bool { addingKey == item.text }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                if let checked = item.checked {
                    Button { onToggle(item) } label: {
                        ZStack {
                            Circle()
                                .strokeBorder(checked ? LooreColor.accentDim : LooreColor.borderHover, lineWidth: 1.5)
                                .background(Circle().fill(checked ? LooreColor.accentDim : Color.clear))
                            if checked {
                                Text("✓").font(.system(size: 9.6, weight: .semibold)).foregroundStyle(LooreColor.bgDeep)
                            }
                        }
                        .frame(width: 18, height: 18)
                        .padding(.top, 2)
                        .frame(width: 30, height: 22, alignment: .top)
                        .contentShape(Rectangle().inset(by: -10))
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, -6)
                    .accessibilityLabel(checked ? "Mark as not done" : "Mark as done")
                    .accessibilityIdentifier("todo.check.\(item.text)")
                } else {
                    Color.clear.frame(width: 18, height: 1)
                }
                MarkdownView(markdown: item.text, style: item.checked == nil ? .todoCategory : .todoItem)
                    .strikethrough(item.checked == true)
                    .opacity(item.checked == true ? 0.4 : 1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !item.children.isEmpty {
                    Button { withAnimation(LooreMotion.micro) { collapsed.toggle() } } label: {
                        HStack(spacing: 4) {
                            Text("▶")
                                .font(.system(size: 8.8))
                                .rotationEffect(.degrees(collapsed ? 0 : 90))
                            Text("\(TodoSections.countAll(item.children))")
                                .font(LooreFont.sans(10.4, .light))
                                .tracking(0.5)
                        }
                        .foregroundStyle(LooreColor.textMuted)
                        .frame(minWidth: 30, minHeight: 28)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(collapsed ? "Show \(item.children.count) sub-items" : "Hide sub-items")
                }
                if item.checked != nil {
                    Button {
                        addText = ""
                        addingKey = item.text
                        addFocused = true
                    } label: {
                        Text("+")
                            .font(LooreFont.sans(19.2, .light))
                            .foregroundStyle(LooreColor.accent)
                            .opacity(0.55)
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Add an item below")
                }
            }
            .padding(.vertical, 12)
            .padding(.leading, CGFloat(depth) * 24)
            .overlay(alignment: .bottom) { Rectangle().fill(LooreColor.bgSurface).frame(height: 1) }

            if adding {
                HStack(spacing: 12) {
                    Circle()
                        .strokeBorder(LooreColor.borderHover, style: StrokeStyle(lineWidth: 1.5, dash: [3, 2]))
                        .frame(width: 18, height: 18)
                        .opacity(0.5)
                    TextField("", text: $addText, prompt: loorePrompt("New item…"))
                        .font(LooreFont.sans(14.7, .light))
                        .foregroundStyle(LooreColor.textPrimary)
                        .padding(.vertical, 6)
                        .padding(.horizontal, 10)
                        .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: LooreRadius.control))
                        .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                        .focused($addFocused)
                        .submitLabel(.done)
                        .onSubmit(submitAdd)
                        .onChange(of: addFocused) { _, focused in
                            if !focused && addText.jsTrimmed.isEmpty { close() }
                        }
                        .accessibilityIdentifier("todo.newItem")
                }
                .padding(.vertical, 8)
                .padding(.leading, CGFloat(depth) * 24)
                .onAppear { addFocused = true }
            }
            if !item.children.isEmpty && !collapsed {
                ForEach(Array(item.children.enumerated()), id: \.offset) { _, child in
                    TodoItemRow(item: child, depth: depth + 1, addingKey: $addingKey,
                                onToggle: onToggle, onInsertAfter: onInsertAfter)
                }
            }
        }
    }

    private func submitAdd() {
        let text = addText.jsTrimmed
        if !text.isEmpty { onInsertAfter(item, text) }
        close()
    }

    private func close() {
        if addingKey == item.text { addingKey = nil }
        addText = ""
    }
}

extension MarkdownStyle {
    /// A todo item (sans .92rem 300, secondary, line-height 1.5, no paragraph margin).
    static let todoItem = MarkdownStyle(fontSize: 14.7, weight: .light, color: LooreColor.textSecondary,
                                        lineHeight: 1.5, paragraphMargin: 0)
    /// A plain `- ` category line (no checkbox): primary colour, weight 400.
    static let todoCategory = MarkdownStyle(fontSize: 14.7, weight: .regular, color: LooreColor.textPrimary,
                                            lineHeight: 1.5, paragraphMargin: 0)
}
