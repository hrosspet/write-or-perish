import SwiftUI

/// The todo checklist (web `TodoPage`, map E §1.3): sections, nested items
/// (collapsed by default), tick/untick, per-row "+", quick-add to Today, raw
/// markdown editing, version history.
///
/// Saves overwrite the latest version with no version check (E §15), so every
/// in-place edit (`PATCH`) re-fetches the todo first and applies the same
/// text-keyed edit to the fresh content (design doc §10); the list is also
/// re-fetched after a "todo changed" signal and when the app comes back.
struct TodoPage: View {
    let onSelect: (WorkspaceDocument) -> Void

    @Environment(AppState.self) private var app
    @Environment(\.scenePhase) private var scenePhase
    @State private var todo: TodoDoc?
    @State private var loading = true
    @State private var editing = false
    @State private var editContent = ""
    @State private var saving = false
    @State private var quickAddOpen = false
    @State private var quickAddText = ""
    @State private var quickAddSaving = false
    @State private var addingKey: String?
    @State private var showHistory = false
    @FocusState private var quickAddFocused: Bool

    static let createTemplate = "## Today\n\n- [ ] \n\n## Upcoming\n\n- [ ] \n\n## Completed recently\n"

    var body: some View {
        WorkspaceScroll(active: .todo, onSelect: onSelect) {
            if !loading { content }
        }
        .task { await fetchTodo() }
        .onChange(of: app.signals.todoChanged) { _, _ in Task { await fetchTodo() } }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active, !editing { Task { await fetchTodo() } }
        }
        .sheet(isPresented: $showHistory) {
            VersionHistorySheet(source: historySource)
        }
    }

    @ViewBuilder private var content: some View {
        DocTitleRow(title: "Todo") {
            if let todo {
                HStack(spacing: 12) {
                    VersionChip(text: "v\(todo.versionNumber.map(String.init) ?? "") · \(LooreDateFormat.date(todo.createdAt))") {
                        if editing { save() } else {
                            editContent = todo.content
                            editing = true
                        }
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
            DocEditor(text: $editContent, identifier: "todo.editor")
            DocEditButtons(saving: saving, onSave: save, onCancel: {
                editing = false
                if let todo { editContent = todo.content }
            })
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
        .accessibilityLabel("Quick-add task")
        .accessibilityHint("Quick-add task to Today")
        .accessibilityIdentifier("todo.quickAdd")
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Text("Your todo list is the concrete counterpart to intentions — specific, finishable tasks; as you write and talk, the AI notices completions, new items, and priorities, and proposes updates that apply only when you confirm.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .lineSpacing(9)
            Text("No todo list yet. Create one to track your tasks.")
                .font(LooreFont.sans(14.4, .light))
                .foregroundStyle(LooreColor.textMuted)
            Button("Create Todo") {
                editContent = Self.createTemplate
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
        do {
            let envelope: TodoEnvelope = try await app.api.get(APIPath.todo)
            todo = envelope.todo
            if let fresh = envelope.todo, !editing { editContent = fresh.content }
        } catch {
            // The web logs load errors only.
        }
        loading = false
    }

    /// `PUT /api/todo/` (Save in edit mode and the first create): a new version.
    private func save() {
        guard !saving, !editContent.jsTrimmed.isEmpty else { return }
        saving = true
        Task {
            defer { saving = false }
            do {
                let envelope: TodoEnvelope = try await app.api.put(
                    APIPath.todo, json: ["content": .string(editContent), "generated_by": "user"])
                todo = envelope.todo
                editing = false
                app.signals.post(.todoChanged)
            } catch {
                // Logged only on the web.
            }
        }
    }

    private func toggle(_ item: TodoSections.Item) {
        let key = MarkdownEdits.stripInlineMarkdown(item.text).jsTrimmed
        let checked = item.checked ?? false
        patch(failure: "Couldn't save change — reverted") { content in
            MarkdownEdits.toggleCheckbox(content, itemText: key, currentChecked: checked)
        }
    }

    private func insertAfter(_ item: TodoSections.Item, _ text: String) {
        let key = MarkdownEdits.stripInlineMarkdown(item.text).jsTrimmed
        patch(failure: "Couldn't add task — reverted", skipUnchanged: true) { content in
            MarkdownEdits.insertItemAfter(content, afterItemText: key, newText: text)
        }
    }

    private func quickAdd() {
        let task = quickAddText.jsTrimmed
        guard !task.isEmpty, !quickAddSaving, todo != nil else { return }
        quickAddSaving = true
        quickAddText = ""
        patch(failure: nil, onFailure: { quickAddText = task }, onDone: {
            quickAddSaving = false
            quickAddFocused = true
        }) { content in
            MarkdownEdits.appendItemToSection(content, sectionTitle: "Today", task: task, createAtStart: true)
        }
    }

    /// Optimistic in-place edit: apply `edit` to what is shown, then re-fetch,
    /// apply the same edit to the server's latest content and `PATCH` it. On
    /// failure the shown content goes back and a toast names the reason.
    private func patch(failure: String?, skipUnchanged: Bool = false, onFailure: @escaping () -> Void = {},
                       onDone: @escaping () -> Void = {}, edit: @escaping (String) -> String) {
        guard let current = todo else { onDone(); return }
        let optimistic = edit(current.content)
        if skipUnchanged && optimistic == current.content { onDone(); return }
        todo?.content = optimistic
        Task {
            defer { onDone() }
            do {
                let fresh: TodoEnvelope = try await app.api.get(APIPath.todo, poll: true)
                let base = fresh.todo?.content ?? current.content
                let updated = edit(base)
                let answer: TodoEnvelope = try await app.api.patch(APIPath.todo, json: ["content": .string(updated)])
                if let saved = answer.todo { todo = saved } else { todo?.content = updated }
            } catch {
                todo?.content = current.content
                onFailure()
                if let failure {
                    let reason = (error as? APIError)?.serverMessage ?? (error as? APIError)?.errorDescription
                        ?? "Unknown error"
                    app.toasts.show("\(failure) (\(reason))")
                }
            }
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
                    todo = envelope.todo
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
                        .frame(width: 30, height: 30, alignment: .top)
                        .contentShape(Rectangle())
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
