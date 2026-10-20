import XCTest

final class LaunchSmokeUITests: XCTestCase {
    func testLaunches() {
        let app = XCUIApplication()
        app.launchArguments += ["-LooreResetState", "YES"]
        app.launch()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
    }
}
