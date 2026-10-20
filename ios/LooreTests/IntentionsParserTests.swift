import XCTest
@testable import Loore

/// Port of `utils/intentions.test.js`, plus `statusState` cases.
final class IntentionsParserTests: XCTestCase {
    private let sample = """
        # Endorsed
        ## Help people become more intentional through Loore
        *held since early — active*
        The meta-intention.

        ## Get Pája to see the cage
        *held since early — fulfilled 2026-06-25*
        Not to punish, but because without her acknowledgment the system can't move.
        - 2026-06-25: User confirmed this is fulfilled.

        # Inferred
        ## Build a life that is coherent regardless of the marriage
        *noticed early — inferred, unconfirmed*
        Valeč, Loore, the spiritual path — none depend on Pája.
        """

    func testSplitsIntoEndorsedInferredSectionsWithEntryCounts() {
        let sections = IntentionsParser.parse(sample)
        XCTAssertEqual(sections.map(\.title), ["Endorsed", "Inferred"])
        XCTAssertEqual(sections[0].entries.count, 2)
        XCTAssertEqual(sections[1].entries.count, 1)
    }

    func testParsesNameStatusBodyAndDatedNotesPerEntry() {
        let cage = IntentionsParser.parse(sample)[0].entries[1]
        XCTAssertEqual(cage.name, "Get Pája to see the cage")
        XCTAssertEqual(cage.status, "held since early — fulfilled 2026-06-25")
        XCTAssertTrue(cage.body.joined(separator: " ").contains("can't move"))
        XCTAssertEqual(cage.notes, ["2026-06-25: User confirmed this is fulfilled."])
    }

    func testAnEntryWithOnlyAStatusLineHasEmptyBodyAndNotes() {
        let meta = IntentionsParser.parse(sample)[0].entries[0]
        XCTAssertEqual(meta.status, "held since early — active")
        XCTAssertEqual(meta.body, ["The meta-intention."])
        XCTAssertEqual(meta.notes, [])
    }

    func testContentWithNoEntriesReturnsNoParsedEntries() {
        let sections = IntentionsParser.parse("just some freeform text, no headings")
        XCTAssertFalse(sections.contains { !$0.entries.isEmpty })
    }

    // MARK: Extra cases

    func testEntriesBeforeAnySectionGoIntoAnUntitledSection() {
        let sections = IntentionsParser.parse("## Loose\n_quietly held_\nbody\n*not a status now*")
        XCTAssertEqual(sections, [IntentionsParser.Section(title: "", entries: [
            IntentionsParser.Entry(name: "Loose", status: "quietly held", body: ["body", "*not a status now*"], notes: []),
        ])])
        XCTAssertEqual(IntentionsParser.parse(nil), [])
    }

    func testStatusState() {
        XCTAssertEqual(IntentionsParser.statusState("held since early — Fulfilled 2026-06-25"), .fulfilled)
        XCTAssertEqual(IntentionsParser.statusState("released 2026-07-01"), .released)
        XCTAssertEqual(IntentionsParser.statusState("noticed early — inferred, unconfirmed"), .inferred)
        XCTAssertEqual(IntentionsParser.statusState("Unconfirmed"), .inferred)
        XCTAssertEqual(IntentionsParser.statusState("held since early — active"), .active)
        XCTAssertEqual(IntentionsParser.statusState(""), .active)
        XCTAssertEqual(IntentionsParser.statusState(nil), .active)
    }
}
