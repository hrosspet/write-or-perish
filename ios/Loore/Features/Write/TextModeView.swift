import SwiftUI

/// Text mode (`/textmode`, web `WritePage`): "What's on your mind?" and the
/// writing form. A typed entry starts an agentic thread (`/textmode/start`);
/// with AI usage off it is saved as a plain entry, with the web's toast.
/// Opened from the Glean card (`glean`, #435), the new thread is marked as a
/// Glean thread, so every turn of it offers the Glean button.
struct TextModeView: View {
    var glean = false

    @Environment(AppState.self) private var app
    @State private var formToken = 0

    /// A Glean session: the card's flag and the user's Glean (a copied link does
    /// nothing for a user without it).
    private var gleanEntry: Bool { glean && app.capabilities.gleanEnabled }

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                Text("What's on your mind?")
                    .font(LooreFont.serif(25.6, .light, relativeTo: .largeTitle))
                    .foregroundStyle(LooreColor.textPrimary)
                    .multilineTextAlignment(.center)
                    .padding(.bottom, 32)
                    .accessibilityAddTraits(.isHeader)
                NodeFormView(config: config) { result in
                    formToken += 1
                    Self.open(result, app: app)
                }
                .id("\(formToken)-\(app.capabilities.craftMode)")
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.top, 60)
            .padding(.bottom, 40)
            .looreReadableWidth(1170)
        }
        .scrollDismissesKeyboard(.interactively)
        .background {
            ZStack {
                LooreColor.bgDeep
                RadialGradient(colors: [LooreColor.pageGlow.opacity(0.83), .clear], center: UnitPoint(x: 0.5, y: 0.3),
                               startRadius: 0, endRadius: 420)
            }
            .ignoresSafeArea()
        }
        .navigationBarTitleDisplayMode(.inline)
    }

    private var config: NodeFormConfig {
        let craft = app.capabilities.craftMode
        var config = NodeFormConfig(parentId: nil, hidePowerFeatures: !craft, hideAudioUpload: !craft,
                                    placeholder: "Type what's on your mind…")
        config.aiUsageFromGlobalDefault = true
        let glean = gleanEntry
        config.submitOverride = { [app] submission in try await Self.submit(submission, app: app, glean: glean) }
        return config
    }

    /// The page's own submit (web `WritePage.handleSubmit`). `glean` sends
    /// `entry: "glean"` with the entry that starts the thread (typed or dictated).
    static func submit(_ s: NodeFormSubmission, app: AppState, glean: Bool = false) async throws -> NodeFormResult {
        if !s.aiUsage.allowsAI {
            app.toasts.show("Turning off auto-generate. AI usage on some nodes is turned off.", duration: 8)
            if let sid = s.streamingSessionId {
                let answer: SaveAsNodeResponse = try await app.api.post(APIPath.streamingSaveAsNode(sid),
                                                                        json: .object(["content": .string(s.content)]))
                return NodeFormResult(id: answer.id)
            }
            let created: NodeCreateResponse = try await app.api.post(APIPath.nodes, json: .object([
                "content": .string(s.content), "privacy_level": .string(s.privacy.rawString),
                "ai_usage": .string(s.aiUsage.rawString),
            ]))
            app.signals.post(.nodeCreated(created.id))
            return NodeFormResult(id: created.id)
        }
        let stored = UserDefaults.standard.object(forKey: DefaultsKey.autoGenerate)
        let autoGenerate = stored == nil ? true : UserDefaults.standard.bool(forKey: DefaultsKey.autoGenerate)
        if let sid = s.streamingSessionId {
            var body: [String: JSONValue] = [
                "content": .string(s.content), "agentic": .bool(true), "auto_generate": .bool(autoGenerate),
            ]
            if glean { body["entry"] = .string("glean") }
            let answer: SaveAsNodeResponse = try await app.api.post(APIPath.streamingSaveAsNode(sid),
                                                                    json: .object(body))
            return NodeFormResult(id: answer.id, userNodeId: answer.userNodeId, llmNodeId: answer.llmNodeId,
                                  spendCapped: answer.spendCapped, llmError: answer.llmError)
        }
        var body: [String: JSONValue] = [
            "content": .string(s.content), "privacy_level": .string(s.privacy.rawString),
            "ai_usage": .string(s.aiUsage.rawString), "auto_generate": .bool(autoGenerate),
        ]
        if glean { body["entry"] = .string("glean") }
        let answer: TextmodeStartResponse = try await app.api.post(APIPath.textmodeStart, json: .object(body))
        app.signals.post(.nodeCreated(answer.userNodeId))
        return NodeFormResult(id: answer.userNodeId, userNodeId: answer.userNodeId, llmNodeId: answer.llmNodeId,
                              spendCapped: answer.spendCapped, llmError: answer.llmError)
    }

    /// Where a Text-mode send lands (web `WritePage.handleSuccess`). The entry's
    /// page opens once its node is in, with the spinner over this screen meanwhile
    /// (NodePrefetch), not on a loading page.
    static func open(_ result: NodeFormResult, app: AppState) {
        let prefetch = NodePrefetch.shared
        if let llm = result.llmNodeId, let user = result.userNodeId {
            prefetch.openThread(user, awaitLLM: llm, app: app)
        } else if let llm = result.llmNodeId {
            prefetch.openThread(llm, awaitLLM: llm, app: app)
        } else if let user = result.userNodeId {
            prefetch.openThread(user, app: app)
        } else if let id = result.id {
            prefetch.openThread(id, app: app)
        }
    }
}

/// "Write New Entry" (craft, More menu; web `NodeFormModal` with `allowAgenticPrompt`).
struct WriteNewEntrySheet: View {
    @Environment(AppState.self) private var app
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NodeFormSheet(title: "Write New Entry",
                      config: NodeFormConfig(parentId: nil, allowAgenticPrompt: true)) { result in
            dismiss()
            guard let id = result.id else { return }
            app.signals.post(.nodeCreated(id))
            // The entry opens once its node is in, with the spinner over the screen
            // under the sheet meanwhile (NodePrefetch), not on a loading page.
            NodePrefetch.shared.openThread(id, awaitLLM: result.awaitLLM, app: app)
        } onClose: {
            dismiss()
        }
    }
}
