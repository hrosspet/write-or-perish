import XCTest
@testable import Loore

/// Glean in the app (#475): the payloads #473/#474 added, the Home tab and
/// cards, the Glean sessions' routes, the thread page's rules and the tweet card.
final class GleanModelTests: XCTestCase {
    func testCurrentUserCarriesTheGleanFields() throws {
        let user = try decode(CurrentUser.self, #"{"id": 5, "glean_available": true, "glean_enabled": true}"#)
        XCTAssertTrue(user.gleanAvailable)
        XCTAssertTrue(user.gleanEnabled)
        XCTAssertTrue(UserCapabilities(user: user).gleanEnabled)

        let off = try decode(CurrentUser.self, #"{"id": 5, "glean_available": true, "glean_enabled": false}"#)
        XCTAssertTrue(off.gleanAvailable)
        XCTAssertFalse(UserCapabilities(user: off).gleanEnabled, "the user's switch is off: today's app")
    }

    func testAnOlderServerMeansNoGlean() throws {
        let user = try decode(CurrentUser.self, #"{"id": 5}"#)
        XCTAssertFalse(user.gleanAvailable)
        XCTAssertFalse(user.gleanEnabled)
        let wrongType = try decode(CurrentUser.self, #"{"id": 5, "glean_enabled": "yes"}"#)
        XCTAssertFalse(wrongType.gleanEnabled)
        XCTAssertFalse(UserCapabilities().gleanEnabled)
    }

    func testNodeDetailSaysWhetherTheThreadIsAGleanThread() throws {
        let glean = try decode(NodeDetail.self, #"{"id": 7, "content": "x", "glean_thread": true}"#)
        XCTAssertTrue(glean.gleanThread)
        let other = try decode(NodeDetail.self, #"{"id": 7, "content": "x"}"#)
        XCTAssertFalse(other.gleanThread)
    }

    func testQuotedTweetCarriesTheDisplayName() throws {
        let resolved = try decode(ResolvedQuotes.self, #"""
            {"has_quotes": true, "quotes": {}, "external_quotes": {"31": {
              "id": 31, "content": "A tweet.", "source": "read_pick", "author_handle": "ann",
              "author_name": "Ann Example", "url": "https://x.com/ann/status/1", "user_id": 5}}}
            """#)
        let quote = try XCTUnwrap(resolved.externalQuotes.values[31] ?? nil)
        XCTAssertEqual(quote.authorName, "Ann Example")
        XCTAssertEqual(quote.authorHandle, "ann")
        let noName = try decode(ResolvedQuotes.QuotedExternal.self, #"{"id": 32, "content": "", "author_name": null}"#)
        XCTAssertNil(noName.authorName)
    }

    func testReferenceCarriesTheDisplayName() throws {
        let item = try decode(ExternalItem.self, #"{"id": 3, "source": "community_archive", "author_handle": "ann", "author_name": "Ann Example"}"#)
        XCTAssertEqual(item.authorName, "Ann Example")
        XCTAssertEqual(item.authorLabel, "Ann Example @ann")
        let plain = try decode(ExternalItem.self, #"{"id": 3, "source": "twitter_bookmark", "author_handle": "ann"}"#)
        XCTAssertNil(plain.authorName)
        XCTAssertEqual(plain.authorLabel, "@ann")
        XCTAssertEqual(ReferenceUtils.authorLabel(source: "web_clip", handle: "The Site", name: "Ignored"), "The Site")
    }

    func testGleanStartAnswer() throws {
        let answer = try decode(GleanStartResponse.self, #"{"llm_node_id": 41, "task_id": "abc"}"#)
        XCTAssertEqual(answer.llmNodeId, 41)
        XCTAssertEqual(answer.openId, 41)
        let promptOnly = try decode(GleanStartResponse.self, #"{"prompt_node_id": 40}"#)
        XCTAssertNil(promptOnly.llmNodeId)
        XCTAssertEqual(promptOnly.openId, 40)
    }

    func testGleanRequestSendsTheModelOnlyWhenPicked() {
        XCTAssertEqual(GleanRequest.body(model: nil), .object([:]), "no pick: the server's default")
        XCTAssertEqual(GleanRequest.body(model: ""), .object([:]))
        XCTAssertEqual(GleanRequest.body(model: "claude-sonnet-5.5"), .object(["model": .string("claude-sonnet-5.5")]))
    }

    func testPickerDefaultWhenTheProviderHasNoGleanModel() throws {
        // `suggested-model?purpose=read` answers null + "unavailable": nothing is
        // preselected, and a glean without a model is refused by the server.
        let suggestion = try decode(SuggestedModel.self, #"{"suggested_model": null, "source": "unavailable"}"#)
        XCTAssertNil(suggestion.suggestedModel)
        let models = try APIClient.makeDecoder().decode(ModelsResponse.self, from: try fixture("models.json")).models
        XCTAssertNil(ModelPickerOptions.appliedSuggestion(models: models, suggestion: suggestion, selected: nil,
                                                          purpose: .read))
    }

    func testTheGleanPickerOffersTheReadModelsOfBothProviders() throws {
        let models = try APIClient.makeDecoder().decode(ModelsResponse.self, from: try fixture("models.json")).models
        let offered = ModelPickerOptions.offered(models, purpose: .read)
        XCTAssertFalse(offered.isEmpty)
        XCTAssertTrue(offered.allSatisfy(\.read))
        XCTAssertEqual(Set(offered.map(\.provider)), Set(models.filter(\.read).map(\.provider)))
    }

    func testSpendCapToastForAGlean() {
        let now = Date(timeIntervalSince1970: 1_791_000_000) // 2026-10-03
        XCTAssertEqual(SpendCap.toastMessage(.glean, now: now),
                       "You've reached your monthly usage limit, so you can't glean until it resets on November 1.")
    }
}

final class GleanRouteTests: XCTestCase {
    private let env = AppEnvironment.production

    func testTheFirstTabIsNamedHome() {
        XCTAssertEqual(AppTab.reflect.title, "Home")
        XCTAssertEqual(AppRoute.home.screenTitle, "Home")
    }

    func testGleanSessionsParseAndPrint() {
        XCTAssertEqual(AppRoute.parse("/voice?glean=1", environment: env),
                       .voice(parentId: nil, resumeLLMId: nil, glean: true))
        XCTAssertEqual(AppRoute.parse("/voice?parent=5&glean=1", environment: env),
                       .voice(parentId: 5, resumeLLMId: nil, glean: true))
        XCTAssertEqual(AppRoute.parse("/voice", environment: env), .voice(parentId: nil, resumeLLMId: nil, glean: false))
        XCTAssertEqual(AppRoute.parse("/textmode?glean=1", environment: env), .textMode(glean: true))
        XCTAssertEqual(AppRoute.parse("/textmode", environment: env), .textMode(glean: false))
        XCTAssertEqual(AppRoute.voice(parentId: 5, resumeLLMId: nil, glean: true).webPath, "/voice?parent=5&glean=1")
        XCTAssertEqual(AppRoute.textMode(glean: true).webPath, "/textmode?glean=1")
        XCTAssertEqual(AppRoute.textMode().webPath, "/textmode")
        XCTAssertEqual(AppRoute.voice(parentId: nil, resumeLLMId: nil, glean: true).preferredTab, .reflect)
    }

    func testHomeCardsOpenTheirSessions() {
        XCTAssertEqual(HomePurpose.allCases.map(\.title), ["Reflect", "Glean"])
        XCTAssertEqual(HomePurpose.reflect.line, "Talk it through with Loore.")
        XCTAssertEqual(HomePurpose.glean.line, "Reflect, and Loore gleans for you.")
        XCTAssertEqual(HomePurpose.reflect.route(.voice), .voice(parentId: nil, resumeLLMId: nil, glean: false))
        XCTAssertEqual(HomePurpose.reflect.route(.text), .textMode(glean: false))
        XCTAssertEqual(HomePurpose.glean.route(.voice), .voice(parentId: nil, resumeLLMId: nil, glean: true))
        XCTAssertEqual(HomePurpose.glean.route(.text), .textMode(glean: true))
    }

    @MainActor
    func testATweetCardsReferencePageOpensOverItsThread() {
        let router = Router()
        router.selectedTab = .log
        router.paths[.log] = [.thread(id: 5, awaitLLM: nil)]
        router.push(.reference(id: 31))
        XCTAssertEqual(router.selectedTab, .log, "not the More tab, where References live")
        XCTAssertEqual(router.path(for: .log), [.thread(id: 5, awaitLLM: nil), .reference(id: 31)])
    }
}

@MainActor
final class GleanThreadRuleTests: XCTestCase {
    private func menu(gleanEnabled: Bool = true, gleanThread: Bool = false, owned: Bool = true,
                      deleted: Bool = false, isSystemPrompt: Bool = false, aiUsage: AIUsage? = .chat,
                      pending: Bool = false) -> Bool {
        ThreadModel.offersGleanInMenu(gleanEnabled: gleanEnabled, gleanThread: gleanThread, owned: owned,
                                      deleted: deleted, isSystemPrompt: isSystemPrompt, aiUsage: aiUsage,
                                      pending: pending)
    }

    func testGleanForThisReflectionIsInTheMenuOfOwnEntriesOutsideGleanThreads() {
        XCTAssertTrue(menu())
        XCTAssertTrue(menu(aiUsage: .train))
        XCTAssertTrue(menu(aiUsage: nil), "an ancestor without ai_usage, as on the web")
        XCTAssertEqual(ThreadModel.gleanMenuLabel, "Glean for this reflection")
    }

    func testNoMenuEntryWhereItDoesNotBelong() {
        XCTAssertFalse(menu(gleanEnabled: false), "a user without Glean sees none of it")
        XCTAssertFalse(menu(gleanThread: true), "a Glean-card thread has the button instead")
        XCTAssertFalse(menu(owned: false))
        XCTAssertFalse(menu(deleted: true))
        XCTAssertFalse(menu(isSystemPrompt: true))
        XCTAssertFalse(menu(aiUsage: .off), "no AI means no AI")
        XCTAssertFalse(menu(pending: true), "not on a reply still being written")
    }

    func testTheGleaningsHead() {
        XCTAssertEqual(ThreadModel.gleaningsTitle, "Today's gleanings")
        XCTAssertNil(ThreadModel.gleaningsSubline(picks: 0))
        XCTAssertEqual(ThreadModel.gleaningsSubline(picks: 1), "1 tweet from today, chosen for what you said.")
        XCTAssertEqual(ThreadModel.gleaningsSubline(picks: 4), "4 tweets from today, chosen for what you said.")
        XCTAssertEqual(ThreadModel.emptyDayDetail(tweetsRead: 1240),
                       "Loore read all 1,240 of today's tweets in the archive.")
        XCTAssertEqual(ThreadModel.emptyDayDetail(tweetsRead: 0), "The archive had no new tweets today.")
    }
}

final class TweetCardTests: XCTestCase {
    private func quote(_ json: String) throws -> ResolvedQuotes.QuotedExternal {
        try decode(ResolvedQuotes.QuotedExternal.self, json)
    }

    func testTheTextIsCutAt500Characters() {
        let long = String(repeating: "a", count: 501)
        XCTAssertEqual(ExternalQuoteBubble.previewText(long), String(repeating: "a", count: 500) + "…")
        let exact = String(repeating: "b", count: 500)
        XCTAssertEqual(ExternalQuoteBubble.previewText(exact), exact)
    }

    func testTheTweetBylineIsNameHandleNoSavedFromLine() throws {
        let named = try quote(#"{"id": 1, "content": "x", "source": "read_pick", "author_handle": "ann", "author_name": "Ann"}"#)
        XCTAssertEqual(ExternalQuoteBubble.byline(named).name, "Ann")
        XCTAssertEqual(ExternalQuoteBubble.byline(named).handle, "@ann")
        let unnamed = try quote(#"{"id": 1, "content": "x", "source": "community_archive", "author_handle": "ann"}"#)
        XCTAssertNil(ExternalQuoteBubble.byline(unnamed).name)
        XCTAssertEqual(ExternalQuoteBubble.byline(unnamed).handle, "@ann")
        let noHandle = try quote(#"{"id": 1, "content": "x", "source": "twitter_bookmark"}"#)
        XCTAssertEqual(ExternalQuoteBubble.byline(noHandle).handle, "@unknown")
    }

    func testAPageNamesItsTitleAndSite() throws {
        let clip = try quote(#"{"id": 2, "content": "x", "source": "web_clip", "title": "A page", "url": "https://www.example.org/a"}"#)
        XCTAssertEqual(ExternalQuoteBubble.byline(clip).name, "A page")
        XCTAssertEqual(ExternalQuoteBubble.byline(clip).handle, "example.org")
    }
}

/// The rework after Peter's local test (2026-10-09): Glean and its picker on the
/// Glean entry, a failed gleaning that says so and where to try again, the
/// "Glean" tag, and no quote markers in a card.
@MainActor
final class GleanReworkTests: StubbedAppTestCase {
    override func setUp() async throws {
        try await super.setUp()
        let user = try decode(CurrentUser.self, """
        {"id":5,"username":"seowriter","approved":true,"terms_up_to_date":true,"plan":"alpha","craft_mode":false,
         "preferred_model":"claude-opus-4.6","default_privacy_level":"private","default_ai_usage":"chat",
         "voice_mode_enabled":true,"timezone":"UTC","glean_available":true,"glean_enabled":true}
        """)
        app.useForTesting(api: makeStubbedClient(environment: .local), user: user)
    }

    private let entry = """
    {"id":20,"content":"I keep going back and forth.","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private"}
    """
    private let readPrompt = """
    {"id":31,"content":"","node_type":"user","user_id":5,"ai_usage":"chat","privacy_level":"private",
     "is_system_prompt":true,"prompt_key":"read_thread"}
    """

    private func load(_ id: Int, _ json: String) async -> ThreadModel {
        StubURLProtocol.install { request in
            let path = request.url?.path(percentEncoded: true) ?? ""
            if path == "/api/nodes/\(id)" { return .json(200, json) }
            if path.hasPrefix("/api/read/from-node/") { return .json(202, #"{"llm_node_id":41,"task_id":"t"}"#) }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: id, awaitLLM: nil, app: app)
        await model.load()
        return model
    }

    private func gleanEntry(gleanThread: Bool) -> String {
        """
        {"id":31,"content":"","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
         "ai_usage":"chat","is_system_prompt":true,"prompt_key":"read_thread","child_count":0,"children":[],
         "ancestors":[\(entry)],"in_read_thread":true,"glean_thread":\(gleanThread)}
        """
    }

    func testTheGleanEntryCarriesGleanInAnyThread() async {
        for gleanThread in [true, false] {
            let model = await load(31, gleanEntry(gleanThread: gleanThread))
            XCTAssertTrue(model.isGleanEntry)
            XCTAssertTrue(model.readActions(craftMode: false), "glean_thread \(gleanThread)")
        }
    }

    func testTheThreadsOwnVoicePromptHasNoGlean() async {
        let model = await load(10, """
        {"id":10,"content":"","node_type":"user","user":{"id":5,"username":"seowriter"},"privacy_level":"private",
         "ai_usage":"chat","is_system_prompt":true,"prompt_key":"voice","child_count":0,"children":[],
         "ancestors":[],"glean_thread":true}
        """)
        XCTAssertFalse(model.isGleanEntry)
        XCTAssertFalse(model.readActions(craftMode: false))
    }

    func testAFailedGleaningSaysWhyAndTheRetryStartsWhereItDid() async {
        let model = await load(32, """
        {"id":32,"content":"[LLM response generation pending...]","node_type":"llm","llm_model":"claude-haiku-5.5",
         "user":{"id":3,"username":"claude-haiku-5.5"},"parent_user_id":5,"privacy_level":"private","ai_usage":"chat",
         "llm_task_status":"failed","llm_task_error":"The model provider did not answer.",
         "tool_calls_meta":[{"name":"_live"}],"child_count":0,"children":[],
         "ancestors":[\(entry),\(readPrompt)],"in_read_thread":true,"read_reply_above":true,"glean_thread":true}
        """)
        XCTAssertTrue(model.gleaningFailed)
        XCTAssertFalse(model.gleaningDone)
        XCTAssertEqual(model.node?.llmTaskError, "The model provider did not answer.")
        XCTAssertTrue(model.readActions(craftMode: false))
        model.gleanHere()
        let posted = await eventually {
            StubURLProtocol.requests.contains { $0.url?.path(percentEncoded: true) == "/api/read/from-node/31" }
        }
        XCTAssertTrue(posted, "the next glean starts under the Glean entry, not under the failed reply")
    }

    /// A gleaning that finishes while its page is open: the poll's answer has no
    /// render, so the node is fetched once more for the window line and the empty
    /// day (web NodeDetail). A fetch that is not completed yet is not kept.
    private func watchGleaning(reloadStatus: String) async -> ThreadModel {
        final class Count: @unchecked Sendable {
            private let lock = NSLock()
            private var n = 0
            func next() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n }
        }
        let gets = Count()
        let base = """
        "id":32,"node_type":"llm","llm_model":"claude-haiku-5.5",
        "user":{"id":3,"username":"claude-haiku-5.5"},"parent_user_id":5,"privacy_level":"private","ai_usage":"chat",
        "tool_calls_meta":[{"name":"_live"}],"child_count":0,"children":[],
        "ancestors":[\(entry),\(readPrompt)],"in_read_thread":true,"glean_thread":true
        """
        let pending = "{\(base),\"content\":\"[LLM response generation pending...]\",\"llm_task_status\":\"pending\"}"
        let reloaded = """
        {\(base),"content":"A quiet day.","llm_task_status":"\(reloadStatus)","read_reply":true,
         "read_window":{"tweets":1240,"accounts":300,"excluded":0,
                        "window_start":"2026-10-08T07:00:00Z","window_end":"2026-10-09T07:00:00Z"}}
        """
        StubURLProtocol.install { request in
            switch request.url?.path(percentEncoded: true) ?? "" {
            case "/api/nodes/32": return .json(200, gets.next() == 1 ? pending : reloaded)
            case "/api/nodes/32/llm-status":
                return .json(200, #"{"node_id":32,"status":"completed","content":"A quiet day.","tool_calls_meta":[{"name":"_live"}]}"#)
            default: return .json(200, "{}")
            }
        }
        let tab = app.router.selectedTab
        app.router.setPath([.thread(id: 31, awaitLLM: nil), .thread(id: 32, awaitLLM: 32)], for: tab)
        let model = ThreadModel(nodeId: 32, awaitLLM: 32, app: app)
        await model.start()
        return model
    }

    func testAGleaningFinishedWhileOpenGetsItsWindowAndEmptyDay() async {
        let model = await watchGleaning(reloadStatus: "completed")
        let shown = await eventually { model.node?.readWindow != nil }
        XCTAssertTrue(shown, "the window line comes without leaving the page")
        XCTAssertEqual(model.node?.readWindow?.tweets, 1240)
        XCTAssertTrue(model.gleaningEmpty, "an empty day says so at once")
        model.stop()
    }

    func testAReloadThatIsNotCompletedIsNotKept() async {
        let model = await watchGleaning(reloadStatus: "processing")
        let done = await eventually { model.node?.llmTaskStatus == .completed }
        XCTAssertTrue(done)
        try? await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(model.node?.llmTaskStatus, .completed, "the page stays on the finished gleaning")
        XCTAssertNil(model.node?.readWindow)
        model.stop()
    }

    /// "Glean for this reflection" in a failed gleaning's own ⋯ menu, like its
    /// Glean button, starts from the parent: the failed reply is never read.
    func testAGleanOnAFailedReplyStartsFromItsParent() async {
        let failed = await load(32, """
        {"id":32,"content":"[LLM response generation pending...]","node_type":"llm","llm_model":"claude-haiku-5.5",
         "user":{"id":3,"username":"claude-haiku-5.5"},"parent_user_id":5,"privacy_level":"private","ai_usage":"chat",
         "llm_task_status":"failed","tool_calls_meta":[{"name":"_live"}],"child_count":0,"children":[],
         "ancestors":[\(entry),\(readPrompt)],"in_read_thread":true,"glean_thread":false}
        """)
        XCTAssertEqual(failed.gleanTarget, 31)
        XCTAssertTrue(failed.offersGleanInMenu(owned: true, deleted: false, isSystemPrompt: false, aiUsage: .chat))

        let done = await load(33, """
        {"id":33,"content":"A quiet day.","node_type":"llm","llm_model":"claude-haiku-5.5",
         "user":{"id":3,"username":"claude-haiku-5.5"},"parent_user_id":5,"privacy_level":"private","ai_usage":"chat",
         "llm_task_status":"completed","tool_calls_meta":[{"name":"_live"}],"child_count":0,"children":[],
         "ancestors":[\(entry),\(readPrompt)],"in_read_thread":true,"glean_thread":false}
        """)
        XCTAssertEqual(done.gleanTarget, 33, "glean again under a finished gleaning keeps its picks in view")
    }

    func testTheReadPromptsAreTaggedGlean() {
        XCTAssertEqual(BubblePreview.promptLabel("read"), "Glean")
        XCTAssertEqual(BubblePreview.promptLabel("read_thread"), "Glean")
    }

    func testACardLeavesOutQuoteMarkers() {
        let data = BubbleData(id: 1, content: "Two tweets today.\n\nWhy this one. {quote_ext:12}\n\nAnd {quote:7} this one.")
        let shown = BubblePreview.display(text: data.text, threadName: nil)
        XCTAssertEqual(shown.heading, "Two tweets today.")
        XCTAssertFalse(shown.body.contains("{quote"))
        XCTAssertTrue(shown.body.contains("And this one."))
    }

    private func eventually(_ condition: @escaping () -> Bool) async -> Bool {
        for _ in 0..<40 {
            if condition() { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition()
    }
}
