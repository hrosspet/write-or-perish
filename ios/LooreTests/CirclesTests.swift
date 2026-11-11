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
        // What an AI reply here would read of the viewer's own: absent = nothing.
        XCTAssertEqual(audience.aiReads, [])
        XCTAssertEqual(own.aiReads, [])
        let reads = try decode(NodeAudience.self, """
        {"entry_id":4,"author":"anna","is_author":false,"readers":["seowriter"],"is_entry":true,
         "entry_ai_usage":"chat","ai_reads":["profile","memory","to-do list"]}
        """)
        XCTAssertEqual(reads.aiReads, ["profile", "memory", "to-do list"])
        XCTAssertEqual(reads.aiReadsKey, "", "absent: no key")
        let keyed = try decode(NodeAudience.self, #"{"entry_id":4,"author":"anna","ai_reads":["profile"],"ai_reads_key":"abc"}"#)
        XCTAssertEqual(keyed.aiReadsKey, "abc")
        XCTAssertEqual(try decode(NodeAudience.self, #"{"entry_id":4,"author":"anna","ai_reads_key":7}"#).aiReadsKey, "")
        let odd = try decode(NodeAudience.self, #"{"entry_id":4,"author":"anna","ai_reads":"profile"}"#)
        XCTAssertEqual(odd.aiReads, [])
        // The edit form's answer: the prompt the thread starts from, for the author.
        let answer = try decode(NodeAudienceAnswer.self, #"{"audience":null,"thread_audience":null,"thread_path":{"count":3,"shared":true}}"#)
        XCTAssertEqual(answer.threadPath?.count, 3)
        XCTAssertEqual(answer.threadPath?.shared, true)
        XCTAssertNil(try decode(NodeAudienceAnswer.self, #"{"audience":null,"thread_path":null}"#).threadPath)
    }

    func testAReaderOfASharedPromptSeesTheirOwnArtifacts() throws {
        // A prompt anna shared, as the server sends it to a reader: the
        // reader's own latest artifacts, no version numbers, and no key for
        // an artifact the reader does not have.
        let node = try decode(NodeDetail.self, """
        {"id":7,"content":"Reflect.\\n{user_profile}\\n{user_memory}","node_type":"user",
         "user":{"id":9,"username":"anna"},"privacy_level":"circles","ai_usage":"chat",
         "is_system_prompt":true,"prompt_title":"Reflect",
         "context_artifacts":{"profile":{"id":70,"content":"The reader's own profile."}}}
        """)
        let artifacts = node.systemPrompt.contextArtifacts
        XCTAssertEqual(ContentSegmenter.segments(node.content),
                       [.text("Reflect.\n"), .artifact(.profile), .text("\n"), .artifact(.memory)])
        // The reader's profile is embedded, without a version in its header.
        let profile = try XCTUnwrap(InlineArtifactSection.artifact(.profile, in: artifacts))
        XCTAssertEqual(profile.content, "The reader's own profile.")
        XCTAssertNil(profile.versionNumber)
        XCTAssertEqual(InlineArtifactSection.headerLabel(.profile, profile), "User Profile")
        // The reader has no memory: the placeholder itself is shown.
        XCTAssertNil(InlineArtifactSection.artifact(.memory, in: artifacts))
        XCTAssertEqual(InlineArtifactSection.missingText(.memory, forOwner: false), "{user_memory}")
        XCTAssertEqual(InlineArtifactSection.missingText(.aiPreferences, forOwner: false), "{user_ai_preferences}")
        // The owner's own view is as before.
        XCTAssertEqual(InlineArtifactSection.missingText(.memory, forOwner: true), "[Memory not available]")
        let pinned = try decode(ContextArtifacts.self, #"{"profile":{"id":1,"version_number":4,"content":"p"}}"#)
        XCTAssertEqual(InlineArtifactSection.headerLabel(.profile, try XCTUnwrap(pinned.profile)), "User Profile v4")
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
        // An AI reply has its own "Don't ask again" memory, and its own privacy choice.
        XCTAssertEqual(DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5), "loore_circle_ai_reply_ack_5_31")
        XCTAssertEqual(DefaultsKey.circleAiReplyPrivacy(entryId: 31, userId: 5), "loore_circle_ai_reply_privacy_5_31")
    }

    func testAnAIReplyInASharedThreadCarriesItsLabel() throws {
        let node = try decoder.decode(NodeDetail.self, from: try fixture("node_shared_ai_reply.json"))
        XCTAssertEqual(node.llmLabel, "Claude Fable 5.2 via @ben")
        XCTAssertEqual(node.humanOwnerUsername, "ben")
        XCTAssertEqual(node.ancestors[0].llmLabel, nil)
        let child = try XCTUnwrap(node.children.first)
        XCTAssertEqual(child.llmLabel, "GPT-6 Luna via @seowriter")
        XCTAssertEqual(child.humanOwnerUsername, "seowriter")
        XCTAssertEqual(BubbleData(child).llmLabel, "GPT-6 Luna via @seowriter")
        // The label stands in place of the model id in the footer.
        XCTAssertEqual(NodeFooterText.author(username: "anna", llmModel: node.llmModel, llmLabel: node.llmLabel,
                                             humanOwnerUsername: node.humanOwnerUsername, isPublic: false),
                       "Claude Fable 5.2 via @ben")
        // Without a label (a private or public thread, an older server): as before.
        XCTAssertEqual(NodeFooterText.author(username: "anna", llmModel: "claude-opus-5.5", llmLabel: nil,
                                             humanOwnerUsername: "anna", isPublic: false), "claude-opus-5.5")
        XCTAssertEqual(NodeFooterText.author(username: "anna", llmModel: "claude-opus-5.5", llmLabel: "",
                                             humanOwnerUsername: "anna", isPublic: true), "claude-opus-5.5 · via anna")
        XCTAssertEqual(NodeFooterText.author(username: "anna", llmModel: nil, llmLabel: nil,
                                             humanOwnerUsername: nil, isPublic: false), "anna")
        // A label of the wrong type is dropped; the node still decodes.
        let odd = try decode(TreeNode.self, #"{"id":3,"node_type":"llm","llm_model":"m","llm_label":7}"#)
        XCTAssertNil(odd.llmLabel)
        XCTAssertEqual(odd.llmModel, "m")
        let card = try decode(LogCard.self, #"{"id":3,"llm_model":"m","llm_label":"M via @ben","human_owner_username":"ben"}"#)
        XCTAssertEqual(card.llmLabel, "M via @ben")
    }
}

/// The sentences of `frontend/src/utils/circles.js` and the dialogs, word for word.
final class CirclesWordingTests: XCTestCase {
    private func audience(readers: [String], isAuthor: Bool = false, ai: AIUsage = .chat) -> NodeAudience {
        NodeAudience(entryId: 31, author: "anna", isAuthor: isAuthor, readers: readers, entryAIUsage: ai)
    }

    func testNames() {
        XCTAssertEqual(CirclesWording.joinNames([]), "")
        XCTAssertEqual(CirclesWording.joinNames(["ben"]), "@ben")
        XCTAssertEqual(CirclesWording.joinNames(["anna", "ben"]), "@anna and @ben")
        XCTAssertEqual(CirclesWording.joinNames(["ben", "chidi", "dana"]), "@ben, @chidi and @dana")
        // A name is printed with one "@", also when it already has one.
        XCTAssertEqual(CirclesWording.joinNames(["@ben", "@@chidi", ""]), "@ben and @chidi")
        XCTAssertEqual(CirclesWording.at("ben"), "@ben")
        XCTAssertEqual(CirclesWording.at("@ben"), "@ben")
        XCTAssertEqual(CirclesWording.at(""), "")
        XCTAssertEqual(CirclesWording.possessive("anna"), "@anna's")
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
                       "@anna, @ben and @chidi can read this entry and reply to it. Nobody else can.")
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
                       "@anna shared this with you and two other people. Only they can read it, and nobody but @anna can make it public.")
        XCTAssertEqual(CirclesWording.sharedBannerText(audience(readers: ["ben", "me"])),
                       "@anna shared this with you and one other person. Only they can read it, and nobody but @anna can make it public.")
        XCTAssertEqual(CirclesWording.sharedBannerText(audience(readers: ["me"])),
                       "@anna shared this with you. Only you and @anna can read it, and nobody but @anna can make it public.")
    }

    func testTheReadersListMarksTheAuthorAndTheViewer() {
        XCTAssertEqual(CirclesWording.readersListTitle, "Can read this entry and its replies")
        XCTAssertEqual(CirclesWording.readerLines(audience(readers: ["ben", "chidi", "me"]), me: "me"),
                       ["@anna, the author", "@ben", "@chidi", "You"])
        XCTAssertEqual(CirclesWording.readerLines(audience(readers: ["ben"], isAuthor: true), me: "anna"),
                       ["You, the author", "@ben"])
    }

    func testTheReplyDialogNamesEveryoneButTheWriter() {
        let a = audience(readers: ["ben", "chidi", "me"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(a, me: "me"), ["anna", "ben", "chidi"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(a, me: "anna"), ["ben", "chidi", "me"])
        XCTAssertEqual(CirclesWording.replyAudienceNames(nil, me: "me"), [])
        XCTAssertEqual(CircleReplyDialog.title(aiReply: false), "Send this reply?")
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna", "ben", "chidi"], aiReply: false),
                       "@anna, @ben and @chidi will see it, with your name.")
        XCTAssertEqual(CircleReplyDialog.confirmLabel(aiReply: false), "Send reply")
    }

    func testTheDialogBeforeAnAIReply() {
        XCTAssertEqual(CircleReplyDialog.title(aiReply: true), "Generate an AI reply?")
        // The thread's prompt reads none of the requester's artifacts: one sentence.
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna", "ben"], aiReply: true),
                       "@anna and @ben will see it, marked with your name.")
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna"], aiReply: true, aiReads: []),
                       "@anna will see it, marked with your name.")
        XCTAssertNil(CirclesWording.aiReadsSentence([]))
        // It reads some: they are named, without "@".
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna"], aiReply: true, aiReads: ["profile", "memory", "to-do list"]),
                       "@anna will see it, marked with your name. Your AI reads your profile, memory and to-do list for this reply and could share private information from them.")
        XCTAssertEqual(CirclesWording.aiReadsSentence(["profile"]),
                       "Your AI reads your profile for this reply and could share private information from them.")
        XCTAssertEqual(CirclesWording.joinPlain(["profile", "memory"]), "profile and memory")
        XCTAssertEqual(CirclesWording.aiReadsSentence(["list of artifacts", "anything else its tools look up (your artifacts, to-do list and writing)"]),
                       "Your AI reads your list of artifacts and anything else its tools look up (your artifacts, to-do list and writing) for this reply and could share private information from them.")
        XCTAssertEqual(CirclesWording.aiReplyPrivateDialogBody(aiReads: []), "Only you will see it.")
        // The share note's title is the server's, printed as it comes: no second "@".
        XCTAssertEqual(CirclesWording.at("@anna"), "@anna")
        // A typed reply's dialog never carries the sentence.
        XCTAssertEqual(CircleReplyDialog.body(names: ["anna"], aiReply: false, aiReads: ["profile"]),
                       "@anna will see it, with your name.")
        XCTAssertEqual(CirclesWording.sharePathLabel, "Share also the discussion leading up to this node?")
        XCTAssertEqual(CirclesWording.sharePathNote,
                       "Everything from the start of this thread down to this entry is then shared with the same people, including the thread's prompt and the AI replies on the way. People's AI replies use the prompt with their own artifacts, never yours. Without it, only this entry is shared.")
        XCTAssertEqual(CircleReplyDialog.confirmLabel(aiReply: true), "Generate")
        XCTAssertEqual(CirclesWording.aiReplyPrivacyLabel, "AI reply:")
        XCTAssertEqual(CirclesWording.aiReplyShared, "Shared with this thread")
        XCTAssertEqual(CirclesWording.aiReplyOnlyMe, "Only me")
        XCTAssertEqual(CirclesWording.foreignSharedNote,
                       "Before your reply is sent, Loore names the people who will see it. No AI reply is generated here unless you press LLM Response.")
    }

    func testTheAIReplyControlIsOffWithItsReason() {
        let reader = audience(readers: ["me"])
        XCTAssertNil(CirclesWording.aiReplyOffReason(reader, nodeAIUsage: .chat, myDefaultAIUsage: .chat))
        XCTAssertEqual(CirclesWording.aiReplyOffReason(audience(readers: ["me"], ai: .off), nodeAIUsage: .chat,
                                                       myDefaultAIUsage: .chat),
                       "@anna set this entry's AI usage to None, so no AI can read it.")
        XCTAssertEqual(CirclesWording.aiReplyOffReason(audience(readers: ["me"], isAuthor: true, ai: .off),
                                                       nodeAIUsage: .off, myDefaultAIUsage: .chat),
                       "This entry's AI usage is None, so no AI can read it.")
        // The node the reply would answer is closed to AI.
        XCTAssertEqual(CirclesWording.aiReplyOffReason(reader, nodeAIUsage: .off, myDefaultAIUsage: .chat),
                       "This entry's AI usage is None, so no AI can read it.")
        XCTAssertEqual(CirclesWording.aiReplyOffReason(reader, nodeAIUsage: .chat, myDefaultAIUsage: .off),
                       "Your own AI usage is set to None in Account settings.")
    }

    func testTheAddDialogAddsForNewEntriesAndStatesTheRealCount() {
        XCTAssertEqual(CirclesWording.addMemberTitle(name: "dana", circle: "Close friends"), "Add @dana to Close friends?")
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Close friends", sharedCount: 12),
                       "@dana will read every entry you share with Close friends from now on. The 12 entries you have already shared with it stay closed to @dana unless you choose \"Also show earlier entries\".")
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Close friends", sharedCount: 1),
                       "@dana will read every entry you share with Close friends from now on. The entry you have already shared with it stays closed to @dana unless you choose \"Also show earlier entries\".")
        XCTAssertEqual(CirclesWording.addMemberBody(name: "dana", circle: "Family", sharedCount: 0),
                       "@dana will read every entry you share with Family from now on. You have not shared anything with it yet.")
        // The emphasized button adds for new entries only; the plain one opens the earlier ones too.
        XCTAssertEqual(CirclesWording.addMemberConfirm(name: "dana"), "Add @dana")
        XCTAssertEqual(CirclesWording.addMemberAlsoEarlier, "Also show earlier entries")
        XCTAssertFalse(AddCircleMemberDialog.emphasizedIncludesExisting)
        XCTAssertTrue(AddCircleMemberDialog.plainIncludesExisting)
        XCTAssertTrue(AddCircleMemberDialog.offersEarlier(sharedCount: 1))
        XCTAssertFalse(AddCircleMemberDialog.offersEarlier(sharedCount: 0))
        XCTAssertEqual(CirclesWording.addMemberNote(name: "dana"),
                       "@dana gets a note in Loore the first time there is something to read. You can remove @dana at any time; the entries are closed to @dana from that moment.")
        XCTAssertEqual(CirclesWording.deleteCircleBody(name: "Family", sharedCount: 0),
                       "The list of people is deleted. Nothing was shared with this circle.")
        XCTAssertEqual(CirclesWording.deleteCircleBody(name: "Family", sharedCount: 2),
                       "The 2 entries you shared with Family are closed to its people from this moment, unless you also shared them another way. The entries stay yours.")
        XCTAssertEqual(CirclesWording.removeMemberBody(name: "ben", circleName: "Close friends"),
                       "From this moment @ben no longer reads your entries through Close friends. An entry you also shared with @ben another way (another circle @ben is in, or by name) stays open to @ben. @ben is not told.")
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
        XCTAssertEqual(CirclesWording.entryMeta(replied, now: now, timeZone: utc), "Today, 9:40. @ben replied.")
        let mine = SharedWithMe.Entry(id: 1, title: "t", createdAt: at("2026-10-28T18:05:00Z"), lastReplyBy: "me",
                                      lastReplyByMe: true)
        XCTAssertEqual(CirclesWording.entryMeta(mine, now: now, timeZone: utc), "28 October. You replied.")
        XCTAssertEqual(SharedWithYouView.hideConfirm("anna"), "Don't show @anna's entries")
        XCTAssertEqual(SharedWithYouView.hideTitle("anna"), "Stop showing @anna's entries?")
        XCTAssertEqual(SharedWithYouView.hideBody("anna"),
                       "You will get no notes about @anna's entries and will not see them here. @anna is not told what you choose. You can undo this at the bottom of this page.")
        XCTAssertEqual(SharedWithYouView.nobodySees, "Nobody sees whether you have read an entry.")
    }

    func testThePickerChoices() {
        XCTAssertEqual(PrivacySelectorView.whoCanReadLabel, "Who can read this")
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: true).map(\.1), ["Only me", "Circles", "Everyone"])
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: true).map(\.0.rawString), ["private", "circles", "public"])
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: false).map(\.1), ["Only me", "Everyone"])
        XCTAssertEqual(PrivacySelectorView.threadNote(["anna", "ben"]),
                       "@anna and @ben will see it, with your name. Nobody else can.")
        // An AI reply in a shared thread: Only me or the thread's people, never Everyone.
        XCTAssertEqual(PrivacySelectorView.choices(allowCircles: true, allowPublic: false).map(\.1), ["Only me", "Circles"])
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

    private func pathEdit(shared: Bool?) -> (URLRequest) -> StubResponse {
        let threadPath = shared.map { #"{"count":2,"shared":\#($0)}"# } ?? "null"
        return { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("GET", "/api/nodes/9/audience"):
                return .json(200, """
                {"audience":{"entry_id":9,"author":"seowriter","is_author":true,"readers":["ben"],"is_entry":true,
                 "entry_ai_usage":"chat","circle_ids":[3],"usernames":[]},"thread_audience":null,"thread_path":\(threadPath)}
                """)
            case ("POST", "/api/circles/audience-preview"): return .json(200, #"{"readers":["ben"]}"#)
            case ("PUT", "/api/nodes/9"): return .json(200, #"{"node":{"id":9,"content":"same"},"descendants_updated":0}"#)
            default: return .json(200, "{}")
            }
        }
    }

    private func editForm() -> NodeFormModel {
        form(NodeFormConfig(edit: .init(nodeId: 9, initialContent: "same", initialPrivacy: .circles,
                                        initialAIUsage: .chat))).0
    }

    func testTheDiscussionAboveAnEntryIsSharedOnlyWhenChosen() async {
        StubURLProtocol.install(pathEdit(shared: false))
        let model = editForm()
        await model.start()
        XCTAssertTrue(model.audience.offersSharePath)
        XCTAssertFalse(model.audience.sharePath, "off by default: sharing an entry does not share what is above it")
        await model.submit()
        let plain = body(of: "PUT /api/nodes/9")
        XCTAssertNotNil(plain)
        XCTAssertNil(plain?["share_path"], "not shared before and not chosen: the flag is left out")
        model.audience.sharePath = true
        await model.submit()
        let sent = body(of: "PUT /api/nodes/9")
        XCTAssertEqual(sent?["share_path"] as? Bool, true)
        XCTAssertEqual(sent?["circle_ids"] as? [Int], [3])
    }

    func testASharedPathShowsOnAndCanBeTakenBack() async {
        StubURLProtocol.install(pathEdit(shared: true))
        let model = editForm()
        await model.start()
        XCTAssertTrue(model.audience.sharePath)
        await model.submit()
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["share_path"] as? Bool, true)
        model.audience.sharePath = false
        await model.submit()
        XCTAssertEqual(body(of: "PUT /api/nodes/9")?["share_path"] as? Bool, false)
    }

    func testAnEntryWithNothingAboveItIsNotAsked() async {
        StubURLProtocol.install(pathEdit(shared: nil))
        let model = editForm()
        await model.start()
        XCTAssertFalse(model.audience.offersSharePath)
        await model.submit()
        let sent = body(of: "PUT /api/nodes/9")
        XCTAssertNotNil(sent)
        XCTAssertNil(sent?["share_path"])
        // A new entry without a parent is not asked either.
        let (fresh, _) = form(NodeFormConfig(parentId: nil))
        await fresh.start()
        XCTAssertFalse(fresh.audience.offersSharePath)
    }

    func testANewEntryUnderTheUsersOwnNodeCanTakeTheDiscussionAlong() async {
        let parentJSON = """
        {"id":20,"content":"earlier","node_type":"user","user":{"id":5,"username":"seowriter"},
         "privacy_level":"private","ai_usage":"chat","child_count":0,"children":[],"ancestors":[]}
        """
        StubURLProtocol.install { [self] request in
            switch (request.httpMethod ?? "", path(request)) {
            case ("GET", "/api/nodes/20"): return .json(200, parentJSON)
            case ("POST", "/api/circles/audience-preview"): return .json(200, #"{"readers":["ben"]}"#)
            case ("POST", "/api/nodes/"):
                let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
                if (sent?["content"] as? String) == "refused" {
                    return .json(400, #"{"error":"A prompt cannot be shared on its own.","code":"circle_prompt_node"}"#)
                }
                return .json(201, #"{"id":21,"parent_id":20,"privacy_level":"circles","audience_set":true}"#)
            default: return .json(200, "{}")
            }
        }
        let (model, result) = form(NodeFormConfig(parentId: 20))
        await model.start()
        XCTAssertTrue(model.audience.offersSharePath)
        XCTAssertFalse(model.audience.sharePath)
        model.content = "the one I share"
        model.privacy = .circles
        model.audience.circleIds = [3]
        model.audience.sharePath = true
        await model.submit()
        XCTAssertEqual(result()?.id, 21)
        let sent = body(of: "POST /api/nodes/")
        XCTAssertEqual(sent?["share_path"] as? Bool, true)
        XCTAssertEqual(sent?["circle_ids"] as? [Int], [3])
        // The server's refusal is shown in its own words.
        let (refused, _) = form(NodeFormConfig(parentId: 20))
        await refused.start()
        refused.content = "refused"
        refused.privacy = .circles
        refused.audience.circleIds = [3]
        await refused.submit()
        XCTAssertEqual(refused.error, "A prompt cannot be shared on its own.")
    }

    func testAReplyUnderSomeoneElsesNodeIsNotAsked() async {
        let parent = sharedParent
        StubURLProtocol.install { [self] request in
            path(request) == "/api/nodes/31" ? .json(200, parent) : .json(200, "{}")
        }
        let (model, _) = form(NodeFormConfig(parentId: 31))
        await model.start()
        XCTAssertFalse(model.audience.offersSharePath, "a reply in a shared thread has the thread's people")
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

    private func waitFor(_ call: String) async {
        for _ in 0..<40 where !calls.contains(call) {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
    }

    private func waitForDialog(_ model: ThreadModel) async {
        for _ in 0..<40 where !model.showAIReplyDialog {
            try? await Task.sleep(nanoseconds: 25_000_000)
        }
    }

    private func clearAIReplyMemory() {
        for key in [DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5), DefaultsKey.circleReplyAck(entryId: 31, userId: 5),
                    DefaultsKey.circleAiReplyPrivacy(entryId: 31, userId: 5)] {
            UserDefaults.standard.removeObject(forKey: key)
        }
    }

    func testAReaderGeneratesAnAIReplyWithTheNormalRequest() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        // "Don't ask again" for typed replies does not skip the AI dialog.
        UserDefaults.standard.set("true", forKey: DefaultsKey.circleReplyAck(entryId: 31, userId: 5))
        let model = await thread(sharedParent)
        XCTAssertTrue(model.isSharedThread)
        XCTAssertTrue(model.isForeignShared)
        XCTAssertFalse(model.isOwner, "the entry is anna's")
        XCTAssertFalse(model.autoGenerateActive(true), "no AI reply is generated automatically for a reader")
        // The control shows for a reader on someone else's node, with or without Craft mode.
        XCTAssertTrue(model.showCraftBar(craftMode: true, autoGenerate: true))
        XCTAssertTrue(model.showCraftBar(craftMode: false, autoGenerate: true))
        XCTAssertNil(model.aiReplyOffReason)
        XCTAssertEqual(model.aiReplyNames, ["anna", "ben", "chidi"])
        // The people are named first.
        model.aiReplyPressed()
        await waitForDialog(model)
        XCTAssertTrue(model.showAIReplyDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
        model.aiReplyConfirmed(dontAskAgain: true)
        await waitFor("POST /api/nodes/31/llm")
        XCTAssertFalse(model.showAIReplyDialog)
        let sent = body(of: "POST /api/nodes/31/llm")
        XCTAssertNil(sent?["circle_ask"], "the normal request: no circle_ask")
        XCTAssertEqual(sent?["model"] as? String, "gpt-6-luna", "the requester's own model")
        XCTAssertEqual(sent?["source_mode"] as? String, "textmode")
        XCTAssertNil(sent?["privacy_level"], "shared with the thread: the server gives it the thread's audience")
        XCTAssertNil(sent?["ai_reads_ack"], "the thread has no ai_reads_key: nothing to acknowledge")
        // Without a key "Don't ask again" stores a marker, and the next reply is not asked about.
        XCTAssertEqual(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)), "1")
        let again = await thread(sharedParent)
        let go = await again.confirmAIReply()
        XCTAssertEqual(go, ThreadModel.AIReplyGo(ack: nil))
        XCTAssertFalse(again.showAIReplyDialog)
    }

    /// The shared entry with a prompt someone else shared that fills in the reader's data.
    private func keyedParent(_ key: String, reads: String = #"["profile"]"#) -> String {
        sharedParent.replacingOccurrences(of: #""entry_ai_usage": "chat""#,
                                          with: #""entry_ai_usage": "chat", "ai_reads": \#(reads), "ai_reads_key": "\#(key)""#)
    }

    func testTheAcknowledgedKeyGoesWithTheRequest() async throws {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        let model = await thread(keyedParent("k1"))
        XCTAssertEqual(model.audience?.aiReadsKey, "k1")
        XCTAssertEqual(model.audience?.aiReads, ["profile"])
        model.aiReplyPressed()
        await waitForDialog(model)
        XCTAssertTrue(model.showAIReplyDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"), "no key is sent before the user saw the list")
        model.aiReplyConfirmed(dontAskAgain: true)
        await waitFor("POST /api/nodes/31/llm")
        XCTAssertEqual(body(of: "POST /api/nodes/31/llm")?["ai_reads_ack"] as? String, "k1")
        // "Don't ask again" stores the key that was shown, not a yes.
        XCTAssertEqual(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)), "k1")
        // The same key later: not asked, and the stored key is sent.
        let same = await thread(keyedParent("k1"))
        let go = await same.confirmAIReply()
        XCTAssertEqual(go, ThreadModel.AIReplyGo(ack: "k1"))
        XCTAssertFalse(same.showAIReplyDialog)
        XCTAssertEqual(same.llmRequestBody(ack: go?.ack)["ai_reads_ack"], .string("k1"))
    }

    func testAStoredKeyThatDiffersShowsTheDialogAgain() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        let key = DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)
        for stored in ["k1", "1", "true"] {
            UserDefaults.standard.set(stored, forKey: key)
            let model = await thread(keyedParent("k2", reads: #"["profile","memory"]"#))
            model.aiReplyPressed()
            await waitForDialog(model)
            XCTAssertTrue(model.showAIReplyDialog, "stored \(stored) is not the thread's key")
            XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
            model.showAIReplyDialog = false
        }
        XCTAssertTrue(ThreadModel.aiReplyAcknowledged(stored: "k2", key: "k2"))
        XCTAssertFalse(ThreadModel.aiReplyAcknowledged(stored: "k1", key: "k2"))
        XCTAssertFalse(ThreadModel.aiReplyAcknowledged(stored: nil, key: ""))
        // A marker (or an older key) skips the dialog only when the thread has no key.
        XCTAssertTrue(ThreadModel.aiReplyAcknowledged(stored: "1", key: ""))
        XCTAssertFalse(ThreadModel.aiReplyAcknowledged(stored: "1", key: "k2"))
    }

    func testAChangedListReopensTheDialogAndTheRetryCarriesTheNewKey() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        // The user ticked "Don't ask again" for k1; the server's list has changed since.
        UserDefaults.standard.set("k1", forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5))
        let parent = keyedParent("k1")
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/nodes/31" { return .json(200, parent) }
            if path(request) == "/api/nodes/31/llm" {
                let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
                if (sent?["ai_reads_ack"] as? String) != "k2" {
                    return .json(409, """
                    {"code":"circle_ai_reads_changed","error":"What your AI reads for this reply has changed.",
                     "ai_reads":["profile","whole archive of writing"],"ai_reads_key":"k2"}
                    """)
                }
                return .json(202, #"{"node_id":77,"status":"pending"}"#)
            }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 31, awaitLLM: nil, app: app)
        await model.load()
        model.aiReplyPressed()          // k1 is stored: no dialog, the request carries k1
        await waitForDialog(model)      // 409: the dialog comes with the new list
        XCTAssertTrue(model.showAIReplyDialog)
        XCTAssertEqual(model.audience?.aiReads, ["profile", "whole archive of writing"])
        XCTAssertEqual(model.audience?.aiReadsKey, "k2")
        XCTAssertEqual(body(of: "POST /api/nodes/31/llm")?["ai_reads_ack"] as? String, "k1")
        XCTAssertEqual(calls.filter { $0 == "POST /api/nodes/31/llm" }.count, 1, "no retry before the user agrees")
        XCTAssertTrue(app.toasts.toasts.isEmpty, "this answer is not an error to show")
        XCTAssertEqual(CircleReplyDialog.body(names: model.aiReplyNames, aiReply: true, aiReads: model.audience?.aiReads ?? []),
                       "@anna, @ben and @chidi will see it, marked with your name. Your AI reads your profile and whole archive of writing for this reply and could share private information from them.")
        model.aiReplyConfirmed(dontAskAgain: false)
        for _ in 0..<40 where calls.filter({ $0 == "POST /api/nodes/31/llm" }).count < 2 {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        XCTAssertEqual(body(of: "POST /api/nodes/31/llm")?["ai_reads_ack"] as? String, "k2")
        XCTAssertFalse(model.llmRequesting)
        // Not ticked this time: the stored key stays the old one, so the next reply asks again.
        XCTAssertEqual(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)), "k1")
    }

    func testCancellingAfterAChangedListSendsNoSecondRequest() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        UserDefaults.standard.set("k1", forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5))
        let parent = keyedParent("k1")
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/nodes/31" { return .json(200, parent) }
            if path(request) == "/api/nodes/31/llm" {
                return .json(409, #"{"code":"circle_ai_reads_changed","error":"changed","ai_reads":["memory"],"ai_reads_key":"k2"}"#)
            }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 31, awaitLLM: nil, app: app)
        await model.load()
        model.aiReplyPressed()
        await waitForDialog(model)
        model.showAIReplyDialog = false
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(calls.filter { $0 == "POST /api/nodes/31/llm" }.count, 1)
        XCTAssertFalse(model.llmRequesting)
    }

    func testCancellingTheAIDialogSendsNothing() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        let model = await thread(sharedParent)
        model.aiReplyPressed()
        await waitForDialog(model)
        model.showAIReplyDialog = false   // Cancel, or the dialog closed any other way
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
        XCTAssertNil(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)))
    }

    func testOnlyMeSendsAPrivateReplyWithoutTheDialog() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        let model = await thread(sharedParent)
        XCTAssertFalse(model.aiReplyOnlyMe, "shared with the thread by default")
        XCTAssertTrue(model.showAIReplyPrivacy(craftBar: true, autoGenerate: false))
        model.setAIReplyOnlyMe(true)
        XCTAssertEqual(model.llmRequestBody()["privacy_level"], .string("private"))
        model.aiReplyPressed()
        await waitFor("POST /api/nodes/31/llm")
        XCTAssertFalse(model.showAIReplyDialog, "only the requester reads it: nobody to name")
        let sent = body(of: "POST /api/nodes/31/llm")
        XCTAssertEqual(sent?["privacy_level"] as? String, "private")
        XCTAssertNil(sent?["circle_ask"])
        // Remembered for this thread and user.
        let again = await thread(sharedParent)
        XCTAssertTrue(again.aiReplyOnlyMe)
        again.setAIReplyOnlyMe(false)
        XCTAssertNil(again.llmRequestBody()["privacy_level"])
        XCTAssertNil(body(of: "POST /api/nodes/31/llm")?["ai_reads_ack"], "no key in this thread: no ack")
        XCTAssertEqual(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyPrivacy(entryId: 31, userId: 5)), "shared")
    }

    func testOnlyMeWithAKeyIsAskedAboutAndSendsTheAck() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        // A prompt someone else shared reads the requester's data for a private reply too.
        let model = await thread(keyedParent("k1", reads: #"["profile","memory"]"#))
        model.setAIReplyOnlyMe(true)
        model.aiReplyPressed()
        await waitForDialog(model)
        XCTAssertTrue(model.showAIReplyDialog)
        XCTAssertTrue(model.aiReplyDialogOnlyMe, "the private variant")
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
        XCTAssertEqual(CircleReplyDialog.title(aiReply: true), "Generate an AI reply?")
        XCTAssertEqual(CircleReplyDialog.body(names: model.aiReplyNames, aiReply: true, aiReads: model.audience?.aiReads ?? [],
                                              onlyMe: model.aiReplyDialogOnlyMe),
                       "Only you will see it. Your AI reads your profile and memory for this reply, as the prompt shared in this thread asks.")
        XCTAssertEqual(CircleReplyDialog.confirmLabel(aiReply: true), "Generate")
        model.aiReplyConfirmed(dontAskAgain: true)
        await waitFor("POST /api/nodes/31/llm")
        let sent = body(of: "POST /api/nodes/31/llm")
        XCTAssertEqual(sent?["privacy_level"] as? String, "private")
        XCTAssertEqual(sent?["ai_reads_ack"] as? String, "k1")
        // The same stored key as for shared replies: neither kind is asked about again.
        XCTAssertEqual(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)), "k1")
        let again = await thread(keyedParent("k1"))
        let privateGo = await again.confirmAIReply()
        XCTAssertEqual(privateGo, ThreadModel.AIReplyGo(ack: "k1"))
        again.setAIReplyOnlyMe(false)
        let sharedGo = await again.confirmAIReply()
        XCTAssertEqual(sharedGo, ThreadModel.AIReplyGo(ack: "k1"))
        XCTAssertFalse(again.showAIReplyDialog)
    }

    func testAChangedListReopensThePrivateDialogToo() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        UserDefaults.standard.set("k1", forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5))
        UserDefaults.standard.set("private", forKey: DefaultsKey.circleAiReplyPrivacy(entryId: 31, userId: 5))
        let parent = keyedParent("k1")
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/nodes/31" { return .json(200, parent) }
            if path(request) == "/api/nodes/31/llm" {
                let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
                if (sent?["ai_reads_ack"] as? String) != "k2" {
                    return .json(409, #"{"code":"circle_ai_reads_changed","error":"changed","ai_reads":["memory"],"ai_reads_key":"k2"}"#)
                }
                return .json(202, #"{"node_id":77,"status":"pending"}"#)
            }
            return .json(200, "{}")
        }
        let model = ThreadModel(nodeId: 31, awaitLLM: nil, app: app)
        await model.load()
        XCTAssertTrue(model.aiReplyPrivate)
        model.aiReplyPressed()
        await waitForDialog(model)
        XCTAssertTrue(model.aiReplyDialogOnlyMe)
        XCTAssertEqual(CircleReplyDialog.body(names: [], aiReply: true, aiReads: model.audience?.aiReads ?? [], onlyMe: true),
                       "Only you will see it. Your AI reads your memory for this reply, as the prompt shared in this thread asks.")
        model.aiReplyConfirmed(dontAskAgain: false)
        for _ in 0..<40 where calls.filter({ $0 == "POST /api/nodes/31/llm" }).count < 2 {
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        let sent = body(of: "POST /api/nodes/31/llm")
        XCTAssertEqual(sent?["ai_reads_ack"] as? String, "k2")
        XCTAssertEqual(sent?["privacy_level"] as? String, "private")
        XCTAssertTrue(app.toasts.toasts.isEmpty)
    }

    func testTheAIReplyControlIsOffWhenTheEntryIsNone() async {
        clearAIReplyMemory()
        let model = await thread(sharedParent.replacingOccurrences(of: #""entry_ai_usage": "chat""#,
                                                                    with: #""entry_ai_usage": "none""#))
        XCTAssertEqual(model.aiReplyOffReason, "@anna set this entry's AI usage to None, so no AI can read it.")
        XCTAssertTrue(model.showCraftBar(craftMode: false, autoGenerate: true), "the control stays, disabled, with the reason")
        XCTAssertFalse(model.showAIReplyPrivacy(craftBar: true, autoGenerate: false))
        model.aiReplyPressed()
        try? await Task.sleep(nanoseconds: 150_000_000)
        XCTAssertFalse(model.showAIReplyDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
    }

    func testAnAIReplyBelongsToWhoeverAskedForIt() async throws {
        clearAIReplyMemory()
        let json = String(data: try fixture("node_shared_ai_reply.json"), encoding: .utf8) ?? "{}"
        let model = await thread(json)
        // ben's AI wrote the focal reply under anna's entry: not seowriter's, though any member can answer it.
        XCTAssertFalse(model.isOwner)
        XCTAssertTrue(model.showCraftBar(craftMode: false, autoGenerate: true))
        // The reply seowriter's AI wrote under it is seowriter's, whatever parent_user_id says.
        XCTAssertTrue(model.ownedByMe(userId: 9, parentUserId: 12, nodeType: .llm, humanOwnerUsername: "seowriter"))
        XCTAssertFalse(model.ownedByMe(userId: 9, parentUserId: 5, nodeType: .llm, humanOwnerUsername: "ben"))
        // Without the name (a private thread): parent_user_id decides, as before.
        XCTAssertTrue(model.ownedByMe(userId: 9, parentUserId: 5, nodeType: .llm))
        // Its edit form is told it is an AI reply.
        let child = try XCTUnwrap(model.node?.children.first)
        XCTAssertTrue(model.target(child).isAIReply)
    }

    func testEditingAnAIReplyInASharedThreadOffersOnlyMeAndTheThread() async {
        StubURLProtocol.install { [self] request in
            if path(request) == "/api/nodes/40/audience" {
                return .json(200, """
                {"audience":null,"thread_audience":{"entry_id":31,"author":"anna","is_author":false,
                 "readers":["ben","seowriter"],"is_entry":false,"entry_ai_usage":"chat"}}
                """)
            }
            if path(request) == "/api/nodes/40" { return .json(200, #"{"node":{"id":40,"content":"x"},"descendants_updated":0}"#) }
            return .json(200, "{}")
        }
        let (model, _) = form(NodeFormConfig(edit: .init(nodeId: 40, initialContent: "x", initialPrivacy: .private,
                                                           initialAIUsage: .chat, isAIReply: true)))
        await model.start()
        XCTAssertTrue(model.hidePublic)
        XCTAssertEqual(model.threadAudienceNames, ["anna", "ben"])
        XCTAssertFalse(model.sharesEntry, "Circles here is the thread's people: no picker, no other circle")
        model.privacy = .circles
        await model.submit()
        let sent = body(of: "PUT /api/nodes/40")
        XCTAssertEqual(sent?["privacy_level"] as? String, "circles")
        XCTAssertNil(sent?["circle_ids"])
        // A typed reply in the same thread keeps its choices.
        let (typed, _) = form(NodeFormConfig(edit: .init(nodeId: 40, initialContent: "x", initialPrivacy: .circles,
                                                           initialAIUsage: .chat)))
        await typed.start()
        XCTAssertFalse(typed.hidePublic)
    }

    func testTheAuthorsAIRepliesAreSharedWithTheThread() async {
        clearAIReplyMemory()
        defer { clearAIReplyMemory() }
        let own = """
        {"id":31,"content":"mine","node_type":"user","user":{"id":5,"username":"seowriter"},
         "privacy_level":"circles","ai_usage":"chat","child_count":0,"children":[],"ancestors":[],
         "audience":{"entry_id":31,"author":"seowriter","is_author":true,"readers":["ben"],"is_entry":true,
                     "entry_ai_usage":"chat","circle_ids":[3],"usernames":[]}}
        """
        let model = await thread(own)
        XCTAssertTrue(model.isSharedThread)
        XCTAssertFalse(model.isForeignShared)
        // Her own shared entry keeps auto-generate and the control of any entry of hers.
        XCTAssertTrue(model.autoGenerateActive(true))
        XCTAssertFalse(model.showCraftBar(craftMode: false, autoGenerate: true))
        XCTAssertTrue(model.showCraftBar(craftMode: true, autoGenerate: false))
        XCTAssertNil(model.aiReplyOffReason)
        // What her AI writes is read by the thread's people: they are named first.
        XCTAssertEqual(model.aiReplyNames, ["ben"])
        model.aiReplyPressed()
        await waitForDialog(model)
        XCTAssertTrue(model.showAIReplyDialog)
        XCTAssertFalse(calls.contains("POST /api/nodes/31/llm"))
        model.aiReplyConfirmed(dontAskAgain: false)
        await waitFor("POST /api/nodes/31/llm")
        XCTAssertNil(body(of: "POST /api/nodes/31/llm")?["privacy_level"])
        XCTAssertNil(UserDefaults.standard.string(forKey: DefaultsKey.circleAiReplyAck(entryId: 31, userId: 5)))
    }

    func testAThreadThatIsNotSharedAsksNothing() async {
        let plain = """
        {"id":31,"content":"mine","node_type":"user","user":{"id":5,"username":"seowriter"},
         "privacy_level":"private","ai_usage":"chat","child_count":0,"children":[],"ancestors":[]}
        """
        let model = await thread(plain)
        XCTAssertFalse(model.isSharedThread)
        XCTAssertFalse(model.showAIReplyPrivacy(craftBar: true, autoGenerate: false))
        model.aiReplyPressed()
        await waitFor("POST /api/nodes/31/llm")
        XCTAssertFalse(model.showAIReplyDialog)
        XCTAssertNil(body(of: "POST /api/nodes/31/llm")?["privacy_level"])
    }
}
