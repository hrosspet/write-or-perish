import XCTest
@testable import Loore

// Imports never offer Public (#677); the other screens keep all three levels.

final class ImportPrivacyTests: XCTestCase {
    func testImportMenuLeavesPublicOut() {
        let levels = PrivacySelectorView.privacyOptions(omitting: [.public]).map { $0.0 }
        XCTAssertEqual(levels, [.private, .circles])
    }

    func testOtherScreensKeepAllLevels() {
        let levels = PrivacySelectorView.privacyOptions(omitting: []).map { $0.0 }
        XCTAssertEqual(levels, [.private, .circles, .public])
    }

    func testImportsStartPrivateWithTheNote() {
        XCTAssertEqual(ImportModel.privacyNote,
                       "Imported entries start private. You can make any of them public afterwards.")
    }
}
