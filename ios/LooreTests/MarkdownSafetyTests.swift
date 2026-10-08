import XCTest
@testable import Loore

/// #441: which markdown images load without a tap. Mirrors `utils/markdownImages.test.js`.
final class MarkdownImageSourceTests: XCTestCase {
    private let env = AppEnvironment.production

    private func classify(_ source: String) -> MarkdownImageSource {
        MarkdownImageSource.classify(source, environment: env)
    }

    func testLooreMediaLoads() {
        XCTAssertEqual(classify("/media/user/1/node/2/pic.png"),
                       .own(URL(string: "https://loore.org/media/user/1/node/2/pic.png")!))
        XCTAssertEqual(classify("https://loore.org/media/a/b.png?v=3"), .own(URL(string: "https://loore.org/media/a/b.png?v=3")!))
        XCTAssertEqual(classify("https://staging.loore.org/media/a.png"), .own(URL(string: "https://staging.loore.org/media/a.png")!))
        XCTAssertEqual(MarkdownImageSource.classify("http://localhost:5010/media/a.png", environment: .local),
                       .own(URL(string: "http://localhost:5010/media/a.png")!))
    }

    func testOutsideImagesWaitForATapAndNameTheirHost() {
        let url = URL(string: "https://evil.example/p.png?q=private%20text")!
        XCTAssertEqual(classify(url.absoluteString), .remote(url, host: "evil.example"))
        XCTAssertEqual(classify("http://Evil.Example:8080/p.png"),
                       .remote(URL(string: "http://Evil.Example:8080/p.png")!, host: "evil.example:8080"))
        // A Loore-looking host that is not Loore's.
        if case .remote(_, let host) = classify("https://loore.org.evil.example/media/a.png") {
            XCTAssertEqual(host, "loore.org.evil.example")
        } else {
            XCTFail("a foreign host must not load by itself")
        }
    }

    func testLoorePathsOutsideMediaDoNotLoadByThemselves() {
        for source in ["/auth/logout", "/media/../auth/logout", "/media/%2e%2e/auth/logout", "/media/..%2fauth/logout",
                       "https://loore.org/api/nodes/1", "https://loore.org/media/"] {
            guard case .remote(_, let host) = classify(source) else {
                XCTFail("\(source) must wait for a tap"); continue
            }
            XCTAssertEqual(host, "loore.org", source)
        }
    }

    func testSourcesThatAreNotWebURLsShowTheAltTextOnly() {
        for source in ["", "   ", "javascript:alert(1)", "data:image/png;base64,AAAA", "file:///etc/hosts",
                       "//evil.example/p.png", "evil.example/p.png", "shortcuts://run"] {
            XCTAssertEqual(classify(source), .none, source)
        }
    }
}

/// #442: a tapped markdown link goes through the router's allowlist; nothing
/// else reaches the system.
@MainActor
final class MarkdownLinkTargetTests: XCTestCase {
    private let env = AppEnvironment.production

    private func resolve(_ link: String) -> MarkdownLinkTarget {
        MarkdownLinkTarget.resolve(link, environment: env)
    }

    func testLooreLinksOpenInTheApp() {
        XCTAssertEqual(resolve("https://loore.org/node/42"), .thread(42))
        XCTAssertEqual(resolve("/node/42"), .thread(42))
        XCTAssertEqual(resolve("/account#model"), .route(.account(anchor: "model")))
        XCTAssertEqual(resolve("https://loore.org/todo"), .route(.todo))
    }

    func testWebAndMailLinksOpenAfterTheTap() {
        let web = URL(string: "https://example.com/page")!
        XCTAssertEqual(resolve(web.absoluteString), .route(.external(web)))
        let mail = URL(string: "mailto:someone@example.com")!
        XCTAssertEqual(resolve(mail.absoluteString), .route(.external(mail)))
    }

    func testOtherSchemesAreNeverHandedToTheSystem() {
        for link in ["shortcuts://run-shortcut?name=x", "sms:+15551234", "tel:123", "facetime://someone",
                     "javascript:alert(1)", "file:///etc/hosts", "example.com/page", "", "http:no-host"] {
            XCTAssertEqual(resolve(link), .ignore, link)
        }
    }

    /// The Updates sheet opens outside links itself (the in-app Safari view
    /// cannot cover a sheet); it uses the same allowlist.
    func testTheUpdatesSheetOpensOnlyWebAndMailLinks() {
        var opened: [String] = []
        for link in ["https://example.com", "mailto:a@b.c", "shortcuts://run", "tel:123", "x.com/u"] {
            Router.openOutsideApp(URL(string: link)!) { opened.append($0.absoluteString) }
        }
        XCTAssertEqual(opened, ["https://example.com", "mailto:a@b.c"])
    }
}
