import XCTest
@testable import Loore

/// Choices the phone remembers (last privacy level and AI usage, the
/// public-reply "don't show again", the reply switches) stay with the account
/// that made them.
@MainActor
final class DeviceChoicesTests: StubbedAppTestCase {
    private func user(id: Int, name: String) throws -> CurrentUser {
        try decode(CurrentUser.self, """
        {"id":\(id),"username":"\(name)","approved":true,"terms_up_to_date":true,"plan":"alpha",
         "default_privacy_level":"private","default_ai_usage":"chat","timezone":"\(TimeZone.current.identifier)"}
        """)
    }

    private func makeChoices() {
        defaults.set("public", forKey: DefaultsKey.lastPrivacyLevel)
        defaults.set("train", forKey: DefaultsKey.lastAIUsage)
        defaults.set("true", forKey: DefaultsKey.publicReplyAck)
        defaults.set(false, forKey: DefaultsKey.agenticReply)
        defaults.set(false, forKey: DefaultsKey.autoGenerate)
    }

    private var choicesLeft: [String] {
        DeviceChoices.keys.filter { defaults.object(forKey: $0) != nil }
    }

    /// A new top-level entry, as the Write page opens it.
    private func newEntryForm() -> NodeFormModel {
        NodeFormModel(config: NodeFormConfig(), app: app, defaults: defaults)
    }

    func testADifferentAccountStartsFromItsOwnDefaults() async throws {
        app.didLoad(try user(id: 5, name: "ann"))
        makeChoices()
        defaults.set("light", forKey: DefaultsKey.theme)
        XCTAssertEqual(newEntryForm().privacy, .public)

        await app.signOutLocally()
        app.didLoad(try user(id: 6, name: "ben"))

        XCTAssertEqual(choicesLeft, [])
        let form = newEntryForm()
        XCTAssertEqual(form.privacy, .private)
        XCTAssertEqual(form.aiUsage, .chat)
        XCTAssertEqual(defaults.string(forKey: DefaultsKey.theme), "light", "not an account's choice")
    }

    func testTheSameAccountKeepsItsChoicesOverASignOut() async throws {
        app.didLoad(try user(id: 5, name: "ann"))
        makeChoices()

        await app.signOutLocally()
        XCTAssertEqual(choicesLeft, DeviceChoices.keys)
        app.didLoad(try user(id: 5, name: "ann"))

        XCTAssertEqual(choicesLeft, DeviceChoices.keys)
        XCTAssertEqual(newEntryForm().privacy, .public)
    }

    // Choices stored by a build that did not record their owner.

    func testChoicesWithoutARecordedOwnerStayWithTheSignedInAccount() throws {
        makeChoices()
        app.didLoad(try user(id: 5, name: "ann"))
        XCTAssertEqual(choicesLeft, DeviceChoices.keys)
        XCTAssertEqual(defaults.integer(forKey: DeviceChoices.ownerKey), 5)
    }

    func testChoicesWithoutARecordedOwnerAreRemovedWhenTheAppOpensSignedOut() async {
        makeChoices()
        let signedOut = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(), defaults: defaults)
        await signedOut.start()
        XCTAssertEqual(signedOut.phase, .signedOut)
        XCTAssertEqual(choicesLeft, [])
    }

    func testChoicesWithARecordedOwnerStayWhenTheAppOpensSignedOut() async {
        makeChoices()
        defaults.set(5, forKey: DeviceChoices.ownerKey)
        let signedOut = AppState(launch: LaunchOptions(), secureStore: InMemorySecureStore(), defaults: defaults)
        await signedOut.start()
        XCTAssertEqual(choicesLeft, DeviceChoices.keys)
    }
}
