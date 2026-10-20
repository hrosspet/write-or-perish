import XCTest
@testable import Loore

/// Port of `utils/diff.test.js`, plus cases for the jsdiff tokenizer and
/// the VersionHistoryDrawer helpers.
final class LineDiffTests: XCTestCase {
    private func join(_ ops: [LineDiff.Op]) -> String {
        ops.map { "\($0.type.rawValue.prefix(1)):\($0.text)" }.joined(separator: "|")
    }

    private func lines(_ ops: [LineDiff.Op], _ type: LineDiff.Kind) -> [LineDiff.Op] {
        ops.filter { $0.type == type }
    }

    // MARK: computeLineDiff

    func testIdenticalTextsProduceOnlySameLines() {
        let ops = LineDiff.computeLineDiff("a\nb\nc", "a\nb\nc")
        XCTAssertTrue(ops.allSatisfy { $0.type == .same })
        XCTAssertEqual(ops.count, 3)
    }

    func testAppendedLineAtTheEnd() {
        XCTAssertEqual(join(LineDiff.computeLineDiff("a\nb", "a\nb\nc")), "s:a|s:b|a:c")
    }

    func testChangedLineIsDelPlusAdd() {
        XCTAssertEqual(join(LineDiff.computeLineDiff("a\nold\nc", "a\nnew\nc")), "s:a|d:old|a:new|s:c")
    }

    func testRemovedLine() {
        XCTAssertEqual(join(LineDiff.computeLineDiff("a\nb\nc", "a\nc")), "s:a|d:b|s:c")
    }

    func testInsertionInTheMiddleOfALongCommonText() {
        let base = (0..<50).map { "line \($0)" }
        let modified = Array(base[0..<25]) + ["INSERTED"] + Array(base[25...])
        let ops = LineDiff.computeLineDiff(base.joined(separator: "\n"), modified.joined(separator: "\n"))
        XCTAssertEqual(lines(ops, .add), [LineDiff.Op(type: .add, text: "INSERTED")])
        XCTAssertEqual(lines(ops, .del).count, 0)
        XCTAssertEqual(ops.count, 51)
    }

    func testEmptyOldTextIsAllAdditions() {
        let ops = LineDiff.computeLineDiff("", "a\nb")
        // "" splits to [""]: one empty line, tolerated as del or same-empty.
        let adds = lines(ops, .add).map(\.text)
        XCTAssertTrue(adds.contains("a"))
        XCTAssertTrue(adds.contains("b"))
    }

    // MARK: collapseUnchanged

    func testCollapseFoldsLongUnchangedRunsKeepingContextAroundChanges() {
        let base = (0..<30).map { "line \($0)" }
        let modified = Array(base[0..<15]) + ["NEW"] + Array(base[15...])
        let rows = LineDiff.collapseUnchanged(
            LineDiff.computeLineDiff(base.joined(separator: "\n"), modified.joined(separator: "\n")), context: 2)

        let skips = rows.compactMap { row -> Int? in
            if case .skip(let count) = row { return count }
            return nil
        }
        XCTAssertEqual(skips.count, 2)
        // 15 leading same lines → skip 13, keep 2 before the change.
        XCTAssertEqual(skips[0], 13)
        // 15 trailing same lines → keep 2 after the change, skip 13.
        XCTAssertEqual(skips[1], 13)
        let addIdx = rows.firstIndex { row in
            if case .line(let op) = row { return op.type == .add }
            return false
        }!
        XCTAssertEqual(rows[addIdx - 1], .line(LineDiff.Op(type: .same, text: "line 14")))
        XCTAssertEqual(rows[addIdx - 2], .line(LineDiff.Op(type: .same, text: "line 13")))
        XCTAssertEqual(rows[addIdx - 3], .skip(count: 13))
    }

    func testCollapseShortUnchangedRunsAreNotFolded() {
        let rows = LineDiff.collapseUnchanged(LineDiff.computeLineDiff("a\nb\nold", "a\nb\nnew"), context: 2)
        XCTAssertFalse(rows.contains { if case .skip = $0 { return true } else { return false } })
    }

    // MARK: refineWordDiffs

    func testPairedSimilarLinesGainWordSegments() {
        let ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff(
            "you write a brief acknowledgment of what you are doing",
            "you should write one short sentence naming what you are doing"))
        let del = ops.first { $0.type == .del }!
        let add = ops.first { $0.type == .add }!
        XCTAssertNotNil(del.segments)
        XCTAssertNotNil(add.segments)
        // Unchanged words are unmarked; changed words are marked.
        XCTAssertEqual(del.segments?.first { $0.text.contains("acknowledgment") }?.changed, true)
        XCTAssertEqual(add.segments?.first { $0.text.contains("sentence") }?.changed, true)
        XCTAssertTrue((add.segments ?? []).filter { !$0.changed }.map(\.text).joined().contains("what you are doing"))
        // Reassembled segments reproduce the full lines.
        XCTAssertEqual(del.segments?.map(\.text).joined(), del.text)
        XCTAssertEqual(add.segments?.map(\.text).joined(), add.text)
    }

    func testDissimilarPairsAreLeftUnrefined() {
        let ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff(
            "completely unrelated old line about apples",
            "nothing shared here whatsoever xyz"))
        XCTAssertNil(ops.first { $0.type == .del }!.segments)
        XCTAssertNil(ops.first { $0.type == .add }!.segments)
    }

    func testUnpairedAddsAndDelsStayUnrefined() {
        let ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff("a\nb", "a\nb\npure addition"))
        XCTAssertNil(ops.first { $0.type == .add }!.segments)
    }

    func testMultiLineBlocksPairPositionally() {
        let ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff(
            "first old line here\nsecond old line here",
            "first new line here\nsecond new line here"))
        let dels = lines(ops, .del)
        let adds = lines(ops, .add)
        XCTAssertEqual(dels.count, 2)
        XCTAssertEqual(adds.count, 2)
        XCTAssertTrue(dels.allSatisfy { $0.segments != nil })
        XCTAssertTrue(adds.allSatisfy { $0.segments != nil })
        XCTAssertTrue(adds[0].segments?.first { $0.changed }?.text.contains("new") ?? false)
    }

    // MARK: Extra cases (outputs checked against jsdiff 9 / the web)

    func testWordDiffMatchesJsdiffChangeObjects() {
        XCTAssertEqual(LineDiff.diffWordsWithSpace("foo bar baz", "foo qux baz"), [
            LineDiff.WordChange(value: "foo ", added: false, removed: false, count: 2),
            LineDiff.WordChange(value: "bar", added: false, removed: true, count: 1),
            LineDiff.WordChange(value: "qux", added: true, removed: false, count: 1),
            LineDiff.WordChange(value: " baz", added: false, removed: false, count: 2),
        ])
        XCTAssertEqual(LineDiff.diffWordsWithSpace("", ""), [])
    }

    func testTokenizerSplitsWordsSpaceRunsPunctuationAndNewlines() {
        XCTAssertEqual(LineDiff.wordsWithSpaceTokens("Čaj, naïve  x2\r\n\n\t😀ok"),
                       ["Čaj", ",", " ", "naïve", "  ", "x2", "\r\n", "\n", "\t", "😀", "ok"])
        // × is not a word character; a lone \r is its own token.
        XCTAssertEqual(LineDiff.wordsWithSpaceTokens("a×b\rc"), ["a", "×", "b", "\r", "c"])
    }

    func testRefinedSegmentsMatchTheWeb() {
        let ops = LineDiff.refineWordDiffs(LineDiff.computeLineDiff("foo bar baz", "foo qux baz"))
        XCTAssertEqual(ops[0].segments, [
            LineDiff.Segment(changed: false, text: "foo "), LineDiff.Segment(changed: true, text: "bar"),
            LineDiff.Segment(changed: false, text: " baz"),
        ])
        XCTAssertEqual(ops[1].segments, [
            LineDiff.Segment(changed: false, text: "foo "), LineDiff.Segment(changed: true, text: "qux"),
            LineDiff.Segment(changed: false, text: " baz"),
        ])
    }

    func testLinesCompareByCodeUnitsLikeJS() {
        // Precomposed vs decomposed é: equal for Swift `==`, different for JS `===`.
        XCTAssertEqual(join(LineDiff.computeLineDiff("caf\u{E9}", "cafe\u{301}")), "d:caf\u{E9}|a:cafe\u{301}")
        // "\r\n" keeps its "\r" on the line, as JS split('\n') does.
        XCTAssertEqual(join(LineDiff.computeLineDiff("a\r\nb", "a\r\nc")), "s:a\r|d:b|a:c")
    }

    func testLargeMiddlesFallBackToOneDelAndOneAddBlock() {
        let old = (0..<1600).map { "old \($0)" }.joined(separator: "\n")
        let new = (0..<1600).map { "new \($0)" }.joined(separator: "\n")
        let ops = LineDiff.computeLineDiff(old, new)
        XCTAssertEqual(ops.count, 3200)
        XCTAssertTrue(ops[0..<1600].allSatisfy { $0.type == .del })
        XCTAssertTrue(ops[1600...].allSatisfy { $0.type == .add })
    }

    func testChangedCountAndHeavyRewriteRule() {
        let small = LineDiff.computeLineDiff("a\nb", "c\nd")
        XCTAssertEqual(LineDiff.changedCount(small), 4)
        XCTAssertFalse(LineDiff.isHeavyRewrite(small))  // > 50% but not > 40
        let old = (0..<30).map { "old \($0)" }.joined(separator: "\n")
        let new = (0..<30).map { "new \($0)" }.joined(separator: "\n")
        let rewrite = LineDiff.computeLineDiff(old, new)
        XCTAssertEqual(LineDiff.changedCount(rewrite), 60)
        XCTAssertTrue(LineDiff.isHeavyRewrite(rewrite))
        let base = (0..<100).map { "line \($0)" }
        func editFirst(_ n: Int) -> [LineDiff.Op] {
            let edited = base.enumerated().map { $0.offset < n ? "edited \($0.offset)" : $0.element }
            return LineDiff.computeLineDiff(base.joined(separator: "\n"), edited.joined(separator: "\n"))
        }
        // 90 of 145 ops changed (62%).
        XCTAssertEqual(LineDiff.changedCount(editFirst(45)), 90)
        XCTAssertTrue(LineDiff.isHeavyRewrite(editFirst(45)))
        // 50 of 125 ops changed (40%).
        XCTAssertEqual(LineDiff.changedCount(editFirst(25)), 50)
        XCTAssertFalse(LineDiff.isHeavyRewrite(editFirst(25)))
    }

    func testVersionDiffBundlesTheDrawerValues() {
        let diff = VersionDiff(old: "a\nold\nc", new: "a\nnew\nc")
        XCTAssertEqual(join(diff.ops), "s:a|d:old|a:new|s:c")
        XCTAssertEqual(diff.changedCount, 2)
        XCTAssertFalse(diff.isHeavyRewrite)
        XCTAssertFalse(diff.hasNoChanges)
        XCTAssertEqual(diff.rows.count, 4)
        XCTAssertTrue(VersionDiff(old: "same", new: "same").hasNoChanges)
        XCTAssertNil(VersionDiff(old: "apples", new: "oranges").ops[0].segments)
        // Shared spaces count toward similarity (4 of 9 units ≥ 0.4), as on the web.
        XCTAssertNotNil(VersionDiff(old: "a b c d e", new: "z y x w v").ops[0].segments)
    }

    func testNoChangesAndSkipLabel() {
        XCTAssertTrue(LineDiff.hasNoChanges(LineDiff.collapseUnchanged(LineDiff.computeLineDiff("a\nb", "a\nb"))))
        XCTAssertTrue(LineDiff.hasNoChanges([]))
        XCTAssertFalse(LineDiff.hasNoChanges(LineDiff.collapseUnchanged(LineDiff.computeLineDiff("a", "b"))))
        XCTAssertEqual(LineDiff.skipLabel(count: 1), "⋯ 1 unchanged line ⋯")
        XCTAssertEqual(LineDiff.skipLabel(count: 13), "⋯ 13 unchanged lines ⋯")
    }
}
