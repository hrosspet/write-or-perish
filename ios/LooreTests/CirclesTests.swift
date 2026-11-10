import XCTest
@testable import Loore

/// Circles: the payloads decode, the wording is the web's, and the routes exist.
/// The fixtures are written by hand from the server's serializers
/// (backend/routes/circles.py, utils/circles.audience_for_viewer).
final class CirclesModelTests: XCTestCase {
    private let decoder = APIClient.makeDecoder()

    func testTheFlagIsOffUnlessTheServerSendsIt() throws {
        let off = try decode(CurrentUser.self, #"{"id": 1, "approved": true}"#)
        XCTAssertFalse(off.circlesEnabled)
        XCTAssertFalse(UserCapabilities(user: off).circlesEnabled)
        let on = try decode(CurrentUser.self, #"{"id": 1, "approved": true, "circles_enabled": true}"#)
        XCTAssertTrue(on.circlesEnabled)
        XCTAssertTrue(UserCapabilities(user: on).circlesEnabled)
    }

    func testCirclesDecode() throws {
        let answer = try decoder.decode(CirclesResponse.self, from: try fixture("circles.json"))
        XCTAssertEqual(answer.circles.map(\.name), ["Close friends", "Family"])
        XCTAssertEqual(answer.circles[0].members.map(\.username), ["anna", "ben"])
        XCTAssertEqual(answer.circles[0].members.map(\.onlyNew), [false, true])
        XCTAssertEqual(answer.circles[0].sharedCount, 12)
        XCTAssertTrue(answer.circles[1].members.isEmpty)
        let preview = try decode(CircleMemberPreview.self,
                                 #"{"username":"dana","circle":"Close friends","already_member":false,"shared_count":12}"#)
        XCTAssertEqual(preview.username, "dana")
        XCTAssertEqual(preview.sharedCount, 12)
        XCTAssertFalse(preview.alreadyMember)
    }

    func testSharedWithMeDecodesAndOlderPagesJoinTheirPerson() throws {
        let page = try decoder.decode(SharedWithMe.self, from: try fixture("shared_with_me.json"))
        XCTAssertEqual(page.people.map(\.username), ["anna", "emeka"])
        XCTAssertEqual(page.people[0].entries.map(\.id), [31, 22])
        XCTAssertEqual(page.people[0].entries[0].lastReplyBy, "ben")
        XCTAssertNil(page.people[0].entries[1].lastReplyBy)
        XCTAssertTrue(page.people[1].entries[0].lastReplyByMe)
        XCTAssertEqual(page.hidden, ["dana"])
        XCTAssertEqual(page.nextBefore, 22)

        let older = [SharedWithMe.Person(username: "anna", entries: [.init(id: 7, title: "Older")]),
                     SharedWithMe.Person(username: "greta", entries: [.init(id: 5, title: "Oldest")])]
        let merged = SharedWithMe.merge(page.people, older: older)
        XCTAssertEqual(merged.map(\.username), ["anna", "emeka", "greta"])
        XCTAssertEqual(merged[0].entries.map(\.id), [31, 22, 7])
    }

    func testANodeCarriesItsAudience() throws {
        let node = try decoder.decode(NodeDetail.self, from: try fixture("node_audience.json"))
        XCTAssertEqual(node.privacyLevel, .circles)
        let audience = try XCTUnwrap(node.audience)
        XCTAssertEqual(audience.entryId, 31)
        XCTAssertEqual(audience.author, "anna")
        XCTAssertFalse(audience.isAuthor)
        XCTAssertEqual(audience.readers, ["ben", "chidi", "seowriter"])
        XCTAssertTrue(audience.isEntry)
        XCTAssertEqual(audience.entryAIUsage, .chat)
        XCTAssertNil(audience.circleIds, "a circle's id never reaches a reader")
        // A node that is not shared has none.
        let plain = try decoder.decode(NodeDetail.self, from: try fixture("node_detail.json"))
        XCTAssertNil(plain.audience)
        // The author's own entry carries what it is shared with.
        let own = try decode(NodeAudience.self, """
        {"entry_id":4,"author":"seowriter","is_author":true,"readers":["ben"],"is_entry":true,
         "entry_ai_usage":"none","circle_ids":[3],"usernames":["ben"]}
        """)
        XCTAssertEqual(own.circleIds, [3])
        XCTAssertEqual(own.usernames, ["ben"])
        XCTAssertEqual(own.entryAIUsage, .off)
    }

    func testTheShareNoteDecodes() throws {
        let payload = try decode(UpdatesPayload.self, """
        {"changelog":[],"polls":[],"notifications":[
          {"id":8,"type":"circle_share","title":"anna shared an entry with you.",
           "body":"You find it under Shared with you, whenever you want to.","link":"/node/31",
           "created_at":"2026-11-10T09:41:00Z","meta":null,"node_id":31,"actor_username":"anna"},
          {"id":9,"type":"profile_ready","title":"Profile updated","link":"/profile"}]}
        """)
        XCTAssertTrue(payload.notifications[0].isCircleShare)
        XCTAssertEqual(payload.notifications[0].nodeId, 31)
        XCTAssertEqual(payload.notifications[0].actorUsername, "anna")
        XCTAssertFalse(payload.notifications[1].isCircleShare)
        XCTAssertNil(payload.notifications[1].actorUsername)
    }

    func testRoutesAndPaths() {
        XCTAssertEqual(AppRoute.parse("/circles", environment: .local), .circles)
        XCTAssertEqual(AppRoute.parse("/shared", environment: .local), .sharedWithYou)
        XCTAssertEqual(AppRoute.circles.preferredTab, .more)
        XCTAssertEqual(AppRoute.sharedWithYou.webPath, "/shared")
        XCTAssertEqual(APIPath.canonical(APIPath.circles), "/api/circles")
        XCTAssertEqual(APIPath.circleMember(3, username: "ben"), "/api/circles/3/members/ben")
        XCTAssertEqual(APIPath.circlesMute(username: "anna"), "/api/circles/mutes/anna")
        XCTAssertEqual(APIPath.nodeAudience(9), "/api/nodes/9/audience")
        XCTAssertEqual(DefaultsKey.circleReplyAck(entryId: 31, userId: 5), "loore_circle_reply_ack_5_31")
    }
}

/// The sentences of `frontend/src/utils/circles.js` and the dialogs, word for word.
final class CirclesWordingTests: XCTestCase {
    private func audience(readers: [String], isAuthor: Bool = false, ai: AIUsage = .chat) -> NodeAudience {
        NodeAudience(entryId: 31, author: "anna", isAuthor: isAuthor, readers: readers, entryAIUsage: ai)
    }

    func testNames() {
        XCTAssertEqual(CirclesWording.joinNames([]), "")
        XCTAssertEqual(CirclesWording.joinNames(["anna"]), "anna")
        XCTAssertEqual(CirclesWording.joinNames(["anna", "ben"]), "anna and ben")
        XCTAssertEqual(CirclesWording.joinNames(["anna", "ben", "chidi"]), "anna, ben and chidi")
        XCTAssertEqual(CirclesWording.splitNames("@dana, @emeka  Dana ,,@@x"), ["dana", "emeka", "x"])
        XCTAssertEqual(CirclesWording.splitNames("   "), [])
        XCTAssertTrue(CirclesWording.endsAName("@dana, "))
        XCTAssertTrue(CirclesWording.endsAName(""))
        XCTAssertFalse(CirclesWording.endsAName("@da"))
        XCTAssertEqual(CirclesWording.countWord(2), "two")
        XCTAssertEqual(CirclesWording.countWord(12), "twelve")
        XCTAssertEqual(CirclesWording.countWord(13), "13")
    }

    func testTheReadersSentenceAndTheSaveButton() {
        XCTAssertEqual(CirclesWording.readersSentence(["anna", "ben", "chidi"]),
                       "anna, ben and chidi can read this entry and reply to it. Nobody else can.")
        XCTAssertEqual(CirclesWording.readersSentence([]), "")
        let circles = [PeopleCircle(id: 3, name: "Close friends"), PeopleCircle(id: 4, name: "Family")]
        XCTAssertEqual(CirclesWording.shareButtonLabel(circles: circles, circleIds: [3], names: [], readers: ["a", "b"]),
                       "Share with Close friends")
        XCTAssertEqual(CirclesWording.shareButtonLabel(circles: circles, circleIds: [3], names: ["dana"],
                                                       readers: ["a", "b", "c", "d", "e"]), "Share with 5 people")
        XCTAssertEqual(CirclesWording.shareButtonLabel(circles: circles, circleIds: [], names: ["dana"], readers: ["dana"]),
                       "Share with 1 person")
        XCTAssertEqual(CirclesWording.shareButtonLabel(circles: circles, circleIds: [3, 4], names: [], readers: []), "Share")
    }

    func testTheBannerNamesTheAuthorAndCountsTheOthers() {
        XCTAssertEqual(CirclesWording.sharedBannerText(audience(readers: ["ben", "chidi", "me"])),
                       "anna shared this with you and two other people. Only they can read it, and nobody but anna can make it public.")
        XCTAssertEqual(CirclesWording.sharedBannerText(audience(readers: ["ben", "me"])),
                       "anna shared this with you and one other person. Only they can read it, and nobody but anna can make it public.")
        XCTAssertEqual(CirclesWording.sharedBannerText(audience(readers: ["me"])),
                       "anna shared this with you. Only you and anna can read it, and nobody but anna can make it public.")
    }

    func testTheReadersListMarksTheAuthorAndTheViewer() {
        XCTAssertEqual(CirclesWording.readersListTitle, "Can read this entry and its replies")
        XCTAssertEqual(CirclesWording.readerLines(audience(readers: ["ben", "chidi", "me"]), me: "me"),
                       ["anna, the author", "ben", "chidi", "You"])
        XCTAssertEqual(CirclesWording.readerLines(audience(readers: ["ben"], isAuthor: true), me: "anna"),
                       ["You, the author", "ben"])
    }

    func testTheReplyDialogNamesEveryoneButTheWriter() {
        let a = audience(readers: ["ben", "chidi", "me"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(a, me: "me"), ["anna", "ben", "chidi"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(a, me: "anna"), ["ben", "chidi", "me"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(nil, me: "me"), [])
        XCTAssertEqual(CircleReplyDialog.title(askAI: false), "Send this reply?")
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna", "ben", "chidi"], askAI: false),
                       "anna, ben and chidi will see it, with your name.")
        XCTAssertEqual(CircleReplyDialog.confirmLabel(askAI: false), "Send reply")
        XCTAssertEqual(CircleReplyDialog.title(askAI: true), "Ask your AI here?")
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna"], askAI: true),
                       "Your AI reads this thread and answers in it. anna will see the answer, with your name.")
        XCTAssertEqual(CircleReplyDialog.confirmLabel(askAI: true), "Ask my AI")
    }

    func testAskMyAIIsOffWithItsReason() {
        XCTAssertNil(CirclesWording.askAIOffReason(audience(readers: ["me"]), myDefaultAIUsage: .chat))
        XCTAssertEqual(CirclesWording.askAIOffReason(audience(readers: ["me"], ai: .off), myDefaultAIUsage: .chat),
                       "anna set this entry's AI usage to None, so no AI can read it.")
        XCTAssertEqual(CirclesWording.askAIOffReason(audience(readers: ["me"]), myDefaultAIUsage: .off),
                       "Your own AI usage is set to None in Account settings.")
    }

    func testTheAddDialogStatesTheRealCount() {
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Close friends", sharedCount: 12),
                       "dana will be able to read the 12 entries you have already shared with Close friends, and every entry you share with it from now on.")
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Close friends", sharedCount: 1),
                       "dana will be able to read the entry you have already shared with Close friends, and every entry you share with it from now on.")
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Family", sharedCount: 0),
                       "dana will be able to read every entry you share with Family from now on. You have not shared anything with it yet.")
        XCTAssertEqual(CirclesWording.addMemberNote(name: "dana"),
                       "dana gets a note in Loore the first time there is something to read. You can remove dana at any time; the entries are closed to dana from that moment.")
        XCTAssertEqual(CirclesWording.deleteCircleBody(name: "Family", sharedCount: 0),
                       "The list of people is deleted. Nothing was shared with this circle.")
        XCTAssertEqual(CirclesWording.deleteCircleBody(name: "Family", sharedCount: 2),
                       "The 2 entries you shared with Family are closed to its people from this moment, unless you also shared them another way. The entries stay yours.")
    }

    func testSharedWithYouDatesAndLines() throws {
        let utc = try XCTUnwrap(TimeZone(identifier: "UTC"))
        let iso = ISO8601DateFormatter()
        let now = try XCTUnwrap(iso.date(from: "2026-11-10T15:00:00Z"))
        func at(_ s: String) -> Date? { iso.date(from: s) }
        XCTAssertEqual(CirclesWording.sharedDate(at("2026-11-10T09:40:00Z"), now: now, timeZone: utc), "Today, 9:40")
        XCTAssertEqual(CirclesWording.sharedDate(at("2026-10-28T18:05:00Z"), now: now, timeZone: utc), "28 October")
        XCTAssertEqual(CirclesWording.sharedDate(at("2025-11-02T10:00:00Z"), now: now, timeZone: utc), "2 November 2025")
        XCTAssertEqual(CirclesWording.sharedDate(nil, now: now, timeZone: utc), "")
        let replied = SharedWithMe.Entry(id: 1, title: "t", createdAt: at("2026-11-10T09:40:00Z"), lastReplyBy: "ben")
        XCTAssertEqual(CirclesWording.entryMeta(replied, now: now, timeZone: utc), "Today, 9:40. ben replied.")
        let mine = SharedWithMe.Entry(id: 1, title: "t", createdAt: at("2026-10-28T18:05:00Z"), lastReplyBy: "me",
                                      lastReplyByMe: true)
        XCTAssertEqual(CirclesWording.entryMeta(mine, now: now, timeZone: utc), "28 October. You replied.")
        XCTAssertEqual(SharedWithYouView.hideConfirm("anna"), "Don't show anna's entries")
        XCTAssertEqual(SharedWithYouView.nobodySees, "Nobody sees whether you have read an entry.")
    }

    func testThePickerChoices() {
        XCTAssertEqual(PrivacySelectorView.whoCanReadLabel, "Who can read this")
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: true).map(\.1), ["Only me", "Circles", "Everyone"])
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: true).map(\.0.rawString), ["private", "circles", "public"])
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: false).map(\.1), ["Only me", "Everyone"])
        XCTAssertEqual(PrivacySelectorView.threadNote(["anna", "ben"]),
                       "anna and ben will see it, with your name. Nobody else can.")
    }
}

/// The writing form and the thread with Circles on, against a stubbed backend.
@MainActor
final class CirclesFormTests: StubbedAppTestCase {
    static let circlesUserJSON = """
    {"id":5,"username":"seowriter","approved":true,"terms_up_to_date":true,"plan":"alpha","craft_mode":true,
     "preferred_model":"gpt-6-luna","default_privacy_level":"private","default_ai_usage":"chat",
     "circles_enabled":true,"voice_mode_enabled":true,"timezone":"UTC"}
    """

    override func setUp() async throws {
        try await super.setUp()
        let user = try decode(CurrentUser.self, Self.circlesUserJSON)
        app.useForTesting(api: makeStubbedClient(environment: .local), user: user)
    }

    private func form(_ config: NodeFormConfig) -> (NodeFormModel, () -> NodeFormResult?) {
        let model = NodeFormModel(config: config, app: app, defaults: defaults)
        var captured: NodeFormResult?
        model.onSuccess = { captured = $0 }
        return (model, { captured })
    }

    private func path(_ request: URLRequest) -> String { request.url?.path(percentEncoded: true) ?? "" }

    private var sharedParent: String {
        String(data: (try? fixture("node_audience.json")) ?? Data(), encoding: .utf8) ?? "{}"
    }

    func testANewEntryIsSharedWithItsCirclesAndNames() async {
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("POST", "/api/circles/audience-preview"): return .json(200, #"{"readers":["anna","ben","dana"]}"#)
            case ("POST", "/api/nodes/"): return .json(201, #"{"id":42,"privacy_level":"circles","audience_set":true}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(parentId: nil))
        model.content = "to my friends"
        model.privacy = .circles
        model.audience.circleIds = [3]
        model.audience.namesChanged("@dana")   // not yet looked up: the save checks it
        XCTAssertTrue(model.sharesEntry)
        await model.submit()
        XCTAssertEqual(result()?.id, 42)
        let sent = body(of: "POST /api/nodes/")
        XCTAssertEqual(sent?["privacy_level"] as? String, "circles")
        XCTAssertEqual(sent?["circle_ids"] as? [Int], [3])
        XCTAssertEqual(sent?["circle_usernames"] as? [String], ["dana"])
        XCTAssertEqual(body(of: "POST /api/circles/audience-preview")?["usernames"] as? [String], ["dana"])
        XCTAssertFalse(calls.contains("PUT /api/nodes/42/audience"), "the audience went with the entry")
    }

    func testSharingWithNobodyOrAnUnknownNameIsRefusedBeforeAnythingIsSaved() async {
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/circles/audience-preview" {
                let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
                if (sent?["usernames"] as? [String])?.contains("nobody") == true {
                    return .json(400, #"{"error":"No one on Loore has that name: @nobody","code":"circle_unknown_name"}"#)
                }
                return .json(200, #"{"readers":[]}"#)
            }
            return .json(200, "{}")
        }
        let (model, result) = form(NodeFormConfig(parentId: nil))
        model.content = "x"
        model.privacy = .circles
        await model.submit()
        XCTAssertEqual(model.error, CircleAudienceState.noReadersMessage)
        model.audience.namesChanged("@nobody")
        await model.submit()
        XCTAssertEqual(model.error, "No one on Loore has that name: @nobody")
        XCTAssertNil(result())
        XCTAssertFalse(calls.contains("POST /api/nodes/"))
    }

    func testAnEntryFromAPathWithoutAnAudienceIsSharedInASecondStep() async {
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("POST", "/api/circles/audience-preview"): return .json(200, #"{"readers":["ben"]}"#)
            case ("POST", "/api/textmode/start"): return .json(201, #"{"conversation_id":1,"user_node_id":2,"llm_node_id":3}"#)
            case ("PUT", "/api/nodes/2/audience"):
                return .json(200, #"{"id":2,"privacy_level":"circles","audience":null}"#)
            default: return .json(200, "{}")
            }
        }
        defaults.set(true, forKey: DefaultsKey.agenticReply)
        let (model, result) = form(NodeFormConfig(parentId: nil, allowAgenticPrompt: true))
        model.content = "agentic and shared"
        model.privacy = .circles
        model.audience.namesChanged("ben ")
        await model.submit()
        XCTAssertEqual(result()?.userNodeId, 2)
        let second = body(of: "PUT /api/nodes/2/audience")
        XCTAssertEqual(second?["circle_usernames"] as? [String], ["ben"])
        XCTAssertNil(second?["apply_to_descendants"])
    }

    func testAReplyInASharedThreadNamesItsReadersAndIsSentAsCircles() async {
        let parent = sharedParent
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("GET", "/api/nodes/31"): return .json(200, parent)
            case ("POST", "/api/nodes/"): return .json(201, #"{"id":50,"parent_id":31}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(parentId: 31, hidePowerFeatures: true))
        await model.start()
        XCTAssertEqual(model.threadAudienceNames, ["anna", "ben", "chidi"])
        XCTAssertTrue(model.lockPrivacy)
        XCTAssertFalse(model.sharesEntry)
        model.content = "grief makes sense to me"
        await model.submit()
        XCTAssertEqual(model.dialog, .circleReply)
        XCTAssertFalse(calls.contains("POST /api/nodes/"), "nothing is sent before the readers are named")
        model.answerCircleReply(true)
        await model.dialogSubmit?.value
        XCTAssertEqual(result()?.id, 50)
        let sent = body(of: "POST /api/nodes/")
        XCTAssertEqual(sent?["privacy_level"] as? String, "circles")
        XCTAssertNil(sent?["circle_ids"], "a reply has the thread's audience, none of its own")
        // "Don't ask again in this thread": remembered for this thread and user only.
        XCTAssertEqual(defaults.string(forKey: DefaultsKey.circleReplyAck(entryId: 31, userId: 5)), "true")
        let (again, _) = form(NodeFormConfig(parentId: 31, hidePowerFeatures: true))
        await again.start()
        again.content = "second reply"
        await again.submit()
        XCTAssertNil(again.dialog)
    }

    func testEditingASharedEntrySeedsThePickerAndSendsTheAudience() async {
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("GET", "/api/nodes/9/audience"):
                return .json(200, """
                {"audience":{"entry_id":9,"author":"seowriter","is_author":true,"readers":["ben"],"is_entry":true,
                 "entry_ai_usage":"chat","circle_ids":[3],"usernames":["ben"]},"thread_audience":null}
                """)
            case ("POST", "/api/circles/audience-preview"): return .json(200, #"{"readers":["ben","chidi"]}"#)
            case ("PUT", "/api/nodes/9"): return .json(200, #"{"node":{"id":9,"content":"same"},"descendants_updated":0}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "same", initialPrivacy: .circles,
                                                               initialAIUsage: .chat, hasChildren: true)))
        await model.start()
        XCTAssertEqual(model.audience.circleIds, [3])
        XCTAssertEqual(model.audience.namesText, "@ben")
        XCTAssertFalse(model.audience.changed)
        // Unchanged: saved without asking about the replies.
        await model.submit()
        XCTAssertNil(model.dialog)
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["circle_ids"] as? [Int], [3])
        // A changed audience on an entry with replies asks whether they follow.
        model.audience.namesChanged("@ben, @chidi ")
        XCTAssertTrue(model.audience.changed)
        await model.submit()
        XCTAssertEqual(model.dialog, .scope)
        model.answerScope(true)
        await model.dialogSubmit?.value
        XCTAssertNotNil(result())
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["circle_usernames"] as? [String], ["ben", "chidi"])
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["apply_to_descendants"] as? Bool, true)
    }

    func testWithCirclesOffTheFormIsAsBefore() async throws {
        let user = try decode(CurrentUser.self, StubbedAppTestCase.userJSON)
        app.useForTesting(api: makeStubbedClient(environment: .local), user: user)
        let parent = sharedParent
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("GET", "/api/nodes/31"): return .json(200, parent)
            case ("POST", "/api/nodes/"): return .json(201, #"{"id":50}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(parentId: 31))
        await model.start()
        XCTAssertNil(model.threadAudienceNames)
        XCTAssertFalse(model.sharesEntry)
        model.content = "hi"
        await model.submit()
        XCTAssertNil(model.dialog)
        XCTAssertEqual(result()?.id, 50)
        XCTAssertFalse(calls.contains("POST /api/circles/audience-preview"))
    }

    // MARK: The thread

    private func thread(_ json: String) async -> ThreadModel {
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/nodes/31" { return .json(200, json) }
            if path(request) == "/api/nodes/31/llm" { return .json(202, #"{"node_id":77,"status":"pending"}"#) }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 31, awaitLLM: nil, app: app)
        await model.load()
        return model
    }

    func testAReaderGetsAskMyAIAndNoAutomaticReply() async {
        let model = await thread(sharedParent)
        XCTAssertTrue(model.isSharedThread)
        XCTAssertTrue(model.isForeignShared)
        XCTAssertFalse(model.autoGenerateActive(true), "no model answers in a shared thread on its own")
        XCTAssertFalse(model.showCraftBar(craftMode: true, autoGenerate: false))
        XCTAssertNil(model.askAIOffReason)
        XCTAssertEqual(model.askAINames, ["anna", "ben", "chidi"])
        // The people are named first; the request then carries circle_ask and no model.
        let key = DefaultsKey.circleReplyAck(entryId: 31, userId: 5)
        UserDefaults.standard.removeObject(forKey: key)
        defer { UserDefaults.standard.removeObject(forKey: key) }
        model.askMyAIPressed()
        XCTAssertTrue(model.showAskAIDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
        model.askMyAIConfirmed(dontAskAgain: false)
        for _ in 0..<40 where !calls.contains("POST /api/nodes/31/llm") {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        let sent = body(of: "POST /api/nodes/31/llm")
        XCTAssertEqual(sent?["circle_ask"] as? Bool, true)
        XCTAssertNil(sent?["model"], "the reader's own model: the server picks it")
        XCTAssertNil(UserDefaults.standard.string(forKey: key))
    }

    func testAskMyAIIsOffWhenTheEntryIsNone() async {
        let model = await thread(sharedParent.replacingOccurrences(of: #""entry_ai_usage": "chat""#,
                                                                    with: #""entry_ai_usage": "none""#))
        XCTAssertEqual(model.askAIOffReason, "anna set this entry's AI usage to None, so no AI can read it.")
    }

    func testTheAuthorKeepsTheLLMResponseBarInTheirOwnSharedThread() async {
        let own = """
        {"id":31,"content":"mine","node_type":"user","user":{"id":5,"username":"seowriter"},
         "privacy_level":"circles","ai_usage":"chat","child_count":0,"children":[],"ancestors":[],
         "audience":{"entry_id":31,"author":"seowriter","is_author":true,"readers":["ben"],"is_entry":true,
                     "entry_ai_usage":"chat","circle_ids":[3],"usernames":[]}}
        """
        let model = await thread(own)
        XCTAssertTrue(model.isSharedThread)
        XCTAssertFalse(model.isForeignShared)
        XCTAssertFalse(model.autoGenerateActive(true))
        XCTAssertTrue(model.showCraftBar(craftMode: false, autoGenerate: true),
                      "like a public thread: the explicit bar, also without craft mode")
        XCTAssertNil(model.askAIOffReason)
    }
}
