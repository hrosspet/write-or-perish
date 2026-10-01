import SwiftUI
import Observation
import os

/// A node the kebab actions act on (focal, ancestor or child).
struct NodeTarget: Identifiable, Equatable {
    var id: Int
    var content: String
    var privacy: PrivacyLevel?
    var aiUsage: AIUsage?
    var childCount: Int
    var hasChildren: Bool
    var hasTTS: Bool
    var hasPromptArtifact: Bool
}

/// State and behaviour of one thread screen (web `NodeDetail`, map D §1, §4.7–4.8, §5).
/// Each pushed `/node/<id>` gets its own model, as the web remounts per node.
@MainActor
@Observable
final class ThreadModel {
    let nodeId: Int
    @ObservationIgnored private let initialAwaitLLM: Int?
    @ObservationIgnored private weak var app: AppState?
    @ObservationIgnored private let log = Logger(subsystem: "org.loore.app", category: "thread")

    private(set) var node: NodeDetail?
    private(set) var loading = true
    /// Replaces the page, as on the web.
    private(set) var pageError: String?
    private(set) var quotes = QuoteData()

    // Reply generation
    private(set) var llmTaskNodeId: Int?
    private(set) var llmRequesting = false
    private(set) var llmPollStatus: TaskStatus?
    private(set) var streamText = ""
    var selectedModel: String?

    // UI state
    private(set) var pinLoading = false
    private(set) var voiceLoading = false
    private(set) var readLoading = false
    /// The Read button's own model (read models only, #355); nil until its picker loads.
    var readModel: String?
    var toolActionsExpanded = false
    var replyTarget: NodeTarget?
    var editTarget: NodeTarget?
    var deleteTarget: NodeTarget?
    var showPromptEditConfirm = false
    var showEditForm = false
    var pendingPromptDelete: (targetId: Int, withDescendants: Bool)?
    var showPromptDeleteDialog = false
    private(set) var deleteChecking = false

    @ObservationIgnored private var awaitHandled = false
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var streamTask: Task<Void, Never>?
    @ObservationIgnored private var quotesKey = ""
    @ObservationIgnored private var quotesVersion = 0

    init(nodeId: Int, awaitLLM: Int?, app: AppState) {
        self.nodeId = nodeId
        self.initialAwaitLLM = awaitLLM
        self.app = app
        selectedModel = app.user?.preferredModel
    }

    // MARK: Derived

    private var me: CurrentUser? { app?.user }

    var isOwner: Bool {
        guard let node, let me else { return false }
        return node.user?.id == me.id || (node.nodeType == .llm && node.parentUserId == me.id)
    }

    func ownedByMe(userId: Int?, parentUserId: Int?, nodeType: NodeType) -> Bool {
        guard let me else { return false }
        return userId == me.id || (nodeType == .llm && parentUserId == me.id)
    }

    var isLLMNode: Bool { node.map { $0.nodeType == .llm || $0.llmModel != nil } ?? false }

    var isLLMPending: Bool { isLLMNode && (node?.llmTaskStatus?.isInFlight ?? false) }

    /// A read waiting in a provider batch (`_batch` submitted/cancelling).
    var isBatchWait: Bool {
        node?.toolCallsMeta?.contains { $0.name == "_batch" && ["submitted", "cancelling"].contains($0.status ?? "") } ?? false
    }

    var isPublicThread: Bool { node?.privacyLevel == .public }

    var autoGeneratePreference: Bool {
        get { UserDefaults.standard.object(forKey: DefaultsKey.autoGenerate) == nil
            ? true : UserDefaults.standard.bool(forKey: DefaultsKey.autoGenerate) }
        set { UserDefaults.standard.set(newValue, forKey: DefaultsKey.autoGenerate) }
    }

    /// Public threads force auto-generate off (#228).
    func autoGenerateActive(_ preference: Bool) -> Bool { isPublicThread ? false : preference }

    var parentAncestor: AncestorNode? { node?.ancestors.last }

    var isReadReply: Bool {
        guard isLLMNode, let node else { return false }
        return node.readReply
            || (node.toolCallsMeta?.contains { ["_batch", "_read"].contains($0.name) } ?? false)
            || ["read", "read_thread"].contains(parentAncestor?.systemPrompt.promptKey ?? "")
    }

    var inReadThread: Bool { (node?.inReadThread ?? false) || isReadReply }
    var readReplyAbove: Bool { (node?.readReplyAbove ?? false) || isReadReply }

    /// The topmost visible ancestor, else the node itself.
    var threadRootIsSystemPrompt: Bool {
        guard let node else { return false }
        return node.ancestors.first?.systemPrompt.isSystemPrompt ?? node.systemPrompt.isSystemPrompt
    }
    var threadRootId: Int? { node?.ancestors.first?.id ?? node?.id }
    var threadRootIsPublic: Bool {
        guard let node else { return false }
        if let root = node.ancestors.first { return root.privacyLevel == .public }
        return node.privacyLevel == .public
    }

    var showProposal: Bool {
        guard let node, !node.content.isEmpty, !isLLMPending else { return false }
        if isLLMNode { return ProposalParser.hasProposalSections(node.content) }
        return isOwner && (me?.shareV1Enabled ?? false) && ProposalParser.hasShareBlocks(node.content)
    }

    func showCraftBar(craftMode: Bool, autoGenerate: Bool) -> Bool {
        guard let node else { return false }
        return isOwner && (craftMode || isPublicThread) && !autoGenerateActive(autoGenerate)
            && node.aiUsage != .off && !isLLMPending
    }

    var tabTitle: String {
        guard let node else { return "Loore" }
        if isLLMPending { return isBatchWait ? "Processing…" : "Thinking…" }
        let first = (node.content.jsTrimmed.jsLines.first ?? "")
        let line = JSRegex.replaceFirst(first, #"^[#>\s]+"#, "").jsPrefix(120)
        return line.isEmpty ? "Loore" : line
    }

    // MARK: Loading

    func start() async {
        await load()
        handleAwaitLLM()
    }

    func load() async {
        guard let app else { return }
        loading = node == nil
        do {
            let fetched: NodeDetail = try await app.api.get(APIPath.node(nodeId))
            node = fetched
            pageError = nil
            loading = false
            refreshQuotesIfNeeded()
            resumePendingIfNeeded()
        } catch let error as APIError {
            loading = false
            if error.status == 404 || error.status == 403 {
                pageError = "This node doesn't exist, was deleted, or isn't shared with you."
            } else if !error.isCancelled {
                pageError = "Error fetching node details."
            }
        } catch {
            loading = false
            pageError = "Error fetching node details."
        }
    }

    /// Refetches the focal node (tree included) and its quotes.
    func reload() async {
        await load()
        quotesVersion += 1
        refreshQuotesIfNeeded(force: true)
    }

    private func refreshQuotesIfNeeded(force: Bool = false) {
        guard let app, let node else { return }
        let key = ContentSegmenter.quoteMarkerKey(node.content)
        guard !key.isEmpty else {
            quotes = .none
            quotesKey = ""
            return
        }
        guard force || key != quotesKey else { return }
        quotesKey = key
        Task {
            do {
                let resolved: ResolvedQuotes = try await app.api.get(APIPath.resolveQuotes(nodeId))
                quotes = QuoteData(loaded: true,
                                   quotes: resolved.hasQuotes ? resolved.quotes.values : [:],
                                   external: resolved.hasQuotes ? resolved.externalQuotes.values : [:])
            } catch {
                // As on the web: quotes just do not render (here: shown as inaccessible).
                quotes = QuoteData(loaded: true)
            }
        }
    }

    func updateExternalRead(_ itemId: Int, readAt: Date?) {
        guard let item = quotes.external[itemId] ?? nil else { return }
        var copy = item
        copy.readAt = readAt
        quotes.external[itemId] = copy
    }

    func updateExternalFeedback(_ itemId: Int, feedback: String?) {
        guard let item = quotes.external[itemId] ?? nil else { return }
        var copy = item
        copy.feedback = feedback
        copy.feedbackShared = false
        quotes.external[itemId] = copy
    }

    // MARK: Awaited replies (`?awaitLlm=`)

    /// An entry that arrives with its reply pending goes on to the reply at once
    /// (#367); the reply's own page watches it.
    private func handleAwaitLLM() {
        guard !awaitHandled, let awaitLLM = initialAwaitLLM, let app else { return }
        awaitHandled = true
        if awaitLLM == nodeId {
            track(awaitLLM)
        } else {
            app.router.replaceLast(.thread(id: nodeId, awaitLLM: awaitLLM), with: .thread(id: nodeId, awaitLLM: nil))
            app.open(.thread(id: awaitLLM, awaitLLM: awaitLLM))
        }
    }

    /// Opening a pending reply directly resumes watching it.
    private func resumePendingIfNeeded() {
        guard llmTaskNodeId == nil, isLLMPending, node?.id == nodeId else {
            startStreamIfNeeded()
            return
        }
        track(nodeId)
    }

    private func track(_ id: Int) {
        llmTaskNodeId = id
        llmPollStatus = nil
        startPolling(id)
        startStreamIfNeeded()
    }

    // MARK: Polling and streaming (map D §5.4)

    private func startPolling(_ id: Int) {
        pollTask?.cancel()
        guard let app else { return }
        pollTask = Task { [weak self] in
            var batch = self?.isBatchWait ?? false
            while !Task.isCancelled {
                let options = Poller.Options(interval: batch ? 15 : 2, maxDuration: batch ? 25 * 3600 : 30 * 60)
                var switchedToBatch = false
                let outcome = await Poller.run(options: options,
                    fetch: { try await app.api.get(APIPath.llmStatus(id), poll: true, as: LLMStatus.self) },
                    isTerminal: { status in
                        if status.status?.isTerminal == true { return true }
                        if status.stage == "batch" && !batch { switchedToBatch = true; return true }
                        return false
                    },
                    onUpdate: { [weak self] status in self?.llmPollStatus = status.status })
                guard let self, !Task.isCancelled else { return }
                if case .finished(let status) = outcome {
                    if switchedToBatch && status.status?.isTerminal != true {
                        if id != self.nodeId {
                            // Hand the batch wait to the pending node itself.
                            self.llmTaskNodeId = nil
                            app.open(.thread(id: id, awaitLLM: nil))
                            return
                        }
                        batch = true
                        continue
                    }
                    self.finish(status, trackedId: id)
                }
                return
            }
        }
    }

    private func startStreamIfNeeded() {
        guard let app, let node, streamTask == nil else { return }
        let streaming = (node.nodeType == .llm || node.llmModel != nil) && (node.llmTaskStatus?.isInFlight ?? false)
            && !isBatchWait
        guard streaming else { return }
        streamText = node.streamingContent ?? ""
        let id = node.id
        streamTask = Task { [weak self] in
            let stream = app.sse.subscribe(path: APIPath.sseLLMStream(id))
            do {
                for try await message in stream {
                    guard let self, !Task.isCancelled else { return }
                    guard case .event(let event) = message else { continue }
                    switch LLMStreamEvent(event) {
                    case .snapshot(let text): self.streamText = text
                    case .delta(let text): self.streamText += text
                    case .done, .close, .error: return
                    default: break
                    }
                }
            } catch {
                // Polling stays the source of truth.
            }
        }
    }

    /// The screen went away (another node pushed over it, or popped). A pending
    /// reply on this screen is picked up again by the reload on return.
    func stop() {
        pollTask?.cancel()
        pollTask = nil
        streamTask?.cancel()
        streamTask = nil
        llmTaskNodeId = nil
    }

    /// Completion, failure or cancellation of the tracked reply (map D §5.5–5.6).
    private func finish(_ data: LLMStatus, trackedId: Int) {
        guard let app else { return }
        switch data.status {
        case .completed?:
            let completedId = data.node?.id ?? trackedId
            if let cont = data.continuationNodeId {
                llmTaskNodeId = nil
                app.open(.thread(id: cont, awaitLLM: cont))
                return
            }
            if completedId == nodeId {
                node?.content = data.content ?? node?.content ?? ""
                if let meta = data.toolCallsMeta { node?.toolCallsMeta = meta }
                if let picks = data.feedPicksCount { node?.feedPicksCount = picks }
                node?.llmTaskStatus = .completed
                streamTask?.cancel()
                streamTask = nil
                refreshQuotesIfNeeded()
            } else {
                app.open(.thread(id: completedId, awaitLLM: nil))
            }
            llmTaskNodeId = nil
        case .cancelled?:
            app.toasts.show(data.error ?? "Read cancelled", duration: 8)
            if trackedId == nodeId {
                if let content = data.content { node?.content = content }
                if let meta = data.toolCallsMeta { node?.toolCallsMeta = meta }
                node?.llmTaskStatus = .cancelled
            }
            llmTaskNodeId = nil
        case .failed?:
            app.toasts.show(data.error ?? "Task failed", duration: 8)
            if trackedId == nodeId, let parent = parentAncestor, !parent.deleted {
                // Back to the entry: pop when we came from it, else replace this dead node.
                if case .thread(let prevId, _)? = app.router.previousRoute, prevId == parent.id {
                    app.router.pop()
                } else {
                    app.router.replaceTop(with: .thread(id: parent.id, awaitLLM: nil))
                }
            }
            llmTaskNodeId = nil
        default:
            llmTaskNodeId = nil
        }
    }

    // MARK: Requesting replies (map D §5.2, §5.7)

    func requestLLM(for parentId: Int) async throws -> Int {
        guard let app else { throw CancellationError() }
        let answer: LLMRequestResponse = try await app.api.post(APIPath.nodeLLM(parentId), json: .object([
            "model": .optional(selectedModel), "source_mode": .string("textmode"),
        ]))
        return answer.nodeId
    }

    func handleLLMRequestError(_ error: Error) {
        guard let app else { return }
        if SpendCap.isSpendCapError(error) { return }
        let message = (error as? APIError)?.userMessage(fallback: "Error requesting LLM response.")
            ?? "Error requesting LLM response."
        app.toasts.show(message, duration: 8)
    }

    /// The craft bar's LLM Response: the reply is watched on its own page (#367).
    func llmResponsePressed() {
        guard let app, !llmRequesting else { return }
        llmRequesting = true
        Task {
            defer { llmRequesting = false }
            do {
                let newId = try await requestLLM(for: nodeId)
                app.open(.thread(id: newId, awaitLLM: newId))
            } catch {
                handleLLMRequestError(error)
            }
        }
    }

    /// Fires a reply when auto-generate is on and the whole chain allows AI;
    /// otherwise turns auto-generate off with the web's toast.
    func tryAutoGenerate(for parentId: Int, chainAIUsages: [AIUsage?], autoGenerate: Binding<Bool>) async throws -> Int? {
        guard autoGenerateActive(autoGenerate.wrappedValue) else { return nil }
        guard chainAIUsages.allSatisfy({ $0?.allowsAI == true }) else {
            autoGenerate.wrappedValue = false
            app?.toasts.show("Turning off auto-generate. AI usage on some nodes is turned off.", duration: 8)
            return nil
        }
        return try await requestLLM(for: parentId)
    }

    var chainAIUsages: [AIUsage?] {
        guard let node else { return [] }
        return [node.aiUsage] + node.ancestors.map(\.aiUsage)
    }

    /// After the inline form's send (web `handleInlineSuccess`).
    func inlineReplySent(_ result: NodeFormResult, autoGenerate: Binding<Bool>) async {
        guard let app, let newId = result.id else { return }
        do {
            if let llmId = try await tryAutoGenerate(for: newId, chainAIUsages: chainAIUsages, autoGenerate: autoGenerate) {
                app.open(.thread(id: newId, awaitLLM: llmId))
            } else {
                app.open(.thread(id: newId, awaitLLM: nil))
            }
        } catch {
            app.open(.thread(id: newId, awaitLLM: nil))
            handleLLMRequestError(error)
        }
    }

    /// A reply typed under a read reply is a Text-mode message (#323).
    func submitReadReply(_ submission: NodeFormSubmission, autoGenerate: Bool) async throws -> NodeFormResult {
        guard let app else { throw CancellationError() }
        let auto = autoGenerateActive(autoGenerate)
        if let sid = submission.streamingSessionId {
            let answer: SaveAsNodeResponse = try await app.api.post(APIPath.streamingSaveAsNode(sid), json: .object([
                "content": .string(submission.content), "agentic": .bool(true), "auto_generate": .bool(auto),
                "model": .optional(selectedModel),
            ]))
            return NodeFormResult(id: answer.id, userNodeId: answer.userNodeId, llmNodeId: answer.llmNodeId,
                                  spendCapped: answer.spendCapped, llmError: answer.llmError)
        }
        if submission.aiUsage == .off {
            let created: NodeCreateResponse = try await app.api.post(APIPath.nodes, json: .object([
                "content": .string(submission.content), "parent_id": .int(nodeId),
                "privacy_level": .string(submission.privacy.rawString), "ai_usage": .string(submission.aiUsage.rawString),
            ]))
            return NodeFormResult(id: created.id)
        }
        let answer: TextmodeStartResponse = try await app.api.post(APIPath.textmodeFromNode(nodeId), json: .object([
            "content": .string(submission.content), "ai_usage": .string(submission.aiUsage.rawString),
            "model": .optional(selectedModel), "auto_generate": .bool(auto),
        ]))
        return NodeFormResult(id: answer.userNodeId, userNodeId: answer.userNodeId, llmNodeId: answer.llmNodeId,
                              promptNodeId: answer.promptNodeId, spendCapped: answer.spendCapped, llmError: answer.llmError)
    }

    func readReplySent(_ result: NodeFormResult) {
        guard let app, let userId = result.userNodeId ?? result.id else { return }
        app.open(.thread(id: userId, awaitLLM: result.llmNodeId))
    }

    // MARK: Read (admin Community Archive feature, map D §5.9)

    static let readFurtherTitle = "Another pass over the day's tweets, against everything in this thread so far — your marks on these picks included."
    static let readEntryTitle = "Loore reads the last day of Community Archive tweets and shows you the ones relevant to this thread"

    var readLabel: String { readReplyAbove ? "Read further" : "Read" }

    func readActions(craftMode: Bool) -> Bool {
        isOwner && inReadThread && node?.aiUsage != .off && !isLLMPending
    }

    /// `POST /api/read/from-node/<id>` (billed): a read turn under this node.
    func readFromNode(autoGenerate: Bool) {
        guard let app, !readLoading else { return }
        readLoading = true
        pageError = nil
        Task {
            do {
                struct Answer: Decodable { var llm_node_id: Int?; var prompt_node_id: Int? }
                var body: [String: JSONValue] = ["auto_generate": .bool(autoGenerateActive(autoGenerate))]
                if let readModel { body["model"] = .string(readModel) }
                let answer: Answer = try await app.api.post(APIPath.readFromNode(nodeId), json: .object(body))
                readLoading = false
                if let id = answer.llm_node_id ?? answer.prompt_node_id { app.open(.thread(id: id, awaitLLM: nil)) }
            } catch {
                readLoading = false
                if SpendCap.isSpendCapError(error) { return }
                app.toasts.show((error as? APIError)?.userMessage(fallback: "Could not start the read.")
                                ?? "Could not start the read.", duration: 6)
            }
        }
    }

    // MARK: Voice hand-off

    func startVoice() {
        guard let app, !voiceLoading else { return }
        voiceLoading = true
        pageError = nil
        Task {
            do {
                let answer: VoiceFromNodeResponse = try await app.api.post(APIPath.voiceFromNode(nodeId), json: .object([
                    "model": .optional(selectedModel),
                ]))
                if answer.mode == "processing" {
                    app.open(.voice(parentId: answer.parentId, resumeLLMId: answer.llmNodeId))
                } else {
                    app.open(.voice(parentId: answer.parentId, resumeLLMId: nil))
                }
                voiceLoading = false
            } catch {
                voiceLoading = false
                if SpendCap.isSpendCapError(error) { return }
                pageError = (error as? APIError)?.userMessage(fallback: "Error starting voice session.")
                    ?? "Error starting voice session."
            }
        }
    }

    // MARK: Pin

    var canPin: Bool { isOwner && node?.privacyLevel != .private }

    var pinTitle: String {
        if !isOwner { return "Only the owner can pin" }
        if node?.privacyLevel == .private { return "Cannot pin a private node" }
        return node?.pinnedAt != nil ? "Unpin from your public page" : "Pin to the top of your public page"
    }

    func togglePin() {
        guard let app, let node, !pinLoading, canPin else { return }
        pinLoading = true
        Task {
            defer { pinLoading = false }
            do {
                if node.pinnedAt != nil {
                    _ = try await app.api.delete(APIPath.pin(nodeId), as: EmptyResponse.self)
                    self.node?.pinnedAt = nil
                } else {
                    let answer: PinResponse = try await app.api.post(APIPath.pin(nodeId))
                    self.node?.pinnedAt = answer.pinnedAt ?? Date()
                }
            } catch {
                // The web replaces the page with the error (a quirk kept for parity).
                pageError = (error as? APIError)?.userMessage(fallback: "Error toggling pin.") ?? "Error toggling pin."
            }
        }
    }

    // MARK: Checklists and proposal edits on the focal node

    func applyContentEdit(_ newContent: String, failure: @escaping (String) -> String) {
        guard let app, let old = node?.content, newContent != old else { return }
        node?.content = newContent
        Task {
            do {
                let _: NodeUpdateResponse = try await app.api.put(APIPath.node(nodeId), json: .object([
                    "content": .string(newContent),
                ]))
            } catch {
                node?.content = old
                let reason = (error as? APIError)?.serverMessage ?? (error as? APIError)?.errorDescription
                    ?? error.localizedDescription
                app.toasts.show(failure(reason))
            }
        }
    }

    func toggleCheckbox(_ itemText: String, checked: Bool) {
        guard let content = node?.content, !content.isEmpty else { return }
        applyContentEdit(MarkdownEdits.toggleCheckbox(content, itemText: itemText, currentChecked: checked)) {
            "Couldn't save change — reverted (\($0))"
        }
    }

    func insertTask(after itemText: String, _ newText: String) {
        guard let content = node?.content else { return }
        applyContentEdit(MarkdownEdits.insertItemAfter(content, afterItemText: itemText, newText: newText)) {
            "Couldn't add task — reverted (\($0))"
        }
    }

    /// The proposal card changed the content (tick / "+"), already saved or reverted by it.
    func setContent(_ content: String) {
        node?.content = content
    }

    func updateToolMeta(_ name: String, _ updates: [String: JSONValue]) {
        guard var meta = node?.toolCallsMeta else { return }
        for i in meta.indices where meta[i].name == name {
            meta[i].raw.merge(updates) { _, new in new }
        }
        node?.toolCallsMeta = meta
    }

    // MARK: Kebab targets

    func target(focal node: NodeDetail) -> NodeTarget {
        NodeTarget(id: node.id, content: node.content, privacy: node.privacyLevel, aiUsage: node.aiUsage,
                   childCount: node.childCount, hasChildren: node.childCount > 0 || !node.children.isEmpty,
                   hasTTS: node.hasTTS, hasPromptArtifact: node.systemPrompt.contextArtifacts?.prompt != nil)
    }

    func target(_ n: AncestorNode) -> NodeTarget {
        NodeTarget(id: n.id, content: n.content ?? "", privacy: n.privacyLevel, aiUsage: n.aiUsage,
                   childCount: n.childCount, hasChildren: n.childCount > 0, hasTTS: false,
                   hasPromptArtifact: n.systemPrompt.contextArtifacts?.prompt != nil)
    }

    func target(_ n: TreeNode) -> NodeTarget {
        NodeTarget(id: n.id, content: n.content ?? "", privacy: n.privacyLevel, aiUsage: n.aiUsage,
                   childCount: n.childCount, hasChildren: n.childCount > 0 || !n.children.isEmpty, hasTTS: n.hasTTS,
                   hasPromptArtifact: n.systemPrompt.contextArtifacts?.prompt != nil)
    }

    func beginEdit(_ target: NodeTarget) {
        editTarget = target
        replyTarget = nil
        deleteTarget = nil
        if target.hasPromptArtifact {
            showPromptEditConfirm = true
        } else {
            showEditForm = true
        }
    }

    func beginReply(_ target: NodeTarget) {
        replyTarget = target
        editTarget = nil
        deleteTarget = nil
    }

    func beginDelete(_ target: NodeTarget) {
        deleteTarget = target
        replyTarget = nil
        editTarget = nil
    }

    // MARK: Edit (map D §4.7)

    func editSaved(_ result: NodeFormResult, autoGenerate: Binding<Bool>) async {
        guard let app, let node else { return }
        let wasFocal = editTarget?.id == node.id
        showEditForm = false
        editTarget = nil
        if result.descendantsUpdated > 0 {
            let n = result.descendantsUpdated
            app.toasts.show("Applied to \(n) repl\(n == 1 ? "y" : "ies") too")
        }
        if !wasFocal {
            await reload()
            return
        }
        if result.descendantsUpdated > 0 {
            await load()
        } else if let edited = result.editedNode {
            self.node?.content = edited.content
            self.node?.privacyLevel = edited.privacyLevel
            self.node?.aiUsage = edited.aiUsage
            self.node?.updatedAt = edited.updatedAt
            self.node?.pinnedAt = edited.pinnedAt
            self.node?.hasTTS = edited.hasTTS
            self.node?.permalink = edited.permalink
            self.node?.systemPrompt = edited.systemPrompt
        }
        refreshQuotesIfNeeded()
        guard let updated = self.node, !(updated.nodeType == .llm || updated.llmModel != nil) else { return }
        do {
            if let llmId = try await tryAutoGenerate(for: updated.id, chainAIUsages: chainAIUsages,
                                                     autoGenerate: autoGenerate) {
                app.open(.thread(id: llmId, awaitLLM: llmId))
            }
        } catch {
            handleLLMRequestError(error)
        }
    }

    // MARK: Delete (map D §4.8)

    /// The dialog stays up while the orphaned-prompt check runs, so the
    /// follow-up replaces it in place (one presenter for both).
    func confirmDelete(withDescendants: Bool) {
        guard let app, let target = deleteTarget, !deleteChecking else { return }
        guard threadRootIsSystemPrompt, target.id != threadRootId else {
            deleteTarget = nil
            performDelete(target.id, withDescendants: withDescendants, includePrompt: false)
            return
        }
        deleteChecking = true
        Task {
            let impact: DeleteImpactResponse? = try? await app.api.get(APIPath.deleteImpact(target.id), query: [
                URLQueryItem(name: "delete_descendants", value: withDescendants ? "true" : "false"),
            ])
            deleteChecking = false
            if impact?.orphanedSystemPromptId != nil {
                pendingPromptDelete = (target.id, withDescendants)
                showPromptDeleteDialog = true
                deleteTarget = nil
            } else {
                deleteTarget = nil
                performDelete(target.id, withDescendants: withDescendants, includePrompt: false)
            }
        }
    }

    func confirmPromptDelete(includePrompt: Bool) {
        showPromptDeleteDialog = false
        guard let pending = pendingPromptDelete else { return }
        pendingPromptDelete = nil
        performDelete(pending.targetId, withDescendants: pending.withDescendants, includePrompt: includePrompt)
    }

    private func performDelete(_ targetId: Int, withDescendants: Bool, includePrompt: Bool) {
        guard let app, let node else { return }
        let wasFocal = targetId == node.id
        Task {
            do {
                let answer: NodeDeleteResponse = try await app.api.delete(APIPath.node(targetId), query: [
                    URLQueryItem(name: "delete_descendants", value: withDescendants ? "true" : "false"),
                    URLQueryItem(name: "delete_orphaned_prompt", value: includePrompt ? "true" : "false"),
                ])
                let n = answer.scheduled ?? 1
                app.toasts.show("Deleted \(n) node\(n == 1 ? "" : "s")")
                app.signals.post(.logChanged)
                @MainActor func leaveThread(isPublic: Bool) {
                    if isPublic && (app.user?.shareV1Enabled ?? false) {
                        app.router.popToRoot()
                        app.open(.commons)
                    } else {
                        app.router.popToRoot()
                        app.open(.log)
                    }
                }
                if answer.orphanedPromptDeleted != nil && answer.orphanedPromptDeleted != 0 {
                    leaveThread(isPublic: threadRootIsPublic)
                    return
                }
                let ancestorIdx = !wasFocal && withDescendants ? node.ancestors.firstIndex { $0.id == targetId } : nil
                if !wasFocal && ancestorIdx == nil {
                    await reload()
                    return
                }
                let upper = ancestorIdx ?? node.ancestors.count
                if let alive = node.ancestors.prefix(upper).last(where: { !$0.deleted }) {
                    app.router.replaceTop(with: .thread(id: alive.id, awaitLLM: nil))
                    return
                }
                leaveThread(isPublic: node.privacyLevel == .public)
            } catch {
                pageError = "Error deleting node."
            }
        }
    }
}
