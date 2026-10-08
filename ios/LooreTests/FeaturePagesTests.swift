import XCTest
import ZIPFoundation
import SwiftUI
@testable import Loore

// M4: the jest suites for the feature pages that pin logic
// (AccountPage, ConfirmEmailPage, ProfileGenerationWatcher, ReadReply,
// FeedPicks), plus the todo save rule, import file handling and the models.

private func json(_ body: [String: Any]) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: body), encoding: .utf8)!
}

/// AccountPage.test.js.
@MainActor
final class AccountModelTests: StubbedAppTestCase {
    private func signIn(_ fields: [String: Any]) throws {
        var user: [String: Any] = ["id": 5, "username": "seowriter", "approved": true, "terms_up_to_date": true]
        user.merge(fields) { _, new in new }
        app.useForTesting(api: app.api, user: try decode(CurrentUser.self, json(user)))
    }

    func testAnEmailRequestInFlightBlocksASecondOne() async throws {
        try signIn(["email": "a@example.com"])
        StubURLProtocol.install { _ in
            StubResponse(status: 200, headers: ["Content-Type": "application/json"],
                         chunks: [Data(#"{"email":"a@example.com","pending_email":"b@example.com","pending_email_expired":false}"#.utf8)],
                         chunkDelay: 0.2)
        }
        let model = AccountModel(app: app)
        async let first: Void = model.sendEmailLink("b@example.com")
        async let second: Void = model.sendEmailLink("b@example.com")
        _ = await (first, second)
        XCTAssertEqual(calls.filter { $0 == "POST /api/dashboard/email" }.count, 1, "a double tap mails one link")
        XCTAssertEqual(app.user?.pendingEmail, "b@example.com")
        XCTAssertEqual(model.emailInput, "")
    }

    func testPendingChangeOffersResendAndCancelWhichAdoptsTheServerEmail() async throws {
        try signIn(["email": "a@example.com", "pending_email": "b@example.com"])
        let model = AccountModel(app: app)
        XCTAssertEqual(model.resendTitle, "Resend")
        XCTAssertTrue(model.pendingNotice?.hasPrefix("Confirmation link sent to b@example.com.") == true)
        StubURLProtocol.install { _ in .json(200, #"{"email":"b@example.com","pending_email":null,"pending_email_expired":false}"#) }
        await model.cancelPendingEmail()
        XCTAssertEqual(calls, ["DELETE /api/dashboard/email/pending"])
        XCTAssertEqual(app.user?.email, "b@example.com", "confirmed elsewhere in the meantime")
        XCTAssertNil(model.pendingNotice)
    }

    func testExpiredLinkOffersANewOneForTheSameAddress() async throws {
        try signIn(["email": "a@example.com", "pending_email": "b@example.com", "pending_email_expired": true])
        let model = AccountModel(app: app)
        XCTAssertEqual(model.pendingNotice, "The confirmation link sent to b@example.com has expired.")
        XCTAssertEqual(model.resendTitle, "Send a new link")
        StubURLProtocol.install { _ in .json(200, #"{"email":"a@example.com","pending_email":"b@example.com","pending_email_expired":false}"#) }
        await model.sendEmailLink("b@example.com", resend: true)
        XCTAssertEqual(body(of: "POST /api/dashboard/email")?["email"] as? String, "b@example.com")
        XCTAssertEqual(model.emailMessage?.text, "New link sent. The earlier one no longer works.")
    }

    func testRemoveEmailOnlyWithXLogin() async throws {
        try signIn(["email": "a@example.com"])
        XCTAssertFalse(AccountModel(app: app).showsRemoveEmail)
        try signIn(["email": "a@example.com", "twitter_login": true, "twitter_handle": "sw"])
        let model = AccountModel(app: app)
        XCTAssertTrue(model.showsRemoveEmail)
        StubURLProtocol.install { _ in .json(200, #"{"email":null,"pending_email":null,"pending_email_expired":false}"#) }
        await model.removeEmail()
        XCTAssertEqual(calls, ["DELETE /api/dashboard/email"])
        XCTAssertEqual(model.emailMessage?.text, "Email removed. You sign in with X.")
        XCTAssertNil(app.user?.email)
    }

    func testXRowStates() throws {
        try signIn(["email": "a@example.com"])
        var model = AccountModel(app: app)
        XCTAssertNil(model.xConnectedText, "Connect X is offered")
        XCTAssertFalse(model.showsDisconnectX)
        XCTAssertEqual(model.xHelper, "Lets you sign in with X as well. X will ask you to allow Loore.")
        try signIn(["email": "a@example.com", "twitter_login": true, "twitter_handle": "sw"])
        model = AccountModel(app: app)
        XCTAssertEqual(model.xConnectedText, "Connected as @sw")
        XCTAssertTrue(model.showsDisconnectX)
        try signIn(["twitter_login": true, "twitter_handle": "sw"])
        XCTAssertFalse(AccountModel(app: app).showsDisconnectX, "an X-only account cannot disconnect")
    }

    func testXLoginOutcomeMessages() async throws {
        try signIn(["email": "a@example.com"])
        let model = AccountModel(app: app)
        StubURLProtocol.install { _ in .json(200, #"{"user":\#(Self.userJSON)}"#) }
        await model.xLoginReturned("taken")
        XCTAssertTrue(model.xMessage?.text.contains("already signs in to another Loore account") == true)
        XCTAssertEqual(model.xMessage?.kind, .error)
        await model.xLoginReturned("something-new")
        XCTAssertNil(model.xMessage, "an unknown outcome shows nothing")
    }

    func testDisconnectXCallsTheServerAndForgetsTheHandle() async throws {
        try signIn(["email": "a@example.com", "twitter_login": true, "twitter_handle": "sw"])
        let model = AccountModel(app: app)
        StubURLProtocol.install { _ in .json(200, #"{"twitter_login":false,"twitter_handle":null}"#) }
        await model.disconnectX()
        XCTAssertEqual(calls, ["DELETE /api/dashboard/x"])
        XCTAssertEqual(app.user?.twitterLogin, false)
        XCTAssertEqual(model.xMessage?.text, "X disconnected. You sign in with email.")
    }

    func testUsernameValidation() {
        XCTAssertEqual(AccountModel.usernameError("  "), "Username cannot be empty.")
        XCTAssertEqual(AccountModel.usernameError(String(repeating: "a", count: 65)), "Username must be 64 characters or fewer.")
        XCTAssertEqual(AccountModel.usernameError("bad name!"), "Only letters, numbers, and underscores allowed.")
        XCTAssertNil(AccountModel.usernameError("good_name_42"))
    }
}

/// ConfirmEmailPage.test.js (the app is always signed in here).
@MainActor
final class ConfirmEmailModelTests: StubbedAppTestCase {
    func testPostsTheTokenOnceAndUpdatesTheUser() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"message":"Email confirmed.","email":"new@example.com","pending_email":null,"pending_email_expired":false}"#) }
        let model = ConfirmEmailModel(app: app, token: "tok123")
        model.startOnce()
        model.startOnce()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(calls, ["POST /api/dashboard/email/confirm"])
        XCTAssertEqual(body(of: "POST /api/dashboard/email/confirm")?["token"] as? String, "tok123")
        XCTAssertEqual(model.state, .confirmed(email: "new@example.com"))
        XCTAssertEqual(model.heading, "Email confirmed")
        XCTAssertEqual(app.user?.email, "new@example.com")
        XCTAssertEqual(model.backLabel, "Back to your account")
    }

    func testAWaitlistedAccountContinues() throws {
        app.useForTesting(api: app.api, user: try decode(CurrentUser.self, #"{"id":5,"username":"seowriter","approved":false}"#))
        XCTAssertEqual(ConfirmEmailModel(app: app, token: "t").backLabel, "Continue")
    }

    func testOtherAccountSaysWhoseSessionThisIs() async throws {
        StubURLProtocol.install { _ in .json(403, #"{"error":"This confirmation link was requested from a different Loore account. Sign in to that account to use it.","reason":"other_account"}"#) }
        let model = ConfirmEmailModel(app: app, token: "tok")
        model.startOnce()
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(model.heading, "Not confirmed")
        XCTAssertTrue(model.body.hasSuffix(" You are signed in as @seowriter."))
        XCTAssertEqual(model.action, .signOut)
    }

    func testNoAnswerOffersTryAgainWhichPostsAgain() async throws {
        StubURLProtocol.install { _ in StubResponse(status: 200, failWith: URLError(.notConnectedToInternet)) }
        let model = ConfirmEmailModel(app: app, token: "tok")
        model.startOnce()
        // Polled, not a fixed 300 ms: a busy CI runner was slower than that.
        for _ in 0..<50 where model.action != .retry { try await Task.sleep(nanoseconds: 100_000_000) }
        XCTAssertEqual(model.action, .retry)
        model.confirm()
        for _ in 0..<50 where calls.count < 2 { try await Task.sleep(nanoseconds: 100_000_000) }
        XCTAssertEqual(calls.count, 2)
    }

    func testWithoutATokenNothingIsPosted() async throws {
        let model = ConfirmEmailModel(app: app, token: nil)
        model.startOnce()
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertTrue(calls.isEmpty)
        XCTAssertEqual(model.heading, "This link is incomplete")
        XCTAssertEqual(model.action, .back)
    }

    func testPastedLinkParsing() {
        XCTAssertEqual(ConfirmEmailLink.token(from: " https://loore.org/confirm-email?token=abc123def456ghi789 "), "abc123def456ghi789")
        XCTAssertEqual(ConfirmEmailLink.token(from: "/confirm-email?token=xyz"), "xyz")
        XCTAssertEqual(ConfirmEmailLink.token(from: "abcdefghijklmnop-_"), "abcdefghijklmnop-_")
        XCTAssertNil(ConfirmEmailLink.token(from: "https://loore.org/confirm-email"))
        XCTAssertNil(ConfirmEmailLink.token(from: "hello there"))
    }
}

/// ProfileGenerationWatcher.test.js and the outcome rules (map E §10).
@MainActor
final class ProfileGenerationWatcherTests: XCTestCase {
    private func watcher() -> (ProfileGenerationWatcher, () -> [String], () -> Int) {
        let w = ProfileGenerationWatcher()
        var toasts: [String] = []
        var cleared = 0
        w.showToast = { text, _ in toasts.append(text) }
        w.clearUserFlags = { cleared += 1 }
        w.fetch = { _ in try await Task.sleep(nanoseconds: 60_000_000_000); throw CancellationError() }
        w.start()
        return (w, { toasts }, { cleared })
    }

    func testGivingUpOnErrorsWithStaleRunningDataIsQuiet() {
        let (w, toasts, cleared) = watcher()
        w.handle(ProfileProgress(running: true, source: "batch", status: "progress", message: "Generating profile"))
        w.gaveUp()
        XCTAssertEqual(w.doneCount, 1)
        XCTAssertTrue(toasts().isEmpty, "no toast when polling gave up")
        XCTAssertEqual(cleared(), 1)
        XCTAssertFalse(w.active)
        w.stop()
    }

    func testAFailedAnswerIsHandledOnce() {
        let (w, toasts, _) = watcher()
        XCTAssertEqual(w.handle(ProfileProgress(running: false, status: "failed", error: "boom")), .failed)
        XCTAssertNil(w.handle(ProfileProgress(running: false, status: "failed", error: "boom")), "inactive afterwards")
        XCTAssertEqual(toasts(), ["Profile generation failed"])
        XCTAssertEqual(w.doneCount, 1)
        w.stop()
    }

    func testIdleWithANewVersionCompletes() {
        let (w, toasts, _) = watcher()
        w.handle(ProfileProgress(running: true, source: "sync", status: "progress", progress: 40, message: "Writing",
                                 taskId: "t1", latestProfile: .init(id: 1)))
        XCTAssertEqual(w.progress?.message, "Writing")
        XCTAssertEqual(w.handle(ProfileProgress(running: false, status: "idle", latestProfile: .init(id: 2))), .completed)
        XCTAssertEqual(toasts(), ["Your profile has been updated ✓"])
        w.stop()
    }

    func testIdleAfterRunningWithoutANewVersionStalled() {
        let (w, toasts, _) = watcher()
        w.handle(ProfileProgress(running: true, source: "batch", status: "progress", latestProfile: .init(id: 1)))
        XCTAssertEqual(w.handle(ProfileProgress(running: false, status: "idle", latestProfile: .init(id: 1))), .stalled)
        XCTAssertEqual(toasts(), ["Profile generation stopped before finishing — it will be retried in the background"])
        w.stop()
    }

    func testIdleWithoutEverRunningIsAStaleFlag() {
        let (w, toasts, _) = watcher()
        XCTAssertNil(w.handle(ProfileProgress(running: false, status: "idle")))
        XCTAssertTrue(toasts().isEmpty)
        XCTAssertEqual(w.doneCount, 1)
        w.stop()
    }
}

/// ReadReply.test.js and FeedPicks.test.js.
@MainActor
final class ReadReplyTests: StubbedAppTestCase {
    private func window(excluded: Int) throws -> ReadWindow {
        try decode(ReadWindow.self, """
        {"window_start":"2026-09-20T08:00:00Z","window_end":"2026-09-21T08:00:00Z","tweets":1234,"accounts":56,"excluded":\(excluded)}
        """)
    }

    func testWindowLineStatesTheRangeTheCountsAndWhatWasLeftOut() throws {
        let w = try window(excluded: 7)
        let from = LooreDateFormat.dateTime(w.windowStart)
        let to = LooreDateFormat.dateTime(w.windowEnd)
        XCTAssertEqual(ReadWindowLine.text(w),
                       "Tweets from \(from) to \(to) (your time): 1,234 by 56 accounts. 7 you had already read were left out.")
        XCTAssertFalse(ReadWindowLine.text(try window(excluded: 0))!.contains("left out"))
        XCTAssertNil(ReadWindowLine.text(try decode(ReadWindow.self, #"{"tweets":3}"#)), "no line without a window")
    }

    func testTailStates() {
        XCTAssertEqual(ReadReplyTail.stateText(unread: 3, total: 3, loaded: true), "3 unread")
        XCTAssertEqual(ReadReplyTail.stateText(unread: 1, total: 3, loaded: true), "1 of 3 unread")
        XCTAssertEqual(ReadReplyTail.stateText(unread: 0, total: 3, loaded: true), "All read.")
        XCTAssertNil(ReadReplyTail.stateText(unread: 2, total: 3, loaded: false), "silent before the quotes load")
        XCTAssertNil(ReadReplyTail.stateText(unread: 0, total: 0, loaded: true))
    }

    func testMarkAllAnswerDecodes() throws {
        let answer = try decode(FeedPicksReadAnswer.self, #"{"node_id":9,"read_at":{"11":"2026-09-30T10:00:00Z","12":"2026-09-30T10:00:01Z"}}"#)
        XCTAssertEqual(answer.readAt.keys.sorted(), [11, 12])
    }

    private func picksModel(read: Bool, shared: Bool = false) -> FeedPicksModel {
        let model = FeedPicksModel(nodeId: 9, app: app)
        let item = ExternalItem(id: 11, source: "read_pick", authorHandle: "a", url: "https://x.com/a/status/1",
                                readAt: read ? Date(timeIntervalSince1970: 1) : nil, feedback: shared ? "good" : nil,
                                feedbackShared: shared)
        model.setForTesting([FeedPicksAnswer.Pick(rank: 1, relevance: 80, recommended: true, pickedBy: nil, why: "w", item: item)])
        return model
    }

    func testOpenOnXLogsTheOpenAndMarksThePickRead() async throws {
        let model = picksModel(read: false)
        StubURLProtocol.install { _ in .json(200, #"{"id":11,"read_at":"2026-09-30T10:00:00Z"}"#) }
        await model.openedOnX(0)
        XCTAssertEqual(calls, ["POST /api/external/items/11/read"])
        let sent = body(of: "POST /api/external/items/11/read")
        XCTAssertEqual(sent?["node_id"] as? Int, 9)
        XCTAssertEqual(sent?["via"] as? String, "open")
        XCTAssertNotNil(model.picks?.first?.item.readAt)
        XCTAssertEqual(model.unread, 0)
    }

    func testOpeningAnAlreadyReadPickStillLogsTheOpen() async throws {
        let model = picksModel(read: true)
        StubURLProtocol.install { _ in .json(200, #"{"id":11,"read_at":"1970-01-01T00:00:01Z"}"#) }
        await model.openedOnX(0)
        XCTAssertEqual(calls, ["POST /api/external/items/11/read"])
        XCTAssertNotNil(model.picks?.first?.item.readAt)
    }

    func testAVerdictMarksThePickReadAndIsNoLongerShared() {
        let model = picksModel(read: false, shared: true)
        model.feedbackChanged(0, verdict: "bad", readAt: Date())
        XCTAssertEqual(model.picks?.first?.item.feedback, "bad")
        XCTAssertEqual(model.picks?.first?.item.feedbackShared, false)
        XCTAssertNotNil(model.picks?.first?.item.readAt)
    }
}

/// The todo save rule (design doc §10): re-fetch right before every PATCH.
@MainActor
final class TodoModelTests: StubbedAppTestCase {
    private let shown = "## Today\n\n- [ ] water the plants\n"
    private let fresher = "## Today\n\n- [ ] water the plants\n- [ ] added by the AI\n"

    private func model(serverContent: String, patchStatus: Int = 200) async -> TodoModel {
        let m = TodoModel(app: app)
        StubURLProtocol.install { [shown] request in
            if request.httpMethod == "GET" {
                return .json(200, json(["todo": ["id": 1, "content": shown, "version_number": 1]]))
            }
            return .json(404, #"{"error":"no stub"}"#)
        }
        await m.fetch()
        StubURLProtocol.install { request in
            if request.httpMethod == "GET" {
                return .json(200, json(["todo": ["id": 1, "content": serverContent, "version_number": 1]]))
            }
            if patchStatus != 200 { return .json(patchStatus, #"{"error":"No todo exists to update"}"#) }
            let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
            return .json(200, json(["todo": ["id": 1, "content": sent?["content"] ?? "", "version_number": 1]]))
        }
        return m
    }

    func testToggleRefetchesAndAppliesTheTickToTheFreshContent() async {
        let m = await model(serverContent: fresher)
        let item = TodoSections.parse(m.todo?.content).first!.items.first!
        await m.toggle(item)
        XCTAssertEqual(calls.suffix(2), ["GET /api/todo/", "PATCH /api/todo/"])
        XCTAssertEqual(body(of: "PATCH /api/todo/")?["content"] as? String,
                       "## Today\n\n- [x] water the plants\n- [ ] added by the AI\n", "the AI's item survives")
        XCTAssertEqual(m.todo?.content, "## Today\n\n- [x] water the plants\n- [ ] added by the AI\n")
    }

    func testQuickAddGoesToTheEndOfToday() async {
        let m = await model(serverContent: shown)
        let ok = await m.quickAdd("buy bread")
        XCTAssertTrue(ok)
        XCTAssertEqual(body(of: "PATCH /api/todo/")?["content"] as? String,
                       "## Today\n\n- [ ] water the plants\n- [ ] buy bread\n")
    }

    func testAFailedSaveRevertsAndSaysWhy() async {
        let m = await model(serverContent: shown, patchStatus: 404)
        let item = TodoSections.parse(m.todo?.content).first!.items.first!
        await m.toggle(item)
        XCTAssertEqual(m.todo?.content, shown)
        XCTAssertEqual(app.toasts.toasts.last?.message, "Couldn't save change — reverted (No todo exists to update)")
    }

    // MARK: Quick edits in a row (review B2)

    /// A stub backend that keeps the PATCHed content; a PATCH whose content
    /// matches `failWhen` is answered with 500.
    private final class TodoServer: @unchecked Sendable {
        private let lock = NSLock()
        private var _content: String
        private let failWhen: (String) -> Bool

        init(_ content: String, failWhen: @escaping (String) -> Bool = { _ in false }) {
            _content = content
            self.failWhen = failWhen
        }

        var content: String {
            lock.lock()
            defer { lock.unlock() }
            return _content
        }

        func answer(_ request: URLRequest) -> StubResponse {
            lock.lock()
            defer { lock.unlock() }
            if request.httpMethod == "PATCH" {
                let sent = (try? JSONSerialization.jsonObject(with: request.httpBody ?? Data())) as? [String: Any]
                let content = sent?["content"] as? String ?? _content
                if failWhen(content) { return .json(500, #"{"error":"Server error"}"#) }
                _content = content
            }
            return .json(200, json(["todo": ["id": 1, "content": _content, "version_number": 1]]))
        }
    }

    private let two = "## Today\n\n- [ ] water the plants\n- [ ] feed the cat\n"

    private func model(on server: TodoServer) async -> TodoModel {
        let m = TodoModel(app: app)
        StubURLProtocol.install { server.answer($0) }
        await m.fetch()
        return m
    }

    func testTwoQuickTicksAreBothSaved() async {
        let server = TodoServer(two)
        let m = await model(on: server)
        let items = TodoSections.parse(m.todo?.content).first!.items
        async let first: Void = m.toggle(items[0])
        async let second: Void = m.toggle(items[1])
        _ = await (first, second)
        let both = "## Today\n\n- [x] water the plants\n- [x] feed the cat\n"
        XCTAssertEqual(server.content, both, "the second tick's PATCH builds on the first one's")
        XCTAssertEqual(m.todo?.content, both)
        XCTAssertEqual(calls.suffix(4), ["GET /api/todo/", "PATCH /api/todo/", "GET /api/todo/", "PATCH /api/todo/"])
    }

    func testAQuickAddThenATickKeepsTheNewTask() async {
        let server = TodoServer(two)
        let m = await model(on: server)
        let items = TodoSections.parse(m.todo?.content).first!.items
        async let added = m.quickAdd("buy milk")
        async let ticked: Void = m.toggle(items[1])
        let ok = await added
        _ = await ticked
        XCTAssertTrue(ok)
        let expected = "## Today\n\n- [ ] water the plants\n- [x] feed the cat\n- [ ] buy milk\n"
        XCTAssertEqual(server.content, expected)
        XCTAssertEqual(m.todo?.content, expected)
    }

    func testAFailedEditDoesNotUndoTheNextOne() async {
        // Whichever tap runs first, the PATCH that ticks the plants fails.
        let server = TodoServer(two, failWhen: { $0.contains("- [x] water the plants") })
        let m = await model(on: server)
        let items = TodoSections.parse(m.todo?.content).first!.items
        async let first: Void = m.toggle(items[0])
        async let second: Void = m.toggle(items[1])
        _ = await (first, second)
        let secondOnly = "## Today\n\n- [ ] water the plants\n- [x] feed the cat\n"
        XCTAssertEqual(server.content, secondOnly)
        XCTAssertEqual(m.todo?.content, secondOnly, "only the failed tick is reverted")
        XCTAssertEqual(app.toasts.toasts.last?.message, "Couldn't save change — reverted (Server error)")
    }
}

/// Import: archive reading, bodies, results (map E §7.1).
@MainActor
final class ImportTests: StubbedAppTestCase {
    private var dir: URL!

    override func setUp() async throws {
        try await super.setUp()
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("import-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
        try await super.tearDown()
    }

    private func zip(_ name: String, _ entries: [String: String]) throws -> URL {
        let url = dir.appendingPathComponent(name)
        let archive = try Archive(url: url, accessMode: .create)
        for (path, text) in entries.sorted(by: { $0.key < $1.key }) {
            let data = Data(text.utf8)
            try archive.addEntry(with: path, type: .file, uncompressedSize: Int64(data.count),
                                 compressionMethod: .deflate) { position, size in
                data.subdata(in: Int(position)..<Int(position) + size)
            }
        }
        return url
    }

    /// Review M3: SwiftUI sets the picker's `isPresented` to false before it calls
    /// `onCompletion`; the importer kind must survive that.
    func testThePickedFileKeepsItsImporterAfterThePickerCloses() {
        let model = ImportModel(app: app)
        model.openPicker(.chatgpt)
        XCTAssertTrue(model.showingPicker)
        model.showingPicker = false // SwiftUI, before onCompletion
        let file = URL(fileURLWithPath: "/tmp/export.zip")
        let pick = model.pickerFinished(.success(file))
        XCTAssertEqual(pick?.url, file)
        XCTAssertEqual(pick?.kind, .chatgpt)
        XCTAssertNil(model.pickerFinished(.success(file)), "used once")
        model.openPicker(.twitter)
        XCTAssertNil(model.pickerFinished(.failure(CocoaError(.fileReadNoPermission))))
    }

    // MARK: A Twitter import that stops reporting (review M8)

    private func twitterModel(status: String) -> ImportModel {
        StubURLProtocol.install { request in
            if request.url?.path == "/api/import/twitter/confirm" { return .json(200, #"{"task_id":"t1","total":5}"#) }
            return .json(200, status)
        }
        let model = ImportModel(app: app)
        model.analysis = ImportAnalysis(kind: .twitter, raw: ["import_token": "tok"])
        model.pollInterval = 0.05
        return model
    }

    func testAStalledTwitterImportStopsWithTheCheckYourLogMessage() async {
        let model = twitterModel(status: #"{"status":"running","done":1,"total":5}"#)
        model.stallLimit = 0.3
        await model.confirm()
        XCTAssertEqual(model.error, ImportModel.lostTrackMessage)
        XCTAssertFalse(model.busy)
        XCTAssertFalse(model.pollingTask)
    }

    func testTheDialogClosesWhileATwitterImportIsPolled() async {
        let model = twitterModel(status: #"{"status":"queued"}"#)
        let running = Task { await model.confirm() }
        for _ in 0..<40 where !model.pollingTask { try? await Task.sleep(nanoseconds: 25_000_000) }
        XCTAssertTrue(model.pollingTask)
        XCTAssertTrue(model.busy)
        model.closeWhilePolling()
        await running.value
        XCTAssertNil(model.analysis)
        XCTAssertFalse(model.busy)
        XCTAssertNil(model.error)
        XCTAssertEqual(app.toasts.toasts.last?.message, ImportModel.continuesMessage)
        let polls = calls.filter { $0.hasPrefix("GET /api/import/status/") }.count
        try? await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(calls.filter { $0.hasPrefix("GET /api/import/status/") }.count, polls, "polling stopped")
    }

    func testTheNamedConversationsFileWins() throws {
        let url = try zip("a.zip", ["export/conversations.json": "[{\"a\":1}]", "export/big.json": "[" + String(repeating: "1,", count: 500) + "1]"])
        let out = try ImportFiles.extractConversations(from: url, into: dir)
        XCTAssertEqual(try String(contentsOf: out, encoding: .utf8), "[{\"a\":1}]")
    }

    func testOtherwiseTheLargestJSONArray() throws {
        let url = try zip("b.zip", ["x/settings.json": "{\"big\":\"" + String(repeating: "z", count: 900) + "\"}",
                                    "x/chats.json": "[ {\"t\":1} ]\n", "x/small.json": "[]"])
        let out = try ImportFiles.extractConversations(from: url, into: dir)
        XCTAssertEqual(try String(contentsOf: out, encoding: .utf8), "[ {\"t\":1} ]\n")
    }

    func testZipErrorsUseTheWebsWords() throws {
        let notZip = dir.appendingPathComponent("n.zip")
        try Data("not a zip".utf8).write(to: notZip)
        XCTAssertThrowsError(try ImportFiles.extractConversations(from: notZip, into: dir)) {
            XCTAssertEqual($0 as? ImportFiles.Failure, .unreadableZip)
        }
        let noJSON = try zip("c.zip", ["notes/a.md": "# hi"])
        XCTAssertThrowsError(try ImportFiles.extractConversations(from: noJSON, into: dir)) {
            XCTAssertEqual($0 as? ImportFiles.Failure, .noConversations)
        }
        XCTAssertEqual(ImportFiles.message(.unreadableZip, kind: .claude),
                       "Could not read the zip file. Please make sure it's a valid data export.")
        XCTAssertEqual(ImportFiles.message(.noConversations, kind: .chatgpt),
                       "Could not find conversations.json in the zip archive. Please upload the original data export.")
    }

    func testMultipartBodyStreamsTheFile() throws {
        let source = dir.appendingPathComponent("s.json")
        try Data("[1,2]".utf8).write(to: source)
        let body = try ImportFiles.multipartBody(field: "conversations_file", filename: "conversations.json",
                                                 mimeType: "application/json", source: source, into: dir)
        let text = try String(contentsOf: body.url, encoding: .utf8)
        let boundary = body.contentType.components(separatedBy: "boundary=").last!
        XCTAssertTrue(text.hasPrefix("--\(boundary)\r\nContent-Disposition: form-data; name=\"conversations_file\"; filename=\"conversations.json\"\r\nContent-Type: application/json\r\n\r\n[1,2]\r\n--\(boundary)--\r\n"))
    }

    func testConfirmBodiesPerImporter() {
        let model = ImportModel(app: app)
        let markdown = ImportAnalysis(kind: .markdown, raw: ["files": [["name": "a.md"]], "total_files": 1])
        var body = model.confirmBody(markdown, onDeleted: nil)
        XCTAssertEqual(body["privacy_level"] as? String, "private", "import defaults: private")
        XCTAssertEqual(body["ai_usage"] as? String, "none", "import defaults: no AI")
        XCTAssertEqual(body["import_type"] as? String, "separate_nodes")
        XCTAssertEqual(body["date_ordering"] as? String, "modified")
        XCTAssertNil(body["on_deleted"])
        let twitter = ImportAnalysis(kind: .twitter, raw: ["import_token": "tok"])
        model.includeReplies = true
        body = model.confirmBody(twitter, onDeleted: "skip")
        XCTAssertEqual(body["import_token"] as? String, "tok")
        XCTAssertEqual(body["include_replies"] as? Bool, true)
        XCTAssertEqual(body["on_deleted"] as? String, "skip")
        let claude = ImportAnalysis(kind: .claude, raw: ["conversations": [["name": "c"]]])
        body = model.confirmBody(claude, onDeleted: nil)
        XCTAssertEqual((body["conversations"] as? [[String: Any]])?.count, 1)
        XCTAssertNil(body["import_type"])
    }

    func testDeletedContentConflictAsksThenRetriesWithTheChoice() async {
        let model = ImportModel(app: app)
        model.analysis = ImportAnalysis(kind: .markdown, raw: ["files": []])
        var attempt = 0
        StubURLProtocol.install { _ in
            attempt += 1
            return attempt == 1
                ? .json(409, #"{"error":"deleted_content_matches","deleted_matches":2}"#)
                : .json(201, #"{"created":0,"skipped":2,"restored":0,"updated":0}"#)
        }
        await model.confirm()
        XCTAssertEqual(model.deletedMatches, 2)
        XCTAssertNil(model.result)
        await model.resolveDeleted("skip")
        XCTAssertEqual(body(of: "POST /api/import/confirm")?["on_deleted"] as? String, "skip")
        XCTAssertEqual(model.result?.skipped, 2)
        XCTAssertEqual(model.result?.notes, ["Everything in this archive was already imported — nothing new was added."])
    }

    func testChatGPTAnalyzeErrors() {
        let model = ImportModel(app: app)
        XCTAssertEqual(model.analyzeMessage(.server(status: 400, body: ServerErrorBody(error: "Bad file", details: "line 3")), kind: .chatgpt),
                       "Bad file: line 3")
        XCTAssertEqual(model.analyzeMessage(.server(status: 413, body: nil), kind: .chatgpt),
                       "conversations.json is too large to upload. Please contact support.")
        XCTAssertEqual(model.analyzeMessage(.server(status: 500, body: nil), kind: .chatgpt),
                       "Error analyzing ChatGPT export (HTTP 500). Please try again.")
        XCTAssertEqual(model.analyzeMessage(.transport(code: -1009, description: ""), kind: .chatgpt),
                       "Error analyzing ChatGPT export. The request did not reach the server — check your connection.")
        XCTAssertEqual(model.analyzeMessage(.server(status: 500, body: nil), kind: .markdown),
                       "Error analyzing import file. Please try again.")
    }

    func testResultStatsAndNotes() {
        let result = ImportResult(["created": 3, "skipped": 1, "updated": 2, "empty": 1])
        XCTAssertEqual(result.stats.map(\.label), ["Imported", "Updated", "Skipped", "No text"])
        XCTAssertEqual(result.notes.count, 3)
        XCTAssertEqual(ImportResult(["created": 0]).stats.map(\.label), ["Imported"])
    }
}

/// Feature-page payloads and routes.
@MainActor
final class FeaturePageModelTests: XCTestCase {
    func testVersionRowsIncludingThePromptsFileDefault() throws {
        let list = try decode(VersionList.self, #"{"versions":[{"id":7,"generated_by":"user","created_at":"2026-09-28T10:00:00.123456Z","version_number":1},{"id":"default","generated_by":"default","created_at":null,"version_number":0}]}"#)
        XCTAssertEqual(list.versions.map(\.id), [.row(7), .fileDefault])
        XCTAssertEqual(VersionHistoryText.secondLine(list.versions[1]), "File default · Auto-generated (default)")
        XCTAssertEqual(try decode(VersionContent.self, #"{"artifact":{"id":1,"content":"hi"}}"#).content, "hi")
        XCTAssertEqual(try decode(VersionContent.self, #"{"prompt":{"id":"default","content":"p"}}"#).content, "p")
    }

    func testArtifactPlaceholdersAndReferencePages() throws {
        let artifacts = try decode(ArtifactList.self, #"{"artifacts":[{"id":null,"kind":"memory","title":"Memory","description":"d","content":"","created_at":null},{"id":3,"kind":"m4-x","title":"X","content":"c","created_at":"2026-09-01T00:00:00Z","generated_by":"user"}]}"#)
        XCTAssertFalse(artifacts.artifacts[0].exists)
        XCTAssertTrue(artifacts.artifacts[1].exists)
        XCTAssertEqual(ArtifactsPage.subtitle(artifacts.artifacts[1]), "A persistent document shared between you and the AI. · last updated by edited manually")
        let page = try decode(ExternalItemsPage.self, #"{"items":[{"id":1,"source":"twitter_bookmark","external_id":"20","author_handle":"jack","preview":"p","url":"https://x.com/i/status/20","fetched_at":"2026-09-12T10:49:47.176523Z","has_tts":false,"surfaced_count":0}],"total":1,"has_more":false,"counts":{"twitter_bookmark":1}}"#)
        XCTAssertEqual(page.items.first?.tweetId, "20")
        XCTAssertEqual(page.items.first?.sourceLabel, "Tweet")
        XCTAssertEqual(page.counts["twitter_bookmark"], 1)
        XCTAssertEqual(ReferenceDetailView.surfacingLine(page.items[0]), "Not yet shown by Loore in a conversation")
        XCTAssertEqual(ReferenceFooter.host("https://www.example.com/a"), "example.com")
    }

    func testShareAndCommonsPayloads() throws {
        let shares = try decode(ShareList.self, #"{"shares":[{"id":8,"content":"c","share_type":"insight","status":"draft","created_at":"2026-08-22T18:04:55.963211Z"}]}"#)
        XCTAssertEqual(shares.shares.first?.status, "draft")
        let feed = try decode(CommonsPage.self, #"{"items":[{"id":1,"username":"u","permalink":null,"content":"c","created_at":"2026-08-24T19:27:25Z","reply_count":2}],"has_more":true,"page":1}"#)
        XCTAssertEqual(feed.items.first?.replyCount, 2)
        XCTAssertTrue(feed.hasMore)
    }

    func testProfileMetaLine() throws {
        let profile = try decode(LatestProfile.self, #"{"id":1,"content":"c","generated_by":"claude-opus-5.5","tokens_used":10,"source_tokens_used":120000,"source_origin_stats":{"loore":{"tokens":3000},"twitter":{"tokens":96000},"chatgpt":{"tokens":3000}},"source_data_cutoff":"2026-09-12T00:00:00Z"}"#)
        let line = ProfilePage.metaLine(profile)
        XCTAssertTrue(line.hasPrefix("Built from ~120,000 tokens of writing (94% public tweets, 3% ChatGPT imports) · claude-opus-5.5 · Data through "))
    }

    /// Review M4: a version generated while the editor is open must not be
    /// overwritten in place by text based on the older version.
    func testProfileSaveAsksWhenANewerVersionArrived() {
        XCTAssertEqual(ProfilePage.saveTarget(editingBaseId: 7, latestId: 7), .update(7))
        XCTAssertEqual(ProfilePage.saveTarget(editingBaseId: nil, latestId: nil), .create)
        XCTAssertEqual(ProfilePage.saveTarget(editingBaseId: 7, latestId: 8), .newerVersionArrived)
        XCTAssertEqual(ProfilePage.saveTarget(editingBaseId: nil, latestId: 8), .newerVersionArrived,
                       "writing the first profile while one is generated")
    }

    func testWorkspaceRoutesSwitchTheArtifactsTab() {
        let env = AppEnvironment.local
        XCTAssertEqual(AppRoute.parse("/artifacts?create=1", environment: env), .newArtifact)
        let router = Router()
        router.open(.todo, environment: env, commonsAvailable: true)
        XCTAssertEqual(router.selectedTab, .artifacts)
        XCTAssertEqual(router.workspace, .todo)
        router.open(.artifacts(kind: nil), environment: env, commonsAvailable: true)
        XCTAssertEqual(router.workspace, .artifact("memory"))
        XCTAssertEqual(router.path(for: .artifacts), [])
    }
}

/// The Commons cannot be opened as the test user locally (it lists other
/// users' public posts), so its card is rendered from a hand-made fixture.
/// With `TEST_RUNNER_LOORE_SNAPSHOT_DIR` set, the image is written there.
@MainActor
final class CommonsRenderingTests: StubbedAppTestCase {
    func testCommonsCardsRender() throws {
        let items = [
            CommonsItem(id: 1, username: "riverwalker", content: "## Notes from the bank\n\nThe river was **high** after the rain.",
                        createdAt: Date(), replyCount: 2),
            CommonsItem(id: 2, username: "glacier", content: "Ice moves slowly; so do drafts.", createdAt: nil, replyCount: 1),
            CommonsItem(id: 3, username: "quiet", content: "Nothing to add.", createdAt: nil, replyCount: 0),
        ]
        let view = VStack(spacing: 16) {
            ForEach(items) { item in CommonsCard(item: item) {} }
        }
        .padding(24)
        .frame(width: 402)
        .background(LooreColor.bgDeep)
        .environment(app)
        .environment(\.colorScheme, .dark)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3
        let image = try XCTUnwrap(renderer.uiImage)
        XCTAssertGreaterThan(image.size.height, 300)
        if let dir = ProcessInfo.processInfo.environment["LOORE_SNAPSHOT_DIR"], !dir.isEmpty {
            try image.pngData()?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("commons-cards.png"))
        }
    }

    func testIntentionsViewRenders() throws {
        let content = """
        # Endorsed

        ## Finish the field guide
        *active — endorsed 2026-09-01*
        Write the river chapters before winter.
        - 2026-09-20: drafted two chapters

        ## Learn Czech
        *fulfilled*

        # Inferred

        ## Walk more
        *inferred, unconfirmed*
        Mentions walks often.

        ## Old plan
        *released*
        """
        let view = IntentionsView(content: content)
            .padding(24)
            .frame(width: 402)
            .background(LooreColor.bgDeep)
            .environment(app)
            .environment(\.colorScheme, .dark)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 3
        let image = try XCTUnwrap(renderer.uiImage)
        XCTAssertGreaterThan(image.size.height, 400)
        if let dir = ProcessInfo.processInfo.environment["LOORE_SNAPSHOT_DIR"], !dir.isEmpty {
            try image.pngData()?.write(to: URL(fileURLWithPath: dir).appendingPathComponent("intentions-view.png"))
        }
    }
}
