import XCTest
@testable import Loore

// Account deletion and restore (#269): the web's DeleteAccountDialog,
// ConfirmAccountDeletionPage and AccountRestorePage, against a stubbed backend.

private func json(_ body: [String: Any]) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: body), encoding: .utf8)!
}

/// 2026-10-09 12:00 UTC; 30 days later is 8 November.
private let pinnedNow = Date(timeIntervalSince1970: 1_791_547_200)

private let accountDeletionInfo: [String: Any] = [
    "grace_days": 30, "username_reserve_days": 365, "confirm_by_email": false,
    "link_expires_in": 3600, "refusal": NSNull(),
]

@MainActor
class AccountDeletionTestCase: StubbedAppTestCase {
    func signIn(_ fields: [String: Any] = [:]) throws {
        var user: [String: Any] = ["id": 5, "username": "seowriter", "approved": true, "terms_up_to_date": true,
                                   "account_deletion": accountDeletionInfo]
        user.merge(fields) { _, new in new }
        app.useForTesting(api: app.api, user: try decode(CurrentUser.self, json(user)))
    }

    var deleteDate: String { AccountDeletion.formatDate(AccountDeletion.dateAfter(days: 30, from: pinnedNow)) }
}

/// AccountPage.accountDeletion.test.js.
@MainActor
final class DeleteAccountModelTests: AccountDeletionTestCase {
    private func model() -> DeleteAccountModel {
        let model = DeleteAccountModel(app: app)
        model.now = { pinnedNow }
        return model
    }

    func testOfferedOnlyWhenTheServerHasAccountDeletion() throws {
        XCTAssertFalse(DeleteAccountModel(app: app).isAvailable, "the fixture user has no account_deletion")
        try signIn()
        XCTAssertTrue(DeleteAccountModel(app: app).isAvailable)
    }

    func testTheLastAdminSeesWhyInsteadOfTheButton() throws {
        var info = accountDeletionInfo
        info["refusal"] = ["code": "last_admin", "message": "This is the last admin account. Make another account an admin first."]
        try signIn(["is_admin": true, "account_deletion": info])
        XCTAssertEqual(model().refusal, "This is the last admin account. Make another account an admin first.")
    }

    func testTheDialogSaysWhatHappensAndWhenInTheWebsWords() throws {
        try signIn()
        let model = model()
        XCTAssertEqual(model.dialogParagraphs.count, 3)
        XCTAssertEqual(model.dialogParagraphs[0],
                       "Your account is deleted at once and you are signed out everywhere. Nobody can see your public writing any more.")
        XCTAssertTrue(model.dialogParagraphs[1].hasPrefix("If you change your mind, you can still restore it by signing in until \(deleteDate) (30 days). After that it is deleted forever, with everything in it: your entries and recordings, the AI's replies,"))
        XCTAssertTrue(model.dialogParagraphs[1].hasSuffix("Loore keeps a record of what your AI use cost, without your name."))
        XCTAssertEqual(model.dialogParagraphs[2], "Once it is deleted forever, nobody else can take your username for 365 days.")
        XCTAssertFalse(model.dialogParagraphs.joined().contains("hidden"))
        XCTAssertEqual(model.confirmTitle, "Delete my account")
        XCTAssertEqual(model.confirmSubtitle, "Signs you out now. If you change your mind, you can still restore it until \(deleteDate).")
        XCTAssertEqual(model.sectionText, "Deletes your account at once and signs you out everywhere. If you change your mind, you can still restore it by signing in within 30 days; after that it is deleted forever, with everything in it.")
    }

    func testAWaitingWritingDeletionAndXAreMentioned() throws {
        try signIn(["data_deletion": ["status": "scheduled", "purge_at": "2026-10-20T08:00:00Z", "x_connected": true]])
        let paragraphs = model().dialogParagraphs
        XCTAssertEqual(paragraphs.count, 5)
        let writingDate = AccountDeletion.formatDate(LooreDate.parse("2026-10-20T08:00:00Z"))
        XCTAssertEqual(paragraphs[3], "This replaces your request to delete all your writing on \(writingDate): everything is deleted on \(deleteDate) instead, and restoring your account cancels both.")
        XCTAssertTrue(paragraphs[4].hasPrefix("Loore also forgets your X connection for bookmarks and removes its access on X. If X still lists Loore afterwards, remove it yourself: on X, open Settings and privacy"))
        try signIn(["data_deletion": ["status": "done", "purge_at": "2026-10-01T08:00:00Z", "x_connected": false]])
        XCTAssertEqual(model().dialogParagraphs.count, 3, "a finished writing deletion is not replaced")
    }

    func testAnAccountThatSignsInWithXGetsTheXNote() throws {
        let steps = AccountDeletion.xRemoveAccessSteps
        // This session holds the sign-in's token: the request revokes it.
        try signIn(["account_deletion": ["grace_days": 30, "x_sign_in": true, "x_sign_in_revocable": true]])
        XCTAssertEqual(model().dialogParagraphs.last,
                       "Loore also removes the access to your X account that signing in with X gave it. "
                        + "If X still lists Loore afterwards, remove it yourself: " + steps)
        // Without the token here (an email sign-in): the steps only.
        try signIn(["account_deletion": ["grace_days": 30, "x_sign_in": true, "x_sign_in_revocable": false]])
        XCTAssertEqual(model().dialogParagraphs.last,
                       "You sign in with X, so X may list Loore as an app with access to your account. "
                        + "To remove it: " + steps)
        // The bookmark connection's text wins when there is one.
        XCTAssertTrue(DeleteAccountModel.xNote(
            xConnected: true, info: AccountDeletionInfo(xSignIn: true, xSignInRevocable: true))!
            .hasPrefix("Loore also forgets your X connection for bookmarks"))
        XCTAssertNil(DeleteAccountModel.xNote(xConnected: false, info: AccountDeletionInfo()))
    }

    func testTheDeleteButtonWaitsForTheUsername() async throws {
        try signIn()
        let model = model()
        XCTAssertFalse(model.canConfirm)
        model.typed = "seowrite"
        XCTAssertFalse(model.canConfirm)
        let closed = await model.requestDeletion()
        XCTAssertFalse(closed)
        XCTAssertTrue(calls.isEmpty, "nothing is sent until the username matches")
        model.typed = "  SeoWriter "
        XCTAssertTrue(model.canConfirm, "case and surrounding spaces do not matter, as on the web")
    }

    func testAnAccountWithoutEmailIsScheduledAndSignedOut() async throws {
        try signIn()
        StubURLProtocol.install { _ in .json(202, #"{"status":"scheduled","delete_on":"2026-11-08T12:00:00Z","grace_days":30}"#) }
        let model = model()
        model.typed = "seowriter"
        let closed = await model.requestDeletion()
        XCTAssertTrue(closed)
        XCTAssertEqual(calls, ["POST /api/account/delete"])
        XCTAssertEqual(body(of: "POST /api/account/delete")?["confirm"] as? String, "seowriter")
        XCTAssertEqual(app.phase, .accountDeleted(deleteOn: LooreDate.parse("2026-11-08T12:00:00Z")))
        XCTAssertNil(app.user)
        XCTAssertEqual(AccountDeletedView.message(deleteOn: LooreDate.parse("2026-11-08T12:00:00Z")),
                       "You are signed out. If you change your mind, you can still restore it by signing in until \(AccountDeletion.formatDate(LooreDate.parse("2026-11-08T12:00:00Z"))); after that it is deleted forever, with everything in it.")
        XCTAssertEqual(AccountDeletedView.message(deleteOn: nil),
                       "You are signed out. If you change your mind, you can still restore it by signing in within 30 days; after that it is deleted forever, with everything in it.")
        // Answers still in flight from the signed-out session change nothing.
        app.handle(.unauthorized)
        XCTAssertEqual(app.phase, .accountDeleted(deleteOn: LooreDate.parse("2026-11-08T12:00:00Z")))
        await app.leaveAccountDeletionScreen()
        XCTAssertEqual(app.phase, .signedOut)
    }

    func testAWaitlistedAccountCanDeleteItself() async throws {
        // The waitlist screen shows the same section (the server lets an
        // unapproved account through to the deletion routes, #464).
        try signIn(["approved": false])
        let model = model()
        XCTAssertTrue(model.isAvailable)
        StubURLProtocol.install { _ in .json(202, #"{"status":"scheduled","delete_on":"2026-11-08T12:00:00Z","grace_days":30}"#) }
        model.typed = "seowriter"
        let closed = await model.requestDeletion()
        XCTAssertTrue(closed)
        XCTAssertEqual(calls, ["POST /api/account/delete"])
        XCTAssertEqual(app.phase, .accountDeleted(deleteOn: LooreDate.parse("2026-11-08T12:00:00Z")))
    }

    func testAnEmailAccountGetsTheLinkAndStaysSignedIn() async throws {
        var info = accountDeletionInfo
        info["confirm_by_email"] = true
        try signIn(["email": "a@example.com", "account_deletion": info])
        let model = model()
        XCTAssertEqual(model.confirmTitle, "Email me the confirmation link")
        XCTAssertEqual(model.confirmSubtitle, "Nothing changes until you confirm from the link.")
        StubURLProtocol.install { _ in .json(202, #"{"status":"confirm_email","expires_in":3600}"#) }
        model.typed = "seowriter"
        let closed = await model.requestDeletion()
        XCTAssertTrue(closed)
        XCTAssertTrue(model.linkSent)
        XCTAssertEqual(app.phase, .signedIn)
        XCTAssertEqual(model.linkSentText, "Check your email: Loore sent a confirmation link to a@example.com. Nothing changes until you open it and confirm there. The link works for 60 minutes.")
    }

    func testARefusalShowsTheServersReasonAndKeepsTheDialog() async throws {
        try signIn(["is_admin": true])
        StubURLProtocol.install { _ in .json(409, #"{"error":"This is the last admin account. Make another account an admin first.","code":"last_admin"}"#) }
        let model = model()
        model.typed = "seowriter"
        let closed = await model.requestDeletion()
        XCTAssertFalse(closed)
        XCTAssertEqual(model.error, "This is the last admin account. Make another account an admin first.")
        XCTAssertEqual(app.phase, .signedIn)
        XCTAssertFalse(model.busy)
    }

    func testNoAnswerShowsTheWebsFallback() async throws {
        try signIn()
        StubURLProtocol.install { _ in StubResponse(status: 200, failWith: URLError(.notConnectedToInternet)) }
        let model = model()
        model.typed = "seowriter"
        _ = await model.requestDeletion()
        XCTAssertEqual(model.error, "Could not start the deletion. Please try again.")
        model.dialogOpened()
        XCTAssertNil(model.error)
        XCTAssertEqual(model.typed, "", "the dialog opens empty")
    }
}

/// ConfirmAccountDeletionPage.test.js (the app is always signed in here).
@MainActor
final class ConfirmAccountDeletionModelTests: AccountDeletionTestCase {
    func testConfirmingPostsTheTokenAndSignsOut() async throws {
        try signIn(["email": "a@example.com"])
        StubURLProtocol.install { _ in .json(202, #"{"status":"scheduled","delete_on":"2026-11-08T12:00:00Z","grace_days":30}"#) }
        let model = ConfirmAccountDeletionModel(app: app, token: "tok123")
        model.now = { pinnedNow }
        XCTAssertEqual(model.heading, "Delete @seowriter?")
        XCTAssertEqual(model.message, "When you confirm, your account is deleted and you are signed out everywhere. If you change your mind, you can still restore it by signing in until \(deleteDate); after that it is deleted forever, with everything in it. Restoring it also cancels a request to delete all your writing, if one is waiting.")
        XCTAssertTrue(calls.isEmpty, "showing the question sends nothing")
        await model.confirm()
        XCTAssertEqual(calls, ["POST /api/account/delete/confirm"])
        XCTAssertEqual(body(of: "POST /api/account/delete/confirm")?["token"] as? String, "tok123")
        XCTAssertEqual(app.phase, .accountDeleted(deleteOn: LooreDate.parse("2026-11-08T12:00:00Z")))
    }

    func testAnotherAccountsLinkSaysWhoseSessionThisIs() async throws {
        try signIn(["email": "a@example.com"])
        StubURLProtocol.install { _ in .json(403, #"{"error":"This link was sent for a different Loore account. Sign in to that account to use it.","reason":"other_account"}"#) }
        let model = ConfirmAccountDeletionModel(app: app, token: "tok")
        await model.confirm()
        XCTAssertEqual(model.failureText, "This link was sent for a different Loore account. Sign in to that account to use it. You are signed in as @seowriter.")
        XCTAssertTrue(model.offersSignOut)
        XCTAssertEqual(app.phase, .signedIn)
    }

    func testAnExpiredLinkSaysSo() async throws {
        try signIn(["email": "a@example.com"])
        StubURLProtocol.install { _ in .json(400, #"{"error":"This confirmation link is no longer valid. Ask for the deletion again on the Account page.","reason":"invalid_or_expired"}"#) }
        let model = ConfirmAccountDeletionModel(app: app, token: "tok")
        await model.confirm()
        XCTAssertEqual(model.failureText, "This confirmation link is no longer valid. Ask for the deletion again on the Account page.")
        XCTAssertFalse(model.offersSignOut)
    }

    func testWithoutATokenNothingIsPosted() async throws {
        try signIn()
        let model = ConfirmAccountDeletionModel(app: app, token: "")
        XCTAssertEqual(model.heading, "This link is incomplete")
        XCTAssertEqual(model.message, "Open the link from the email again, or ask for the deletion again on the Account page.")
        await model.confirm()
        XCTAssertTrue(calls.isEmpty)
    }

    func testPastedLinkParsing() {
        XCTAssertEqual(AccountDeletion.token(fromPasted: " https://loore.org/confirm-account-deletion?token=abc.DEF-ghi_789 "), "abc.DEF-ghi_789")
        XCTAssertEqual(AccountDeletion.token(fromPasted: "https://staging.loore.org/confirm-account-deletion?token=abc&amp;x=1."), "abc")
        XCTAssertEqual(AccountDeletion.token(fromPasted: "Open https://loore.org/confirm-account-deletion?token=xyz to confirm"), "xyz")
        XCTAssertEqual(AccountDeletion.token(fromPasted: "/confirm-account-deletion?token=xyz"), "xyz")
        XCTAssertEqual(AccountDeletion.token(fromPasted: "eyJ1c2VyX2lkIjo1fQ.ZwX1aQ.sig-nature_x"), "eyJ1c2VyX2lkIjo1fQ.ZwX1aQ.sig-nature_x")
        XCTAssertNil(AccountDeletion.token(fromPasted: "https://loore.org/confirm-account-deletion"))
        XCTAssertNil(AccountDeletion.token(fromPasted: "https://loore.org/auth/magic-link/verify?token=abcdefghijklmnop"),
                     "a sign-in link is not a deletion link")
        XCTAssertNil(AccountDeletion.token(fromPasted: "https://loore.org/confirm-email?token=abcdefghijklmnop"))
        XCTAssertNil(AccountDeletion.token(fromPasted: "hello there"))
        XCTAssertNil(AccountDeletion.token(fromPasted: ""))
    }

    func testTheLinkOpensTheConfirmationInTheApp() {
        XCTAssertEqual(AppRoute.parse("https://loore.org/confirm-account-deletion?token=abc", environment: .production),
                       .confirmAccountDeletion(token: "abc"))
        XCTAssertEqual(AppRoute.parse("/confirm-account-deletion?token=abc", environment: .production)?.preferredTab, .more)
    }
}

/// AccountRestorePage.test.js, and how a sign-in reaches it.
@MainActor
final class AccountRestoreTests: AccountDeletionTestCase {
    private func restoring() -> AccountRestoreModel {
        app.useForTesting(api: app.api, user: nil)
        return AccountRestoreModel(app: app)
    }

    func testASignInIntoADeletedAccountAsksInsteadOfSigningIn() async throws {
        app.useForTesting(api: app.api, user: nil)
        await app.signInCompleted(landing: "/account-restore")
        XCTAssertEqual(app.phase, .restoreOffer)
        XCTAssertTrue(calls.isEmpty, "no user load: the account is signed out")
    }

    func testBothSignInsLandOnTheRestoreQuestion() {
        XCTAssertEqual(MagicLink.landingPath(location: "https://loore.org/account-restore"), "/account-restore")
        XCTAssertTrue(AccountDeletion.isRestoreLanding(MagicLink.landingPath(location: "http://localhost:3001/account-restore")))
        XCTAssertEqual(WebLoginRouting.appLanding(URL(string: "https://loore.org/account-restore")!), "/account-restore")
        XCTAssertNil(WebLoginRouting.appLanding(URL(string: "https://loore.org/profile")!), "a plain X sign-in stays on Reflect")
        XCTAssertFalse(AccountDeletion.isRestoreLanding("/welcome"))
        XCTAssertFalse(AccountDeletion.isRestoreLanding(nil))
    }

    func testTheMagicLinkVerifyReportsTheRestoreLanding() async throws {
        let api = makeStubbedClient(environment: .production)
        let auth = AuthService(api: api, vault: CookieVault(store: InMemorySecureStore(), environment: .production))
        StubURLProtocol.install { _ in
            StubResponse(status: 302, headers: ["Location": "https://loore.org/account-restore",
                                                "Set-Cookie": "session=offer; Path=/; HttpOnly; Secure"],
                         chunks: [])
        }
        let landing = try await auth.verifyMagicLink(pasted: "https://loore.org/auth/magic-link/verify?token=abcdefghijklmnopqrst")
        XCTAssertEqual(landing, "/account-restore")
        XCTAssertTrue(auth.hasAuthCookies, "the restore question lives in the session cookie, which the restore calls send")
    }

    func testARestorableOfferAsksWithBothChoices() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"username":"seowriter","delete_on":"2026-11-08T12:00:00Z","restorable":true}"#) }
        let model = restoring()
        await model.load()
        XCTAssertEqual(calls, ["GET /api/account/restore"])
        XCTAssertEqual(model.heading, "Restore your account?")
        let date = AccountDeletion.formatDate(LooreDate.parse("2026-11-08T12:00:00Z"))
        XCTAssertEqual(model.message, "You deleted @seowriter. You can restore it until \(date); after that it is deleted forever, with everything in it. Restore it to keep using Loore, or keep it deleted. Restoring it also cancels a request to delete all your writing, if one is waiting.")
    }

    func testAStartedDeletionCannotBeUndone() async throws {
        StubURLProtocol.install { _ in .json(200, #"{"username":"seowriter","delete_on":null,"restorable":false}"#) }
        let model = restoring()
        await model.load()
        XCTAssertEqual(model.heading, "Your account is being deleted")
        XCTAssertEqual(model.message, "The deletion of @seowriter has started and cannot be undone.")
    }

    func testNoOfferMeansNothingToRestore() async throws {
        StubURLProtocol.install { _ in .json(404, #"{"error":"Nothing to restore here. Sign in again.","code":"no_offer"}"#) }
        let model = restoring()
        await model.load()
        XCTAssertEqual(model.state, .none)
        XCTAssertEqual(model.heading, "Nothing to restore here")
        XCTAssertEqual(model.message, "Sign in again to see your account's options.")
    }

    func testRestoringSignsIn() async throws {
        StubURLProtocol.install { request in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("GET", "/api/account/restore"):
                return .json(200, #"{"username":"seowriter","delete_on":"2026-11-08T12:00:00Z","restorable":true}"#)
            case ("POST", "/api/account/restore"):
                return .json(200, #"{"status":"restored","next":"/profile"}"#)
            default:
                return .json(200, #"{"user":\#(Self.userJSON),"nodes":[],"pinned_nodes":[]}"#)
            }
        }
        let model = restoring()
        await app.signInCompleted(landing: "/account-restore")
        await model.load()
        await model.restore()
        // A sign-in also syncs the time zone and fetches updates in the background.
        XCTAssertEqual(calls.filter { $0.hasPrefix("GET /api/account") || $0.hasPrefix("POST /api/account") || $0 == "GET /api/dashboard/" },
                       ["GET /api/account/restore", "POST /api/account/restore", "GET /api/dashboard/"])
        XCTAssertEqual(app.phase, .signedIn)
        XCTAssertEqual(app.user?.username, "seowriter")
    }

    func testAnExpiredQuestionOffersTheSignIn() async throws {
        StubURLProtocol.install { request in
            request.httpMethod == "GET"
                ? .json(200, #"{"username":"seowriter","delete_on":"2026-11-08T12:00:00Z","restorable":true}"#)
                : .json(404, #"{"error":"Nothing to restore here. Sign in again.","code":"no_offer"}"#)
        }
        let model = restoring()
        await model.load()
        await model.restore()
        XCTAssertEqual(model.state, .none)
    }

    func testARestoreTheServerRefusesShowsWhy() async throws {
        StubURLProtocol.install { request in
            request.httpMethod == "GET"
                ? .json(200, #"{"username":"seowriter","delete_on":"2026-11-08T12:00:00Z","restorable":true}"#)
                : .json(409, #"{"error":"The deletion has already started, so the account can no longer be restored.","code":"already_started"}"#)
        }
        let model = restoring()
        await model.load()
        await model.restore()
        XCTAssertEqual(model.error, "The deletion has already started, so the account can no longer be restored.")
        XCTAssertFalse(model.busy)
    }

    func testKeepingItDeletedForgetsTheQuestionEvenIfTheCallFails() async throws {
        StubURLProtocol.install { request in
            request.httpMethod == "GET"
                ? .json(200, #"{"username":"seowriter","delete_on":"2026-11-08T12:00:00Z","restorable":true}"#)
                : StubResponse(status: 200, failWith: URLError(.notConnectedToInternet))
        }
        let model = restoring()
        await model.load()
        await model.keepDeleted()
        XCTAssertEqual(calls, ["GET /api/account/restore", "POST /api/account/restore/decline"])
        XCTAssertEqual(model.state, .declined)
        XCTAssertEqual(model.heading, "Your account stays deleted")
        XCTAssertEqual(model.message, "You are signed out. Nothing else changes.")
        await app.leaveAccountDeletionScreen()
        XCTAssertEqual(app.phase, .signedOut)
    }

    func testTheUserObjectDecodesWithoutTheDeletionFields() throws {
        let user = try decode(CurrentUser.self, #"{"id":1,"username":"a"}"#)
        XCTAssertNil(user.accountDeletion)
        XCTAssertNil(user.dataDeletion)
        let full = try decode(CurrentUser.self, json(["id": 1, "username": "a", "account_deletion": ["confirm_by_email": true]]))
        XCTAssertEqual(full.accountDeletion, AccountDeletionInfo(confirmByEmail: true), "missing numbers take the web's defaults")
    }
}
