import XCTest
@testable import Loore

/// Every model against fixtures captured from the local backend (user 5), plus
/// hand-written payloads for shapes user 5 does not have (tombstones, unknown
/// enum values, missing fields).
final class ModelDecodingTests: XCTestCase {
    private let decoder = APIClient.makeDecoder()

    func testDashboard() throws {
        let dashboard = try decoder.decode(DashboardResponse.self, from: try fixture("dashboard.json"))
        XCTAssertEqual(dashboard.user.id, 5)
        XCTAssertEqual(dashboard.user.username, "seowriter")
        XCTAssertTrue(dashboard.user.approved)
        XCTAssertTrue(dashboard.user.termsUpToDate)
        XCTAssertEqual(dashboard.user.plan, .alpha)
        XCTAssertEqual(dashboard.user.defaultPrivacyLevel, .private)
        XCTAssertNotNil(dashboard.user.acceptedTermsAt)
        XCTAssertFalse(dashboard.nodes.isEmpty)
        XCTAssertNotNil(dashboard.nodes.first?.createdAt)
        XCTAssertNil(dashboard.latestProfile)
        let caps = UserCapabilities(user: dashboard.user)
        XCTAssertTrue(caps.approved)
        XCTAssertTrue(caps.voiceModeEnabled)
        XCTAssertFalse(caps.isAdmin)
    }

    func testCurrentUserIsTolerant() throws {
        let user = try decode(CurrentUser.self, #"{"id": 9, "plan": "enterprise", "default_ai_usage": "none", "craft_mode": null, "prefill_consent": "maybe", "timezone": "Europe/Prague"}"#)
        XCTAssertEqual(user.username, "")
        XCTAssertFalse(user.approved, "absent approval must read as not approved")
        XCTAssertFalse(user.termsUpToDate)
        XCTAssertEqual(user.plan, .unknown("enterprise"))
        XCTAssertEqual(user.defaultAIUsage, .off)
        XCTAssertNil(user.craftMode)
        XCTAssertEqual(user.prefillConsent, .unknown("maybe"))
        XCTAssertEqual(UserCapabilities(user: user, craftModeFallback: true).craftMode, true)
        XCTAssertEqual(UserCapabilities(user: user, craftModeFallback: false).craftMode, false)
    }

    func testCapabilitiesPreferServerCraftMode() throws {
        let user = try decode(CurrentUser.self, #"{"id": 1, "craft_mode": false, "approved": true, "share_v1_enabled": true}"#)
        let caps = UserCapabilities(user: user, craftModeFallback: true)
        XCTAssertFalse(caps.craftMode)
        XCTAssertTrue(caps.showsCommons)
    }

    func testEmailStateMerge() throws {
        var user = try decode(CurrentUser.self, #"{"id": 1, "email": null}"#)
        let state = try decode(EmailState.self, #"{"message": "Confirmation link sent.", "email": null, "pending_email": "a@example.invalid", "pending_email_expired": false}"#)
        user.apply(state)
        XCTAssertEqual(user.pendingEmail, "a@example.invalid")
        XCTAssertFalse(user.pendingEmailExpired)
    }

    func testLog() throws {
        let page = try decoder.decode(LogPage.self, from: try fixture("log.json"))
        XCTAssertEqual(page.nodes.count, 5)
        XCTAssertTrue(page.hasMore)
        let cursor = try XCTUnwrap(page.nextCursor)
        XCTAssertTrue(cursor.contains("|"), "the cursor is opaque: <iso>|<id>")
        let first = try XCTUnwrap(page.nodes.first)
        XCTAssertNotEqual(first.id, first.threadRootId)
        XCTAssertNotNil(first.newestNodeId)
        XCTAssertEqual(first.promptKey, "voice")
        XCTAssertTrue(first.hasOriginalAudio)
    }

    func testNodeDetailTree() throws {
        let node = try decoder.decode(NodeDetail.self, from: try fixture("node_detail.json"))
        XCTAssertEqual(node.id, 201084)
        XCTAssertEqual(node.nodeType, .user)
        XCTAssertEqual(node.user, NodeAuthor(id: 5, username: "seowriter"))
        XCTAssertEqual(node.aiUsage, .chat)
        XCTAssertEqual(node.ancestors.count, 1)
        let root = node.ancestors[0]
        XCTAssertTrue(root.systemPrompt.isSystemPrompt)
        XCTAssertEqual(root.systemPrompt.promptKey, "textmode")
        XCTAssertEqual(root.systemPrompt.contextArtifacts?.prompt?.promptKey, "textmode")
        XCTAssertEqual(root.systemPrompt.contextArtifacts?.recentRaw?.sourceTokens, 7751)
        XCTAssertNotNil(root.systemPrompt.contextArtifacts?.shareGuidance)
        XCTAssertEqual(node.children.count, 2)
        let deepest = node.children.first?.children.first?.children.first
        XCTAssertEqual(deepest?.id, 201088)
        XCTAssertEqual(deepest?.nodeType, .llm)
        XCTAssertEqual(deepest?.llmModel, "claude-opus-5.5")
        XCTAssertEqual(node.children.first?.descendantCount, 2)
    }

    func testTreeVariants() throws {
        let json = #"""
        {"id": 1, "content": "root", "node_type": "user", "created_at": "2026-09-30T10:00:00Z",
         "llm_task_status": "streaming_v2", "privacy_level": "public", "ai_usage": "train",
         "tool_calls_meta": [{"name": "propose_todo", "status": "success", "apply_status": "started", "created": 3},
                             {"name": "_batch", "status": "submitted"}],
         "ancestors": [{"id": 0, "deleted": true, "deleted_at": "2026-09-29T10:00:00Z", "username": null,
                        "node_type": "user", "created_at": "2026-09-28T10:00:00Z", "child_count": 1,
                        "ai_usage": "chat", "privacy_level": "private", "is_system_prompt": false}],
         "children": [
           {"id": 2, "deleted": true, "deleted_at": "2026-09-30T11:00:00Z", "username": null, "node_type": "llm",
            "created_at": "2026-09-30T10:05:00Z", "child_count": 1, "descendant_count": 1,
            "children": [{"id": 3, "content": "kept", "node_type": "brand_new_type", "child_count": 0,
                          "created_at": "2026-09-30T10:06:00+00:00", "updated_at": "2026-09-30T10:06:00",
                          "username": "seowriter", "descendant_count": 0, "user_id": 5, "children": [],
                          "has_tts": false, "is_system_prompt": false}]},
           {"id": 4, "inaccessible": true}
         ]}
        """#
        let node = try decode(NodeDetail.self, json)
        XCTAssertEqual(node.llmTaskStatus, .unknown("streaming_v2"))
        XCTAssertEqual(node.privacyLevel, .public)
        XCTAssertEqual(node.aiUsage, .train)
        XCTAssertEqual(node.toolCallsMeta?.count, 2)
        XCTAssertEqual(node.toolCallsMeta?.first?.applyStatus, "started")
        XCTAssertEqual(node.toolCallsMeta?.first?["created"]?.intValue, 3)
        XCTAssertEqual(node.toolCallsMeta?.last?.isInternal, true)
        XCTAssertTrue(node.ancestors[0].deleted)
        XCTAssertNil(node.ancestors[0].content)
        XCTAssertTrue(node.children[0].deleted)
        XCTAssertEqual(node.children[0].children[0].nodeType, .unknown("brand_new_type"))
        XCTAssertTrue(node.children[1].inaccessible)
        XCTAssertEqual(node.childCount, 0)
        XCTAssertNil(node.replyAIUsage)
    }

    // MARK: Deep reply trees (review M6)

    /// A focal node with a single chain of `depth` replies below it.
    static func chain(depth: Int) -> String {
        var json = #"{"id": 1, "content": "root", "node_type": "user", "child_count": 1, "children": ["#
        for level in 1...depth {
            // Content that looks like structure must not confuse the scan.
            json += #"{"id": \#(level + 1), "content": "level \#(level) {\"children\": [ ] } \\", "#
            json += #""node_type": "\#(level.isMultiple(of: 2) ? "llm" : "user")", "child_count": 1, "#
            json += #""descendant_count": \#(depth - level), "tool_calls_meta": [{"children": [1, 2]}], "children": ["#
        }
        json += String(repeating: "]}", count: depth) + "]}"
        return json
    }

    func testADeepThreadDecodesWithoutTheNestingLimit() throws {
        let data = Data(Self.chain(depth: 300).utf8)
        XCTAssertThrowsError(try decoder.decode(NodeDetail.self, from: data), "Foundation's parser stops at 512 levels")
        let node = try NodeTreeDecoding.decodeNodeDetail(data, decoder: decoder)
        var depth = 0
        var level = node.children.first
        var last: TreeNode?
        while let current = level {
            depth += 1
            XCTAssertEqual(current.id, depth + 1)
            last = current
            level = current.children.first
        }
        XCTAssertEqual(depth, 300)
        XCTAssertEqual(last?.content, #"level 300 {"children": [ ] } \"#)
        XCTAssertEqual(last?.nodeType, .llm)
        XCTAssertEqual(ChildRow.flatten(node.children).count, 300)
    }

    func testTheFlatDecodingMatchesTheNestedOne() throws {
        let fixtureData = try fixture("node_detail.json")
        let tolerant = #"""
        {"id": 1, "content": "root", "node_type": "user", "children": [
          {"id": 2, "content": "a", "children": [{"id": 3, "children": []}, {"content": "no id", "children": []}]},
          {"id": 4, "content": "b \"quoted\" {[", "children": [{"id": 5, "content": "c", "children": []}]},
          {"id": 6, "inaccessible": true}
        ], "ancestors": [{"id": 0, "children": [{"id": 99}]}]}
        """#
        for data in [fixtureData, Data(tolerant.utf8)] {
            let nested = try decoder.decode(NodeDetail.self, from: data)
            let flat = try NodeTreeDecoding.decodeNodeDetail(data, decoder: decoder)
            XCTAssertEqual(flat.id, nested.id)
            XCTAssertEqual(flat.content, nested.content)
            XCTAssertEqual(flat.ancestors, nested.ancestors)
            XCTAssertEqual(flat.children, nested.children)
        }
        let flat = try NodeTreeDecoding.decodeNodeDetail(Data(tolerant.utf8), decoder: decoder)
        XCTAssertEqual(flat.children.map(\.id), [2, 4, 6])
        XCTAssertEqual(flat.children[0].children, [], "a child that does not decode drops its siblings, as before")
        XCTAssertEqual(flat.children[1].children.map(\.id), [5])
    }

    func testNodeUpdateResponseWithoutTree() throws {
        let answer = try decode(NodeUpdateResponse.self, #"{"message": "Node updated", "node": {"id": 7, "content": "x", "node_type": "user"}, "descendants_updated": 2}"#)
        XCTAssertEqual(answer.node.id, 7)
        XCTAssertTrue(answer.node.children.isEmpty)
        XCTAssertEqual(answer.descendantsUpdated, 2)
    }

    func testLLMStatus() throws {
        let status = try decoder.decode(LLMStatus.self, from: try fixture("llm_status_completed.json"))
        XCTAssertEqual(status.nodeId, 201088)
        XCTAssertEqual(status.status, .completed)
        XCTAssertEqual(status.pollStatus?.isTerminal, true)
        XCTAssertEqual(status.warnings, [])
        XCTAssertNotNil(status.content)

        let batch = try decode(LLMStatus.self, #"{"node_id": 3, "status": "processing", "progress": 0, "stage": "batch", "batch_submitted_at": "2026-09-30T10:00:00", "tts_streaming": true, "continuation_node_id": 4, "warnings": ["w"]}"#)
        XCTAssertEqual(batch.stage, "batch")
        XCTAssertNotNil(batch.batchSubmittedAt)
        XCTAssertTrue(batch.ttsStreaming)
        XCTAssertEqual(batch.continuationNodeId, 4)
        XCTAssertEqual(batch.pollStatus?.isTerminal, false)
    }

    func testTitlesAndQuotes() throws {
        let titles = try decoder.decode(NodeTitlesResponse.self, from: try fixture("node_titles.json"))
        XCTAssertEqual(titles.titles[201084]?.title, "In about 200 words, what is a cirque? Don't look anything up.")
        XCTAssertTrue(titles.titles.contains(999999999))
        XCTAssertNil(titles.titles[999999999], "null = missing or not visible")
        let deleted = try decode(NodeTitlesResponse.self, #"{"titles": {"5": {"id": 5, "deleted": true, "title": null}}}"#)
        XCTAssertEqual(deleted.titles[5]?.deleted, true)

        let empty = try decoder.decode(ResolvedQuotes.self, from: try fixture("resolve_quotes_empty.json"))
        XCTAssertFalse(empty.hasQuotes)
        let quotes = try decode(ResolvedQuotes.self, #"""
        {"has_quotes": true,
         "quotes": {"11": {"id": 11, "content": "quoted", "username": "seowriter", "user_id": 5, "created_at": "2026-09-01T00:00:00Z", "node_type": "user", "ai_usage": "chat"},
                    "12": null},
         "external_quotes": {"3": {"id": 3, "content": "tweet", "source": "community_archive", "author_handle": "someone", "title": null, "url": "https://x.com/i/1", "posted_at": null, "user_id": 5, "read_at": null, "feedback": "good"}}}
        """#)
        XCTAssertEqual(quotes.quotes[11]?.content, "quoted")
        XCTAssertTrue(quotes.quotes.contains(12))
        XCTAssertNil(quotes.quotes[12])
        XCTAssertEqual(quotes.externalQuotes[3]?.feedback, "good")
    }

    func testModels() throws {
        let models = try decoder.decode(ModelsResponse.self, from: try fixture("models.json"))
        XCTAssertFalse(models.models.isEmpty)
        XCTAssertTrue(models.models.contains { $0.featured })
        XCTAssertTrue(models.models.allSatisfy { !$0.id.isEmpty && !$0.provider.isEmpty })
        // The fixture predates the `chat` flag: a server without it means every model chats.
        XCTAssertTrue(models.models.allSatisfy(\.chat))
    }

    func testModelChatFlag() throws {
        let models = try decode([ModelInfo].self, """
        [{"id":"gpt-6.1-sol","name":"GPT-6.1 Sol","provider":"openai","featured":false,"read":true,"chat":false},
         {"id":"claude-opus-5.5","name":"Opus 5.5","provider":"anthropic","featured":true,"read":false,"chat":true},
         {"id":"gpt-6-luna","name":"GPT-6 Luna","provider":"openai","featured":false,"read":true},
         {"id":"gpt-6-astra","name":"GPT-6 Astra","provider":"openai","featured":true,"read":false,"chat":null},
         {"id":"gpt-5.6-luna","name":"GPT-5.6 Luna","provider":"openai","featured":false,"read":true,"chat":"no"}]
        """)
        XCTAssertEqual(models.map(\.chat), [false, true, true, true, true])
        XCTAssertTrue(models[0].read)
    }

    func testUpdates() throws {
        let empty = try decoder.decode(UpdatesPayload.self, from: try fixture("updates_empty.json"))
        XCTAssertTrue(empty.isEmpty)
        let payload = try decode(UpdatesPayload.self, #"""
        {"changelog": [{"id": "voice-latency", "title": "Faster replies", "date": "2026-09-28", "body": "See [Account](/account#model)."}],
         "notifications": [{"id": 4, "type": "profile_ready", "title": "Your profile is ready", "body": null, "link": "/profile",
                            "created_at": "2026-09-29T08:00:00Z", "meta": {"version": 7, "created_at": "2026-09-29T07:59:00+00:00"}}],
         "polls": [{"id": 2, "question": "What should we build next?", "created_at": "2026-09-20T10:00:00Z",
                    "draft_terms": {"model": "Claude Opus", "data_source": "recent_window"},
                    "response": {"status": "drafting", "content": "", "generated_by": null, "draft_requested_at": "2026-09-30T10:00:00Z", "sent_at": null}}]}
        """#)
        XCTAssertEqual(payload.totalCount, 3)
        XCTAssertEqual(payload.changelog.first?.date, "2026-09-28")
        XCTAssertEqual(payload.notifications.first?.meta?.version, 7)
        XCTAssertEqual(payload.polls.first?.draftTerms?.dataSource, "recent_window")
        XCTAssertEqual(payload.polls.first?.response?.status, "drafting")
    }

    func testDraftsAndRecording() throws {
        let interrupted = try decoder.decode([InterruptedDraft].self, from: try fixture("drafts_interrupted.json"))
        XCTAssertTrue(interrupted.isEmpty)

        let status = try decode(StreamingSessionStatus.self, #"""
        {"session_id": "abc", "draft_id": 4, "streaming_status": "finalizing", "streaming_mime_type": "audio/mp4",
         "total_chunks": null, "completed_chunks": 1, "failed_chunks": 0,
         "chunks": [{"chunk_index": 0, "status": "completed", "text": "hi", "error": null},
                    {"chunk_index": 2, "status": "stored", "text": null, "error": null}],
         "content": "hi"}
        """#)
        XCTAssertEqual(status.streamingStatus, .finalizing)
        XCTAssertEqual(status.nextChunkIndex, 3, "resume at max(chunk_index)+1, not the row count")
        XCTAssertNil(status.llmNodeId)

        let saved = try decode(SaveAsNodeResponse.self, #"{"id": 9, "user_node_id": 9, "tip_id": 9, "content": "x", "parent_id": null, "privacy_level": "private", "ai_usage": "chat", "created_at": "2026-09-30T10:00:00Z", "spend_capped": true}"#)
        XCTAssertTrue(saved.spendCapped)
        XCTAssertNil(saved.llmNodeId)

        let chunk = try decode(ChunkUploadResponse.self, #"{"message": "Chunk already uploaded", "chunk_index": "4", "status": "stored"}"#)
        XCTAssertEqual(chunk.chunkIndex, 4)

        let textmode = try decode(TextmodeStartResponse.self, #"{"conversation_id": 1, "user_node_id": 2, "llm_node_id": 3, "task_id": "t"}"#)
        XCTAssertEqual(textmode.llmNodeId, 3)
        XCTAssertFalse(textmode.spendCapped)
    }

    func testIntKeyedMapSkipsNonNumericKeys() throws {
        let map = try decode(IntKeyedMap<String>.self, #"{"1": "a", "x": "b", "2": null}"#)
        XCTAssertEqual(map[1], "a")
        XCTAssertEqual(map.values.count, 2)
    }

    func testJSONValueRoundTrip() throws {
        let value: JSONValue = ["a": 1, "b": [true, nil, "x"], "c": 1.5]
        let data = try JSONEncoder().encode(value)
        XCTAssertEqual(try JSONDecoder().decode(JSONValue.self, from: data), value)
        XCTAssertEqual(value["b"]?[2]?.stringValue, "x")
        XCTAssertEqual(value["a"]?.intValue, 1)
    }

    func testOpenEnumsRoundTrip() throws {
        for raw in ["private", "circles", "public", "anonymous"] {
            XCTAssertEqual(PrivacyLevel(rawString: raw).rawString, raw)
        }
        XCTAssertEqual(AIUsage(rawString: "none"), .off)
        XCTAssertTrue(AIUsage.chat.allowsAI)
        XCTAssertFalse(AIUsage.off.allowsAI)
        let encoded = try JSONEncoder().encode(["x": AIUsage.off])
        XCTAssertEqual(String(decoding: encoded, as: UTF8.self), #"{"x":"none"}"#)
    }
}
