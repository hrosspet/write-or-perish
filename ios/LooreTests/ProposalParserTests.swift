import XCTest
@testable import Loore

/// One-to-one port of `frontend/src/components/ProposalInline.test.js`.
final class ProposalParserTests: XCTestCase {
    private typealias P = ProposalParser

    // Category badges (issue + feedback) take the first line of their section only.

    func testFeedbackCategoryTakesTheFirstLineOnly() {
        let text = [
            "### Feedback",
            "The text mode feels clean and fast.",
            "### Feedback category",
            "praise",
            "",
            "Take a look and let me know — once you confirm I'll send it.",
        ].joined(separator: "\n")
        let parsed = P.parseOrientResponse(text)
        XCTAssertEqual(parsed.feedback, "The text mode feels clean and fast.")
        XCTAssertEqual(parsed.feedbackCategory, "praise")
    }

    func testIssueCategoryTakesTheFirstLineOnly() {
        let text = [
            "### Issue Title",
            "Add dark mode toggle",
            "### Description",
            "Users want a dark mode.",
            "### Category",
            "enhancement",
            "",
            "Want me to file this?",
        ].joined(separator: "\n")
        XCTAssertEqual(P.parseOrientResponse(text).issueCategory, "enhancement")
    }

    // Trailing commentary after a single-line category value survives stripping.

    func testStripKeepsIntroAndTrailingCommentaryAfterFeedbackCategory() {
        let text = [
            "That's great to hear — I'll draft that for you now.",
            "",
            "### Feedback",
            "The voice mode feels genuinely magical.",
            "",
            "### Feedback category",
            "praise",
            "",
            "Let me know if that captures it.",
        ].joined(separator: "\n")
        let body = P.stripProposalSections(text)
        XCTAssertTrue(body.contains("That's great to hear"))
        XCTAssertTrue(body.contains("Let me know if that captures it."))
        XCTAssertFalse(body.contains("### Feedback"))
        XCTAssertFalse(body.contains("genuinely magical"))
        XCTAssertFalse(JSRegex.test(body, #"(^|\n)praise(\n|$)"#))
    }

    func testStripKeepsTrailingCommentaryAfterIssueCategory() {
        let text = [
            "Here is the issue I drafted.",
            "### Issue Title",
            "Add dark mode",
            "### Description",
            "Users want dark mode.",
            "### Category",
            "enhancement",
            "",
            "Sound right?",
        ].joined(separator: "\n")
        let body = P.stripProposalSections(text)
        XCTAssertTrue(body.contains("Here is the issue I drafted."))
        XCTAssertTrue(body.contains("Sound right?"))
        XCTAssertFalse(body.contains("### Category"))
        XCTAssertFalse(body.contains("Add dark mode"))
    }

    func testSplitSeparatesLeadInFromTrailingCommentary() {
        let text = [
            "Glad you're enjoying it.",
            "",
            "### Feedback",
            "Love the voice mode.",
            "",
            "### Feedback category",
            "praise",
            "",
            "Thanks for building this!",
        ].joined(separator: "\n")
        let split = P.splitProposalText(text)
        XCTAssertEqual(split.before, "Glad you're enjoying it.")
        XCTAssertEqual(split.after, "Thanks for building this!")
    }

    func testSplitReturnsEmptyAfterWithoutTrailingCommentary() {
        let text = [
            "Here is the issue.",
            "### Issue Title",
            "Add dark mode",
            "### Category",
            "enhancement",
        ].joined(separator: "\n")
        let split = P.splitProposalText(text)
        XCTAssertEqual(split.before, "Here is the issue.")
        XCTAssertEqual(split.after, "")
    }

    // MARK: Fenced :::share blocks

    private let multiShare = [
        "Two pieces, as you asked.",
        "",
        ":::share insight",
        "### On attention",
        "",
        "First post, with its own markdown headings.",
        ":::",
        "",
        "And the second:",
        "",
        ":::share exploration",
        "Second post body.",
        ":::",
        "",
        "Say the word.",
    ].joined(separator: "\n")

    func testParseShareBlocksHandlesMultipleBlocksAndInnerHeadings() {
        let shares = P.parseShareBlocks(multiShare)
        XCTAssertEqual(shares.count, 2)
        XCTAssertTrue(shares[0].content.hasPrefix("### On attention"))
        XCTAssertEqual(shares[0].type, "insight")
        XCTAssertEqual(shares[1].content, "Second post body.")
        XCTAssertEqual(shares[1].type, "exploration")
    }

    func testParseShareBlocksUnclosedFenceRunsToEndAndLegacyHeadingsFallBack() {
        XCTAssertEqual(P.parseShareBlocks("lead-in\n\n:::share need\nno closing fence"),
                       [P.ShareBlock(content: "no closing fence", type: "need")])
        XCTAssertEqual(P.parseShareBlocks("### Share\nold style body\n### Share type\nneed"),
                       [P.ShareBlock(content: "old style body", type: "need")])
    }

    func testHasShareBlocksMatchesFencesOnlyAndHasProposalSectionsIncludesThem() {
        XCTAssertTrue(P.hasShareBlocks(multiShare))
        XCTAssertFalse(P.hasShareBlocks("prose about :::share syntax"))
        XCTAssertFalse(P.hasShareBlocks("### Share\nlegacy"))
        XCTAssertTrue(P.hasProposalSections(":::share\nbody\n:::"))
    }

    func testSplitExcludesShareFences() {
        let split = P.splitProposalText(multiShare)
        XCTAssertEqual(split.before, "Two pieces, as you asked.")
        XCTAssertTrue(split.after.contains("And the second:"))
        XCTAssertTrue(split.after.contains("Say the word."))
        XCTAssertFalse(split.after.contains(":::"))
        XCTAssertFalse(split.after.contains("Second post body."))
    }

    func testLeadInStartingWithNotedIsNotAPhantomTodoNote() {
        let text = "Noted both — worth keeping.\n\n:::share insight\n### Inner\nbody\n:::\n\nDone."
        let parsed = P.parseOrientResponse(text)
        XCTAssertNil(parsed.note)
        XCTAssertNil(parsed.completed)
    }

    func testHeadingsInsideAShareBodyDoNotTriggerOtherSections() {
        let text = "Lead.\n\n:::share insight\n### Completed\n- x\n\n### Feedback\ny\n:::"
        let parsed = P.parseOrientResponse(text)
        XCTAssertNil(parsed.completed)
        XCTAssertNil(parsed.feedback)
        XCTAssertTrue(P.hasProposalSections(text))
    }

    func testStandaloneNoteishHeadingStaysVisibleInTheProse() {
        let text = [
            "Lead-in.",
            "### A note on process",
            "keep this visible",
            "",
            ":::share insight",
            "share body",
            ":::",
        ].joined(separator: "\n")
        let split = P.splitProposalText(text)
        XCTAssertTrue((split.before + split.after).contains("### A note on process"))
        XCTAssertTrue((split.before + split.after).contains("keep this visible"))
        XCTAssertFalse((split.before + split.after).contains("share body"))
    }

    func testNoteSectionBelongsToTheTodoCardWhenTaskSectionsExist() {
        let split = P.splitProposalText("Intro.\n### Completed\n- a\n### Note\nnice work")
        XCTAssertEqual(split.before, "Intro.")
        XCTAssertEqual(split.after, "")
    }

    func testShareOnlySplitLeavesTheNodesOwnHeadingsInTheProse() {
        let text = [
            "### My own heading",
            "my prose",
            ":::share insight",
            "the share",
            ":::",
            "closing thought",
        ].joined(separator: "\n")
        let split = P.splitProposalText(text, shareOnly: true)
        XCTAssertTrue(split.before.contains("### My own heading"))
        XCTAssertTrue(split.before.contains("my prose"))
        XCTAssertEqual(split.after, "closing thought")
        XCTAssertFalse((split.before + split.after).contains("the share"))
    }

    // MARK: moveProposalItem (#377 / #378)

    private let newTasksOnly = [
        "Lead-in.",
        "",
        "### New Tasks",
        "- write the intro",
        "- call the bank",
        "",
        "### Note",
        "Good momentum.",
    ].joined(separator: "\n")

    func testTickingWithNoCompletedSectionCreatesOneAboveNewTasks() {
        let ticked = P.moveProposalItem(newTasksOnly, itemText: "write the intro", from: "new task", to: "completed")
        XCTAssertEqual(ticked, [
            "Lead-in.",
            "",
            "### Completed",
            "- write the intro",
            "",
            "### New Tasks",
            "- call the bank",
            "",
            "### Note",
            "Good momentum.",
        ].joined(separator: "\n"))
        let parsed = P.parseOrientResponse(ticked)
        XCTAssertEqual(parsed.completed, "- write the intro")
        XCTAssertEqual(parsed.newTasks, "- call the bank")
    }

    func testUntickingMovesTheTaskBackToNewTasks() {
        let ticked = P.moveProposalItem(newTasksOnly, itemText: "write the intro", from: "new task", to: "completed")
        let unticked = P.moveProposalItem(ticked, itemText: "write the intro", from: "completed", to: "new task",
                                          prepend: true)
        let parsed = P.parseOrientResponse(unticked)
        XCTAssertTrue(parsed.completed?.isEmpty ?? true)
        XCTAssertEqual(parsed.newTasks, "- write the intro\n- call the bank")
        XCTAssertEqual(parsed.note, "Good momentum.")
    }

    func testUntickingWithNoNewTasksSectionCreatesOneBelowCompleted() {
        let text = [
            "### Completed",
            "- ship it",
            "- reply to Ana",
            "",
            "### Priority Order",
            "1. Reply to Ana — quick win",
        ].joined(separator: "\n")
        let unticked = P.moveProposalItem(text, itemText: "ship it", from: "completed", to: "new task", prepend: true)
        XCTAssertEqual(unticked, [
            "### Completed",
            "- reply to Ana",
            "",
            "### New Tasks",
            "- ship it",
            "",
            "### Priority Order",
            "1. Reply to Ana — quick win",
        ].joined(separator: "\n"))
    }

    func testUntickingTheOnlyItemOfATrailingCompletedSectionAppendsNewTasks() {
        let unticked = P.moveProposalItem("Intro.\n\n### Completed\n- ship it\n", itemText: "ship it",
                                          from: "completed", to: "new task")
        XCTAssertEqual(unticked, "Intro.\n\n### Completed\n\n### New Tasks\n- ship it\n")
        XCTAssertEqual(P.parseOrientResponse(unticked).newTasks, "- ship it")
    }

    func testTickingWithBothSectionsPresentAppendsToTheExistingCompletedList() {
        let text = "### Completed\n- done one\n\n### New Tasks\n- todo one\n- todo two"
        XCTAssertEqual(P.moveProposalItem(text, itemText: "todo two", from: "new task", to: "completed"),
                       "### Completed\n- done one\n- todo two\n\n### New Tasks\n- todo one")
    }

    func testMovingAnItemThatIsNotInTheSourceSectionLeavesContentUnchanged() {
        XCTAssertEqual(P.moveProposalItem(newTasksOnly, itemText: "not there", from: "new task", to: "completed"),
                       newTasksOnly)
    }

    // MARK: Extra coverage for the card's item parsers (not in the jest file)

    func testTodoAndPriorityItemParsing() {
        XCTAssertEqual(P.parseTodoItems("- [ ] **Call** mum\n* [x] done\n\n- plain"), ["Call mum", "done", "plain"])
        XCTAssertEqual(P.parsePriorityItems("1. Reply to Ana — quick win\n2) Garden (weekend)\n- Czech"), [
            P.PriorityItem(text: "Reply to Ana", hint: "quick win"),
            P.PriorityItem(text: "Garden", hint: "weekend"),
            P.PriorityItem(text: "Czech", hint: ""),
        ])
    }
}
