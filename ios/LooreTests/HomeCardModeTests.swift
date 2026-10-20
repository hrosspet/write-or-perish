import SwiftUI
import XCTest
@testable import Loore

/// The home cards' Voice / Text switch (Peter, 2026-10-13): each card by
/// purpose remembers its own mode on the device, Voice until the user picks
/// Text, and the card opens the remembered mode. The tests read and write the
/// same `AppStorage` the card uses, on a throwaway defaults suite.
final class HomeCardModeTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!

    override func setUp() {
        suiteName = "HomeCardModeTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
    }

    private func mode(_ purpose: HomePurpose) -> HomePurpose.Mode {
        purpose.modeStorage(store: defaults).wrappedValue
    }

    private func pick(_ mode: HomePurpose.Mode, on purpose: HomePurpose) {
        purpose.modeStorage(store: defaults).wrappedValue = mode
    }

    func testEachCardStartsOnVoice() {
        XCTAssertEqual(mode(.reflect), .voice)
        XCTAssertEqual(mode(.glean), .voice)
        XCTAssertNil(defaults.object(forKey: DefaultsKey.homeReflectMode), "reading the default stores nothing")
    }

    func testEachCardRemembersItsOwnMode() {
        pick(.text, on: .reflect)
        XCTAssertEqual(mode(.reflect), .text, "a card made later (the next launch) reads the choice")
        XCTAssertEqual(mode(.glean), .voice, "Glean keeps its own choice")

        pick(.text, on: .glean)
        pick(.voice, on: .reflect)
        XCTAssertEqual(mode(.reflect), .voice)
        XCTAssertEqual(mode(.glean), .text)
    }

    func testTheKeysAndStoredValues() {
        XCTAssertEqual(HomePurpose.reflect.modeKey, "home.reflectMode")
        XCTAssertEqual(HomePurpose.glean.modeKey, "home.gleanMode")
        pick(.text, on: .glean)
        XCTAssertEqual(defaults.string(forKey: "home.gleanMode"), "text")
        XCTAssertNil(defaults.string(forKey: "home.reflectMode"))
    }

    func testAnUnknownStoredValueMeansVoice() {
        defaults.set("Video", forKey: DefaultsKey.homeGleanMode)
        XCTAssertEqual(mode(.glean), .voice)
    }

    func testTheCardOpensItsRememberedMode() {
        XCTAssertEqual(HomePurpose.reflect.route(mode(.reflect)), .voice(parentId: nil, resumeLLMId: nil, glean: false))
        XCTAssertEqual(HomePurpose.glean.route(mode(.glean)), .voice(parentId: nil, resumeLLMId: nil, glean: true))
        pick(.text, on: .reflect)
        pick(.text, on: .glean)
        XCTAssertEqual(HomePurpose.reflect.route(mode(.reflect)), .textMode(glean: false))
        XCTAssertEqual(HomePurpose.glean.route(mode(.glean)), .textMode(glean: true))
    }

    func testVoiceOverSaysWhatTheCardOpens() {
        XCTAssertEqual(HomePurpose.reflect.openLabel(.voice), "Reflect by voice")
        XCTAssertEqual(HomePurpose.reflect.openLabel(.text), "Reflect in text")
        XCTAssertEqual(HomePurpose.glean.openLabel(.voice), "Glean by voice")
        XCTAssertEqual(HomePurpose.glean.openLabel(.text), "Glean in text")
        XCTAssertEqual(HomePurpose.Mode.allCases.map(\.title), ["Voice", "Text"])
    }
}
