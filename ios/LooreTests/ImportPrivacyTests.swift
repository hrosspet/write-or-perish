import XCTest
@testable import Loore

// Imports are always private (#677): the import screens show a line instead of
// the privacy choice, and the iPhone app keeps sending "private".

final class ImportPrivacyTests: XCTestCase {
    func testImportsShowTheNoteInPlaceOfTheChoice() {
        XCTAssertEqual(ImportModel.privacyNote,
                       "Imported entries are private. You can share or publish the ones you choose afterwards.")
    }

    func testOtherScreensStillOfferAllLevels() {
        let levels = PrivacySelectorView.privacyOptions.map { $0.0 }
        XCTAssertEqual(levels, [.private, .circles, .public])
    }

    func testAResultImportedPrivateAfterAnOlderRequestSaysSo() {
        let result = ImportResult(["created": 2, "private_note": ImportModel.privacyNote])
        XCTAssertEqual(result.notes.first, ImportModel.privacyNote)
        XCTAssertNil(ImportResult(["created": 2]).privateNote)
    }
}
