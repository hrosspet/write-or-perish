import Foundation
import Observation

/// What the host passes to a writing form (web `NodeForm` props, map D §4.1).
struct NodeFormConfig {
    struct Edit {
        var nodeId: Int
        var initialContent: String
        var initialPrivacy: PrivacyLevel?
        var initialAIUsage: AIUsage?
        /// The node has a pinned prompt: a text change detaches it.
        var detachPrompt = false
        var hasGeneratedTTS = false
        var hasChildren = false
    }

    var parentId: Int?
    var edit: Edit?
    var hidePowerFeatures = false
    var hideAudioUpload = false
    var compact = false
    var placeholder: String?
    /// The Write New Entry modal: Agentic Reply and Auto-generate toggles.
    var allowAgenticPrompt = false
    /// Text mode: AI usage always starts from the account default, not the last choice.
    var aiUsageFromGlobalDefault = false
    /// A page-specific submit (Text mode, read replies). Skips `POST /nodes/`.
    var submitOverride: ((NodeFormSubmission) async throws -> NodeFormResult)?

    var isEdit: Bool { edit != nil }
}

/// What a custom submit receives.
struct NodeFormSubmission {
    var content: String
    var parentId: Int?
    var privacy: PrivacyLevel
    var aiUsage: AIUsage
    var streamingSessionId: String?
}

/// What the form hands to its host after a successful send (the web's `onSuccess(data)`).
struct NodeFormResult {
    /// The node to open (for an audio upload: the transcribed node).
    var id: Int?
    /// A pending reply to watch (`?awaitLlm=`).
    var awaitLLM: Int?
    var userNodeId: Int?
    var llmNodeId: Int?
    var promptNodeId: Int?
    var tipId: Int?
    /// Edit: the node's own fields after the save.
    var editedNode: NodeDetail?
    var descendantsUpdated = 0
    var spendCapped = false
    var llmError: String?
}

/// An audio file picked for upload (craft mode).
struct PickedAudioFile: Equatable {
    var url: URL
    var name: String
    var size: Int

    static let maxBytes = 200 * 1024 * 1024
    static let chunkedAbove = 10 * 1024 * 1024
    static let allowedExtensions = [".webm", ".wav", ".m4a", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".oga", ".flac", ".aac"]
}

/// The writing form's state and its submit decision tree (port of
/// `NodeForm.handleSubmit`, map D §4.5). The view is `NodeFormView`.
@MainActor
@Observable
final class NodeFormModel {
    let config: NodeFormConfig
    @ObservationIgnored private let app: AppState
    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored var onSuccess: (NodeFormResult) -> Void = { _ in }

    var content: String
    var error: String?
    var loading = false
    var hasDraft = false
    var privacy: PrivacyLevel {
        didSet { if remembersChoices { defaults.set(privacy.rawString, forKey: DefaultsKey.lastPrivacyLevel) } }
    }
    var aiUsage: AIUsage {
        didSet {
            if remembersChoices && !config.aiUsageFromGlobalDefault {
                defaults.set(aiUsage.rawString, forKey: DefaultsKey.lastAIUsage)
            }
        }
    }
    /// The parent's privacy (replying): a public parent makes the reply public.
    private(set) var parentPrivacy: PrivacyLevel?
    var useAgenticPrompt: Bool {
        didSet { if config.allowAgenticPrompt { defaults.set(useAgenticPrompt, forKey: DefaultsKey.agenticReply) } }
    }
    var useAutoGenerate: Bool {
        didSet { if config.allowAgenticPrompt { defaults.set(useAutoGenerate, forKey: DefaultsKey.autoGenerate) } }
    }

    // Audio upload (craft) and recorded transcripts (M3 dictation).
    var uploadedFile: PickedAudioFile?
    private(set) var isUploading = false
    private(set) var uploadProgress = 0
    private(set) var transcriptionStatus: TaskStatus?
    private(set) var transcriptionProgress = 0
    /// Set when a recording's transcript is in the form (saved through `save-as-node`).
    var streamingSessionId: String?
    /// True while the dictation recorder runs (M3).
    var isRecording = false
    private(set) var isRecoveringAudio = false

    // Dialog chain (TTS → apply-to-replies → split → public reply). One
    // presenter shows whichever is current, so a chain swaps the card in place
    // (two full-screen covers cannot dismiss and present at once).
    enum Dialog: Equatable { case tts, scope, split, publicReply }
    var dialog: Dialog?
    var showTtsDialog: Bool { dialog == .tts }
    var showScopeDialog: Bool { dialog == .scope }
    var showSplitDialog: Bool { dialog == .split }
    var showPublicReplyDialog: Bool { dialog == .publicReply }
    private(set) var pendingPaste: String?
    @ObservationIgnored private var splitAcknowledged = false
    @ObservationIgnored private var pendingRegenerateTts: Bool?
    /// The submit a dialog answer started; tests await it.
    @ObservationIgnored private(set) var dialogSubmit: Task<Void, Never>?

    let drafts: DraftAutosaver
    @ObservationIgnored private var started = false

    init(config: NodeFormConfig, app: AppState, defaults: UserDefaults = .standard) {
        self.config = config
        self.app = app
        self.defaults = defaults
        drafts = DraftAutosaver(api: app.api, nodeId: config.edit?.nodeId,
                                parentId: config.isEdit ? nil : config.parentId)
        content = config.edit?.initialContent ?? ""
        let user = app.user
        let remembers = !config.isEdit && config.parentId == nil
        func remembered(_ key: String) -> String? { remembers ? defaults.string(forKey: key) : nil }
        privacy = config.edit?.initialPrivacy
            ?? remembered(DefaultsKey.lastPrivacyLevel).map(PrivacyLevel.init(rawString:))
            ?? user?.defaultPrivacyLevel ?? .private
        if let initial = config.edit?.initialAIUsage {
            aiUsage = initial
        } else if config.aiUsageFromGlobalDefault {
            aiUsage = user?.defaultAIUsage ?? .off
        } else {
            aiUsage = remembered(DefaultsKey.lastAIUsage).map(AIUsage.init(rawString:)) ?? user?.defaultAIUsage ?? .off
        }
        func persisted(_ key: String) -> Bool {
            guard config.allowAgenticPrompt else { return false }
            return defaults.object(forKey: key) == nil ? true : defaults.bool(forKey: key)
        }
        useAgenticPrompt = persisted(DefaultsKey.agenticReply)
        useAutoGenerate = persisted(DefaultsKey.autoGenerate)
    }

    /// Fresh top-level entries remember the last privacy / AI usage (#63).
    private var remembersChoices: Bool {
        !config.isEdit && config.parentId == nil && config.edit?.initialPrivacy == nil
    }

    var isOnline: Bool { NetworkStatus.shared.isOnline }

    /// Loads the draft and, for replies, the parent's settings. Call once.
    func start() async {
        guard !started else { return }
        started = true
        async let draftLoad: Void = drafts.load()
        if let parentId = config.parentId, !config.isEdit {
            if let parent = try? await app.api.nodeDetail(parentId) {
                parentPrivacy = parent.privacyLevel
                privacy = parent.privacyLevel
                aiUsage = parent.replyAIUsage ?? parent.aiUsage
            }
        }
        await draftLoad
        if let draft = drafts.draft, !draft.content.isEmpty {
            content = draft.content
            hasDraft = true
        }
        if let draft = drafts.draft, let sid = draft.sessionId, draft.hasStoredChunks {
            await recoverAudio(sessionId: sid)
        }
    }

    /// The form left the screen: save the pending draft now (the debounce and the
    /// retry interval stop until the form is back).
    func formDisappeared() {
        Task { await drafts.suspend() }
    }

    func formAppeared() {
        drafts.resume()
    }

    /// Replying under a public node: the reply is public, whatever was remembered.
    var effectivePrivacy: PrivacyLevel {
        !config.isEdit && parentPrivacy == .public ? .public : privacy
    }

    var lockPrivacy: Bool { !config.isEdit && parentPrivacy == .public }

    var showsAgenticToggles: Bool {
        config.allowAgenticPrompt && !config.isEdit && config.parentId == nil && !config.hidePowerFeatures
            && aiUsage != .off
    }

    var canSubmit: Bool {
        (!content.jsTrimmed.isEmpty || (!config.isEdit && uploadedFile != nil)) && !loading && isOnline && !isRecording
    }

    /// Why Record / Upload are unavailable (the web's tooltip, #62).
    var audioDisabledReason: String? {
        if aiUsage == .off { return "Set AI Usage to Chat or Train to record or upload audio — transcription needs AI access." }
        if !isOnline { return "You're offline — reconnect to record or upload audio." }
        if isRecording { return "Finish the current recording first." }
        return nil
    }

    var sendLabel: String {
        if isUploading { return "Uploading... \(uploadProgress)%" }
        if loading && transcriptionStatus == .processing && transcriptionProgress > 0 {
            return "Transcribing... \(transcriptionProgress)%"
        }
        if loading && transcriptionStatus == .pending { return "Waiting to transcribe..." }
        if loading { return "Sending..." }
        return "Send"
    }

    // MARK: Typing

    /// The text changed (typing or paste). Mirrors the web's onChange/onPaste.
    func contentChanged(from old: String, to new: String) {
        // A paste that pushes the entry over the cap is intercepted (the web's
        // onPaste); typing one character past it is left to the submit check.
        if new.jsLength > nodeCharCap && new.jsLength - old.jsLength > 1 {
            content = old
            if config.isEdit || config.submitOverride != nil {
                error = "Pasting this would make the entry \(jsLocaleNumber(new.jsLength)) characters — above the \(jsLocaleNumber(nodeCharCap))-character limit."
                return
            }
            pendingPaste = new
            dialog = .split
            return
        }
        if !new.jsTrimmed.isEmpty {
            drafts.save(new)
            hasDraft = true
        }
        if !config.isEdit && uploadedFile != nil { uploadedFile = nil }
    }

    func discardDraft() {
        Task { await drafts.delete() }
        hasDraft = false
        content = config.edit?.initialContent ?? ""
    }

    // MARK: Audio file

    func pickFile(_ url: URL) {
        let name = url.lastPathComponent
        let size = Self.pickedFileSize(url)
        if size > PickedAudioFile.maxBytes {
            error = "File size must be under 200 MB"
            return
        }
        let ext = "." + (name.split(separator: ".").last.map { String($0).lowercased() } ?? "")
        if !PickedAudioFile.allowedExtensions.contains(ext) {
            error = "Invalid file type. Please upload an audio file (mp3, wav, m4a, webm, ogg, flac, aac)"
            return
        }
        error = nil
        uploadedFile = PickedAudioFile(url: url, name: name, size: size)
        content = ""
    }

    /// A picked file's size. A file outside the app's sandbox (Files, iCloud Drive)
    /// can be read only while its security-scoped access is open; read before
    /// that, the size came back as 0, which skipped the 200 MB check and the
    /// chunked upload.
    static func pickedFileSize(_ url: URL,
                               startAccess: (URL) -> Bool = { $0.startAccessingSecurityScopedResource() },
                               stopAccess: (URL) -> Void = { $0.stopAccessingSecurityScopedResource() },
                               readSize: (URL) -> Int? = { try? $0.resourceValues(forKeys: [.fileSizeKey]).fileSize }) -> Int {
        let access = startAccess(url)
        defer { if access { stopAccess(url) } }
        return readSize(url) ?? 0
    }

    /// The Upload press: refused up front when the monthly cap is reached (#341).
    func uploadPressed() -> Bool {
        if app.spendCapped {
            app.notifySpendBlocked()
            app.toasts.show(SpendCap.toastMessage(.upload), duration: 8)
            return false
        }
        return true
    }

    // MARK: Dialog answers

    func cancelDialog() {
        dialog = nil
    }

    func answerTts(_ regenerate: Bool) {
        dialogSubmit = Task { await submit(regenerateTts: regenerate) }
    }

    func answerScope(_ applyToReplies: Bool) {
        dialogSubmit = Task { await submit(regenerateTts: pendingRegenerateTts, applyToReplies: applyToReplies) }
    }

    func answerPublicReply() {
        dialogSubmit = Task { await submit(publicConfirmed: true) }
    }

    func confirmSplit() {
        splitAcknowledged = true
        if let paste = pendingPaste {
            dialog = nil
            content = paste
            drafts.save(paste)
            hasDraft = true
            pendingPaste = nil
        } else {
            dialogSubmit = Task { await submit(splitConfirmed: true) }
        }
    }

    func cancelSplit() {
        dialog = nil
        pendingPaste = nil
    }

    // MARK: Submit (map D §4.5)

    func submit(regenerateTts: Bool? = nil, splitConfirmed: Bool = false, publicConfirmed: Bool = false,
                applyToReplies: Bool? = nil) async {
        let edit = config.edit
        if !(edit == nil && uploadedFile != nil) && content.jsTrimmed.isEmpty {
            dialog = nil
            error = "Content is required."
            return
        }
        if let edit, edit.hasGeneratedTTS, regenerateTts == nil, content != edit.initialContent {
            dialog = .tts
            return
        }
        if let edit, edit.hasChildren, applyToReplies == nil,
           (edit.initialPrivacy != nil && privacy != edit.initialPrivacy)
            || (edit.initialAIUsage != nil && aiUsage != edit.initialAIUsage) {
            pendingRegenerateTts = regenerateTts
            dialog = .scope
            return
        }
        if content.jsLength > nodeCharCap && uploadedFile == nil {
            if edit != nil || (config.submitOverride != nil && streamingSessionId == nil) {
                dialog = nil
                error = "This entry is \(jsLocaleNumber(content.jsLength)) characters — above the \(jsLocaleNumber(nodeCharCap))-character limit. Please move part of it into separate entries."
                return
            }
            if !splitAcknowledged && !splitConfirmed {
                dialog = .split
                return
            }
        }
        if edit == nil, parentPrivacy == .public, !publicConfirmed,
           defaults.string(forKey: DefaultsKey.publicReplyAck) != "true" {
            dialog = .publicReply
            return
        }

        dialog = nil
        loading = true
        error = nil
        do {
            if let result = try await send(regenerateTts: regenerateTts, applyToReplies: applyToReplies) {
                loading = false
                onSuccess(result)
            }
        } catch {
            loading = false
            isUploading = false
            uploadProgress = 0
            if uploadedFile != nil, SpendCap.isSpendCapError(error) {
                app.toasts.show(SpendCap.toastMessage(.upload), duration: 8)
            } else if let apiError = error as? APIError {
                self.error = apiError.userMessage(fallback: "Error submitting form.")
            } else {
                self.error = error.localizedDescription.isEmpty ? "Error submitting form." : error.localizedDescription
            }
        }
    }

    /// Returns the result to hand to the host, or nil when the form keeps
    /// waiting (an upload being transcribed reports through `onSuccess` itself).
    private func send(regenerateTts: Bool?, applyToReplies: Bool?) async throws -> NodeFormResult? {
        let api = app.api
        let privacyLevel = effectivePrivacy

        // Custom submit (Text mode, read replies).
        if let override = config.submitOverride, !config.isEdit, uploadedFile == nil {
            let result = try await override(NodeFormSubmission(content: content, parentId: config.parentId,
                                                               privacy: privacyLevel, aiUsage: aiUsage,
                                                               streamingSessionId: streamingSessionId))
            await clearAfterSend()
            streamingSessionId = nil
            if result.spendCapped { app.notifySpendBlocked() }
            if let llmError = result.llmError { app.toasts.show(llmError, duration: 10) }
            return result
        }

        // Agentic top-level entry (Write New Entry with Agentic Reply on).
        if useAgenticPrompt && !config.isEdit && uploadedFile == nil && streamingSessionId == nil
            && config.parentId == nil && aiUsage != .off && content.jsLength <= nodeCharCap {
            let body: JSONValue = .object([
                "content": .string(content), "privacy_level": .string(privacyLevel.rawString),
                "ai_usage": .string(aiUsage.rawString), "auto_generate": .bool(useAutoGenerate),
            ])
            let answer: TextmodeStartResponse = try await api.post(APIPath.textmodeStart, json: body)
            await clearAfterSend()
            if answer.spendCapped { app.notifySpendBlocked() }
            if let llmError = answer.llmError { app.toasts.show(llmError, duration: 10) }
            return NodeFormResult(id: answer.userNodeId, awaitLLM: answer.llmNodeId, userNodeId: answer.userNodeId,
                                  llmNodeId: answer.llmNodeId, spendCapped: answer.spendCapped, llmError: answer.llmError)
        }

        // A recorded transcript: the streaming draft becomes the node.
        if let sid = streamingSessionId {
            let topLevelWithAI = config.parentId == nil && aiUsage != .off
            var body: [String: JSONValue] = ["content": .string(content)]
            if topLevelWithAI && useAgenticPrompt { body["agentic"] = .bool(true) }
            if topLevelWithAI && useAutoGenerate && config.allowAgenticPrompt { body["auto_generate"] = .bool(true) }
            let answer: SaveAsNodeResponse = try await api.post(APIPath.streamingSaveAsNode(sid), json: .object(body))
            await clearAfterSend()
            streamingSessionId = nil
            if answer.spendCapped { app.notifySpendBlocked() }
            return NodeFormResult(id: answer.id, awaitLLM: answer.llmNodeId, userNodeId: answer.userNodeId,
                                  llmNodeId: answer.llmNodeId, tipId: answer.tipId, spendCapped: answer.spendCapped,
                                  llmError: answer.llmError)
        }

        if let edit = config.edit {
            var body: [String: JSONValue] = [
                "content": .string(content), "privacy_level": .string(privacy.rawString),
                "ai_usage": .string(aiUsage.rawString),
            ]
            if edit.detachPrompt && content != edit.initialContent { body["detach_prompt"] = .bool(true) }
            if regenerateTts == true { body["regenerate_tts"] = .bool(true) }
            if applyToReplies == true { body["apply_to_descendants"] = .bool(true) }
            let answer: NodeUpdateResponse = try await api.put(APIPath.node(edit.nodeId), json: .object(body))
            await clearAfterSend()
            return NodeFormResult(id: answer.node.id, editedNode: answer.node,
                                  descendantsUpdated: answer.descendantsUpdated)
        }

        if let file = uploadedFile {
            let nodeId = try await upload(file, privacy: privacyLevel)
            try await awaitTranscription(nodeId: nodeId)
            return nil
        }

        var body: [String: JSONValue] = [
            "content": .string(content), "privacy_level": .string(privacyLevel.rawString),
            "ai_usage": .string(aiUsage.rawString),
        ]
        body["parent_id"] = .optional(config.parentId)
        let created: NodeCreateResponse = try await api.post(APIPath.nodes, json: .object(body))
        await clearAfterSend()
        app.signals.post(.nodeCreated(created.id))

        // Auto-generate without Agentic Reply (Write New Entry only).
        if useAutoGenerate && config.allowAgenticPrompt && config.parentId == nil && aiUsage != .off
            && !useAgenticPrompt {
            if let reply: LLMRequestResponse = try? await api.post(
                APIPath.nodeLLM(created.id), json: .object(["source_mode": .string("textmode")])) {
                return NodeFormResult(id: reply.nodeId, awaitLLM: reply.nodeId, tipId: created.tipId)
            }
        }
        return NodeFormResult(id: created.id, tipId: created.tipId)
    }

    private func clearAfterSend() async {
        await drafts.delete()
        hasDraft = false
    }

    // MARK: Upload and transcription

    private func upload(_ file: PickedAudioFile, privacy: PrivacyLevel) async throws -> Int {
        let access = file.url.startAccessingSecurityScopedResource()
        defer { if access { file.url.stopAccessingSecurityScopedResource() } }
        if file.size > PickedAudioFile.chunkedAbove {
            return try await uploadInChunks(file, privacy: privacy)
        }
        let data = try Data(contentsOf: file.url)
        var form = MultipartFormData()
        form.addFile("audio_file", filename: file.name, mimeType: Self.mimeType(for: file.name), data: data)
        if let parentId = config.parentId { form.addField("parent_id", String(parentId)) }
        form.addField("privacy_level", privacy.rawString)
        form.addField("ai_usage", aiUsage.rawString)
        var request = form.request(.post, APIPath.nodes)
        request.timeout = 300
        let created: NodeCreateResponse = try await app.api.send(request)
        return created.id
    }

    /// Port of `utils/chunkedUpload.js`: 5 MB chunks, 3 tries each (1 s, 2 s backoff).
    private func uploadInChunks(_ file: PickedAudioFile, privacy: PrivacyLevel) async throws -> Int {
        let chunkSize = 5 * 1024 * 1024
        let total = Int((Double(file.size) / Double(chunkSize)).rounded(.up))
        let uploadId = "\(Int(Date().timeIntervalSince1970 * 1000))-\(UUID().uuidString.prefix(9).lowercased())"
        isUploading = true
        uploadProgress = 0
        defer { isUploading = false }
        do {
            var initBody: [String: JSONValue] = [
                "filename": .string(file.name), "filesize": .int(file.size), "total_chunks": .int(total),
                "upload_id": .string(uploadId), "node_type": .string("user"),
                "privacy_level": .string(privacy.rawString), "ai_usage": .string(aiUsage.rawString),
            ]
            initBody["parent_id"] = .optional(config.parentId)
            struct InitAnswer: Decodable { var node_id: Int }
            let started: InitAnswer = try await app.api.post(APIPath.uploadInit, json: .object(initBody))
            let handle = try FileHandle(forReadingFrom: file.url)
            defer { try? handle.close() }
            for index in 0..<total {
                try handle.seek(toOffset: UInt64(index * chunkSize))
                let chunk = handle.readData(ofLength: chunkSize)
                var lastError: Error?
                for attempt in 1...3 {
                    do {
                        var form = MultipartFormData()
                        form.addFile("chunk", filename: "blob", mimeType: "application/octet-stream", data: chunk)
                        form.addField("chunk_index", String(index))
                        form.addField("upload_id", uploadId)
                        form.addField("node_id", String(started.node_id))
                        var request = form.request(.post, APIPath.uploadChunk)
                        request.timeout = 120
                        _ = try await app.api.send(request, as: EmptyResponse.self)
                        lastError = nil
                        break
                    } catch {
                        lastError = error
                        if attempt < 3 { try await Task.sleep(nanoseconds: UInt64(pow(2, Double(attempt - 1))) * 1_000_000_000) }
                    }
                }
                if let lastError { throw lastError }
                uploadProgress = Int((Double(index + 1) / Double(total) * 100).rounded())
            }
            uploadProgress = 100
            let finished: NodeCreateResponse = try await app.api.post(APIPath.uploadFinalize, json: .object([
                "upload_id": .string(uploadId), "node_id": .int(started.node_id),
            ]))
            return finished.id
        } catch {
            app.api.fireAndForget(.json(.post, APIPath.uploadCleanup, .object(["upload_id": .string(uploadId)])))
            throw error
        }
    }

    /// Polls `transcription-status` every 2 s until the node is transcribed.
    private func awaitTranscription(nodeId: Int) async throws {
        transcriptionStatus = .pending
        let outcome = await Poller.runStatus(options: .init(interval: 2),
            fetch: { [app] in try await app.api.get(APIPath.transcriptionStatus(nodeId), poll: true, as: TranscriptionStatus.self) },
            onUpdate: { [weak self] status in
                self?.transcriptionStatus = status.status
                self?.transcriptionProgress = status.progress
            })
        loading = false
        transcriptionStatus = nil
        if case .finished(let status) = outcome, status.status == .completed {
            await clearAfterSend()
            uploadedFile = nil
            onSuccess(NodeFormResult(id: status.nodeId ?? nodeId))
        } else if case .finished(let status) = outcome {
            error = status.error ?? "Transcription failed"
        }
    }

    static func mimeType(for name: String) -> String {
        switch (name.split(separator: ".").last.map { String($0).lowercased() } ?? "") {
        case "mp3", "mpga", "mpeg": return "audio/mpeg"
        case "wav": return "audio/wav"
        case "m4a": return "audio/m4a"
        case "mp4": return "audio/mp4"
        case "webm": return "audio/webm"
        case "ogg", "oga": return "audio/ogg"
        case "flac": return "audio/flac"
        case "aac": return "audio/aac"
        default: return "application/octet-stream"
        }
    }

    // MARK: Recovered audio (a draft with stored, untranscribed chunks)

    private func recoverAudio(sessionId: String) async {
        isRecoveringAudio = true
        streamingSessionId = sessionId
        defer { isRecoveringAudio = false }
        do {
            _ = try await app.api.post(APIPath.streamingTranscribeRemaining(sessionId), as: EmptyResponse.self)
        } catch {
            return
        }
        let outcome = await Poller.run(options: .init(interval: 3, maxDuration: 5 * 60, maxConsecutiveErrors: 1),
            fetch: { [app] in try await app.api.get(APIPath.streamingStatus(sessionId), poll: true,
                                                    as: StreamingSessionStatus.self) },
            isTerminal: { $0.streamingStatus == .completed || $0.streamingStatus == .failed },
            onUpdate: { [weak self] status in
                if !status.content.isEmpty { self?.content = status.content }
            })
        if case .finished(let status) = outcome, !status.content.isEmpty {
            drafts.save(status.content)
            hasDraft = true
        }
    }

    // MARK: Dictation hook (M3)

    /// M3's recorder calls these while dictating into the form (text-mode path:
    /// finalize without the Voice label, then `save-as-node` on Send).
    @ObservationIgnored private var preDictationContent = ""

    func dictationStarted() {
        preDictationContent = content
        isRecording = true
    }

    func dictationTranscript(_ transcript: String) {
        let separator = !preDictationContent.isEmpty && !transcript.isEmpty ? "\n\n" : ""
        content = preDictationContent + separator + transcript
    }

    func dictationFinished(sessionId: String, transcript: String) {
        isRecording = false
        loading = false
        dictationTranscript(transcript)
        streamingSessionId = sessionId
        drafts.save(content)
        hasDraft = true
    }

    func dictationFailed(_ message: String?, spendCapped: Bool) {
        isRecording = false
        loading = false
        if !spendCapped { error = message ?? "Streaming transcription failed" }
    }
}
