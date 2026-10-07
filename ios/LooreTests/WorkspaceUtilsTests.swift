import XCTest
@testable import Loore

/// TodoSections (`pages/TodoPage.js`), SourceMix (`pages/ProfilePage.js`),
/// ArtifactKinds (`utils/artifactKinds.js`, `pages/ArtifactsPage.js`) and
/// VersionLabels (`components/VersionHistoryDrawer.js`). Expected values
/// were produced by the web functions.
final class WorkspaceUtilsTests: XCTestCase {
    // MARK: TodoSections

    func testTodoItemsNestByTwoSpaceIndentUnderTheLastItemOneLevelUp() {
        let sections = TodoSections.parse("## Today\n- [ ] a\n  - [x] b\n    - [X] c\n  - d\n- [ ] e")
        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections[0].title, "Today")
        let a = sections[0].items[0]
        XCTAssertEqual(a.text, "a")
        XCTAssertEqual(a.checked, false)
        XCTAssertEqual(a.children.map(\.text), ["b", "d"])
        XCTAssertEqual(a.children[0].checked, true)
        XCTAssertEqual(a.children[0].children.map(\.text), ["c"])
        XCTAssertEqual(a.children[0].children[0].checked, true)
        XCTAssertEqual(a.children[0].children[0].depth, 2)
        XCTAssertEqual(sections[0].items.map(\.text), ["a", "e"])
        XCTAssertEqual(TodoSections.countAll(sections[0].items), 5)
    }

    func testPlainListLinesAreCategoriesWithoutACheckbox() {
        let sections = TodoSections.parse("## Areas\n- Health\n  - [ ] Run\n  - Sleep\n- [ ]  spaced  ")
        let health = sections[0].items[0]
        XCTAssertNil(health.checked)
        XCTAssertEqual(health.text, "Health")
        XCTAssertEqual(health.raw, "- Health")
        XCTAssertEqual(health.children.map(\.text), ["Run", "Sleep"])
        XCTAssertNil(health.children[1].checked)
        // "- [ ]" with text is a checkbox; its text is trimmed.
        XCTAssertEqual(sections[0].items[1].checked, false)
        XCTAssertEqual(sections[0].items[1].text, "spaced")
    }

    func testItemsBeforeAnyHeadingGoIntoAnUntitledSection() {
        let sections = TodoSections.parse("intro text\n- [ ] early\n## Later\n- [x] done")
        XCTAssertEqual(sections.map(\.title), ["", "Later"])
        XCTAssertEqual(sections[0].items.map(\.text), ["early"])
        XCTAssertEqual(sections[1].items.map(\.checked), [true])
        XCTAssertEqual(TodoSections.parse(""), [])
        XCTAssertEqual(TodoSections.parse(nil), [])
        XCTAssertEqual(TodoSections.parse("just text\n### h3"), [])
    }

    func testAnItemWithNoParentOneLevelUpStaysTopLevel() {
        let sections = TodoSections.parse("## S\n- [ ] a\n      - [ ] deep\n- [ ] b")
        XCTAssertEqual(sections[0].items.map(\.text), ["a", "deep", "b"])
        XCTAssertEqual(sections[0].items[1].depth, 3)
        XCTAssertEqual(TodoSections.countAll(sections[0].items), 3)
    }

    func testTodoUpdatedByLabel() {
        XCTAssertEqual(TodoSections.updatedByLabel("user"), "edited manually")
        XCTAssertEqual(TodoSections.updatedByLabel("manual"), "edited manually")
        XCTAssertEqual(TodoSections.updatedByLabel("orient_session"), "Orient session")
        XCTAssertEqual(TodoSections.updatedByLabel("voice_session"), "Voice")
        XCTAssertEqual(TodoSections.updatedByLabel("revert"), "reverted")
        XCTAssertEqual(TodoSections.updatedByLabel("import"), "imported")
        XCTAssertEqual(TodoSections.updatedByLabel("claude-opus-4"), "claude-opus-4")
        XCTAssertEqual(TodoSections.updatedByLabel(nil), "")
    }

    // MARK: SourceMix

    func testSourceMixRoundsHalvesUpAndExcludesLoore() {
        XCTAssertEqual(SourceMix.format([("loore", 5), ("twitter", 95)]),
                       " (95% public tweets)")
        // 1 of 200 = 0.5% rounds up to 1%, like Math.round.
        XCTAssertEqual(SourceMix.format([("twitter", 1), ("loore", 199)]),
                       " (1% public tweets)")
        // 2.5% → 3%.
        XCTAssertEqual(SourceMix.format([("chatgpt", 1), ("loore", 39)]),
                       " (3% ChatGPT imports)")
        XCTAssertEqual(SourceMix.jsRound(0.49999999999999994), 0)
        XCTAssertEqual(SourceMix.jsRound(2.5), 3)
    }

    func testSourceMixOrdersByShareAndKeepsInputOrderOnTies() {
        XCTAssertEqual(SourceMix.format([("claude", 10), ("twitter", 30),
                                         ("markdown", 10), ("other", 50)]),
                       " (50% other, 30% public tweets, 10% Claude imports, 10% markdown imports)")
        XCTAssertEqual(SourceMix.format([("twitter", 1), ("claude", 1)]),
                       " (50% public tweets, 50% Claude imports)")
        // The dictionary form iterates keys in code-point order, as Flask sends them.
        XCTAssertEqual(SourceMix.format(["twitter": 1, "claude": 1]), " (50% Claude imports, 50% public tweets)")
    }

    func testSourceMixIsEmptyForPureLooreZeroTotalsAndNoStats() {
        XCTAssertEqual(SourceMix.format([("loore", 1000)]), "")
        XCTAssertEqual(SourceMix.format([("twitter", 0)]), "")
        XCTAssertEqual(SourceMix.format([("twitter", 1), ("loore", 1000)]), "")
        XCTAssertEqual(SourceMix.format([]), "")
        XCTAssertEqual(SourceMix.format(nil), "")
    }

    // MARK: ArtifactKinds

    func testBuiltinKindsSortFirstInCanonicalOrderThenByTitle() {
        let items: [(kind: String, title: String?)] = [
            ("memory", "Memory"), ("zeta", "Zeta"), ("alpha", nil), ("scratchpad", "Scratchpad"), ("b", ""),
            ("intentions", "Intentions"), ("ai_preferences", "AI Preferences"), ("Beta", "beta"),
            ("custom", "Custom"), ("predictions", "Predictions"), ("aa", "Ärger"),
        ]
        XCTAssertEqual(items.sorted(by: ArtifactKinds.areInIncreasingOrder).map(\.kind),
                       ["intentions", "predictions", "memory", "scratchpad", "ai_preferences",
                        "alpha", "aa", "b", "Beta", "custom", "zeta"])
        XCTAssertEqual(["Zeta", "beta", "Ärger", "alpha"].sorted(by: ArtifactKinds.titleSortsBefore),
                       ["alpha", "Ärger", "beta", "Zeta"])
        XCTAssertTrue(ArtifactKinds.isBuiltinKind("memory"))
        XCTAssertFalse(ArtifactKinds.isBuiltinKind("todo"))
    }

    func testTitleFromKind() {
        XCTAssertEqual(ArtifactKinds.titleFromKind("ai_preferences"), "Ai Preferences")
        XCTAssertEqual(ArtifactKinds.titleFromKind("my-kind"), "My Kind")
        XCTAssertEqual(ArtifactKinds.titleFromKind("x2y"), "X2y")
        XCTAssertEqual(ArtifactKinds.titleFromKind("caféx"), "CaféX")  // JS \b\w is ASCII-only
    }

    func testKindSlugValidationAndCreateFormSanitizer() {
        XCTAssertTrue(ArtifactKinds.isValidKind("reading_list-2"))
        XCTAssertTrue(ArtifactKinds.isValidKind("9"))
        XCTAssertTrue(ArtifactKinds.isValidKind(String(repeating: "a", count: 48)))
        XCTAssertFalse(ArtifactKinds.isValidKind(String(repeating: "a", count: 49)))
        XCTAssertFalse(ArtifactKinds.isValidKind("-lead"))
        XCTAssertFalse(ArtifactKinds.isValidKind("_lead"))
        XCTAssertFalse(ArtifactKinds.isValidKind("Upper"))
        XCTAssertFalse(ArtifactKinds.isValidKind(""))
        XCTAssertFalse(ArtifactKinds.isValidKind("kind\n"))
        XCTAssertEqual(ArtifactKinds.sanitizeKindInput("My Kind!"), "my-kind-")
        XCTAssertEqual(ArtifactKinds.sanitizeKindInput("emoji😀x"), "emoji--x")
        XCTAssertEqual(ArtifactKinds.sanitizeKindInput("İstanbul"), "i-stanbul")
        XCTAssertEqual(ArtifactKinds.sanitizeKindInput("ok_kind-1"), "ok_kind-1")
    }

    // MARK: VersionLabels

    func testVersionLabel() {
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "user", generationType: nil), "Manual edit")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "manual", generationType: "update"), "Manual edit")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "revert", generationType: nil), "Reverted")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "orient_session", generationType: nil), "Orient session")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "voice_session", generationType: nil), "Voice")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "import", generationType: nil), "Imported")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "gpt-5", generationType: "update"), "Auto-updated (gpt-5)")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "gpt-5", generationType: "iterative"),
                       "Iterative build (gpt-5)")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "gpt-5", generationType: "integration"),
                       "Integrated profile (gpt-5)")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: "gpt-5", generationType: nil), "Auto-generated (gpt-5)")
        XCTAssertEqual(VersionLabels.versionLabel(generatedBy: nil, generationType: nil), "Auto-generated (null)")
    }
}
