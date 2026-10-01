import XCTest
import SwiftUI
@testable import Loore

/// Link routing, a superset of `utils/nodeLinks.test.js` recognition cases.
final class AppRouteTests: XCTestCase {
    private let env = AppEnvironment.production

    func testNodeLinksOnOwnHosts() {
        for link in ["https://loore.org/node/42", "https://www.loore.org/node/42", "https://staging.loore.org/node/42",
                     "/node/42", "http://loore.org/node/42"] {
            XCTAssertEqual(AppRoute.parse(link, environment: env), .thread(id: 42, awaitLLM: nil), link)
            XCTAssertEqual(AppRoute.nodeId(inLink: link, environment: env), 42, link)
        }
        XCTAssertEqual(AppRoute.parse("/node/42?awaitLlm=43", environment: env), .thread(id: 42, awaitLLM: 43))
        XCTAssertEqual(AppRoute.parse("http://localhost:3001/node/7", environment: .local), .thread(id: 7, awaitLLM: nil))
    }

    func testForeignHostsAreExternal() {
        XCTAssertEqual(AppRoute.parse("https://example.com/node/42", environment: env),
                       .external(URL(string: "https://example.com/node/42")!))
        XCTAssertNil(AppRoute.nodeId(inLink: "https://notloore.org/node/42", environment: env))
        XCTAssertEqual(AppRoute.parse("mailto:info@loore.org", environment: env), .external(URL(string: "mailto:info@loore.org")!))
    }

    func testWebPaths() {
        XCTAssertEqual(AppRoute.parse("/account#model", environment: env), .account(anchor: "model"))
        XCTAssertEqual(AppRoute.parse("/import#x-bookmarks", environment: env), .importData(anchor: "x-bookmarks"))
        XCTAssertEqual(AppRoute.parse("/dashboard", environment: env), .profile)
        XCTAssertEqual(AppRoute.parse("/feed", environment: env), .log)
        XCTAssertEqual(AppRoute.parse("/ai-preferences", environment: env), .artifacts(kind: "ai_preferences"))
        XCTAssertEqual(AppRoute.parse("/artifacts/intentions", environment: env), .artifacts(kind: "intentions"))
        XCTAssertEqual(AppRoute.parse("/references/9", environment: env), .reference(id: 9))
        XCTAssertEqual(AppRoute.parse("/prompts/voice", environment: env), .prompt(key: "voice"))
        XCTAssertEqual(AppRoute.parse("/voice?parent=5&resume=6", environment: env), .voice(parentId: 5, resumeLLMId: 6))
        XCTAssertEqual(AppRoute.parse("/confirm-email?token=abc", environment: env), .confirmEmail(token: "abc"))
        XCTAssertEqual(AppRoute.parse("/@seowriter", environment: env), .webPage(path: "/@seowriter"))
        XCTAssertEqual(AppRoute.parse("/@seowriter/a-slug", environment: env), .webPage(path: "/@seowriter/a-slug"))
        XCTAssertEqual(AppRoute.parse("/dashboard/seowriter", environment: env), .webPage(path: "/@seowriter"))
        XCTAssertEqual(AppRoute.permalink(in: "/@seowriter/a-slug")?.username, "seowriter")
        XCTAssertEqual(AppRoute.permalink(in: "/@seowriter/a-slug")?.slug, "a-slug")
        XCTAssertNil(AppRoute.permalink(in: "/@seowriter"))
        XCTAssertNil(AppRoute.permalink(in: "/@/a-slug"))
        XCTAssertEqual(AppRoute.parse("/vision", environment: env), .webPage(path: "/vision"))
        XCTAssertEqual(AppRoute.parse("/somewhere/else", environment: env), .home)
        XCTAssertEqual(AppRoute.parse("/", environment: env), .home)
        XCTAssertNil(AppRoute.parse("", environment: env))
    }

    func testTabs() {
        XCTAssertEqual(AppRoute.profile.preferredTab, .artifacts)
        XCTAssertEqual(AppRoute.todo.preferredTab, .artifacts)
        XCTAssertEqual(AppRoute.account(anchor: nil).preferredTab, .more)
        XCTAssertNil(AppRoute.thread(id: 1, awaitLLM: nil).preferredTab, "threads push on the current tab")
        XCTAssertEqual(AppRoute.thread(id: 3, awaitLLM: nil).webPath, "/node/3")
    }

    @MainActor
    func testRouterOpens() {
        let router = Router()
        router.selectedTab = .log
        router.open(.thread(id: 5, awaitLLM: nil), environment: env, commonsAvailable: false)
        XCTAssertEqual(router.selectedTab, .log)
        XCTAssertEqual(router.path(for: .log), [.thread(id: 5, awaitLLM: nil)])

        router.open(.profile, environment: env, commonsAvailable: false)
        XCTAssertEqual(router.selectedTab, .artifacts)
        XCTAssertEqual(router.path(for: .artifacts), [])

        router.open(.commons, environment: env, commonsAvailable: false)
        XCTAssertEqual(router.selectedTab, .reflect, "no Commons tab without the flag")
        XCTAssertEqual(router.path(for: .reflect), [.commons])

        router.open(.webPage(path: "/vision"), environment: env, commonsAvailable: false)
        XCTAssertEqual(router.presentedWeb?.url.absoluteString, "https://loore.org/vision")
        XCTAssertEqual(router.presentedWeb?.kind, .safari)

        router.open(.admin, environment: env, commonsAvailable: false)
        XCTAssertEqual(router.presentedWeb?.kind, .authenticated)
    }
}

final class DesignSystemTests: XCTestCase {
    func testBundledFontsResolve() {
        for name in LooreFontFace.allNames {
            let font = UIFont(name: name, size: 16)
            XCTAssertNotNil(font, "\(name) is not available")
        }
    }

    func testFontWeightsDiffer() throws {
        func weight(_ name: String) throws -> CGFloat {
            let font = try XCTUnwrap(UIFont(name: name, size: 16), name)
            let traits = font.fontDescriptor.object(forKey: .traits) as? [UIFontDescriptor.TraitKey: Any]
            return traits?[.weight] as? CGFloat ?? .nan
        }
        let light = try weight(LooreFontFace.sansName(.light))
        let regular = try weight(LooreFontFace.sansName(.regular))
        let medium = try weight(LooreFontFace.sansName(.medium))
        XCTAssertLessThan(light, regular)
        XCTAssertLessThan(regular, medium)
        XCTAssertLessThan(try weight(LooreFontFace.serifName(.light)), try weight(LooreFontFace.serifName(.semibold)))
    }

    func testDynamicColorsFollowTheTheme() {
        let dark = LooreColor.bgDeepUI.resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark))
        let light = LooreColor.bgDeepUI.resolvedColor(with: UITraitCollection(userInterfaceStyle: .light))
        XCTAssertEqual(dark, UIColor(hex: 0x0E0D0B))
        XCTAssertEqual(light, UIColor(hex: 0xF5EFE4))
        let accentDark = LooreColor.accentUI.resolvedColor(with: UITraitCollection(userInterfaceStyle: .dark))
        XCTAssertEqual(accentDark, UIColor(hex: 0xC4956A))
    }

    func testSVGPathParser() {
        let box = SVGPath.parse("M4 21v-7M4 10V3").boundingRect
        XCTAssertEqual(box, CGRect(x: 4, y: 3, width: 0, height: 18))
        let tri = SVGPath.parse("M0 0 L10 0 L10 10 Z").boundingRect
        XCTAssertEqual(tri, CGRect(x: 0, y: 0, width: 10, height: 10))
        // Relative moveto followed by implicit relative linetos.
        let rel = SVGPath.parse("m1 1 2 0 0 2z").boundingRect
        XCTAssertEqual(rel, CGRect(x: 1, y: 1, width: 2, height: 2))
        // Numbers packed with signs and dots, H/V, curves.
        let packed = SVGPath.parse("M18.244 2.25h3.308l-7.227 8.26-1.5.5C1 1 2 2 3 3").boundingRect
        XCTAssertGreaterThan(packed.width, 0)
        XCTAssertTrue(SVGPath.parse("").isEmpty)
    }

    @MainActor
    func testThemeManager() {
        let defaults = UserDefaults(suiteName: "loore.tests.theme")!
        defaults.removePersistentDomain(forName: "loore.tests.theme")
        let theme = ThemeManager(defaults: defaults)
        XCTAssertNil(theme.preferredColorScheme, "follows the system until the user chooses")
        XCTAssertFalse(theme.isLight(system: .dark))
        theme.toggle(system: .dark)
        XCTAssertEqual(theme.preferredColorScheme, .light)
        XCTAssertEqual(defaults.string(forKey: DefaultsKey.theme), "light")
        theme.toggle(system: .dark)
        XCTAssertEqual(theme.preferredColorScheme, .dark)
        XCTAssertEqual(ThemeManager(defaults: defaults).preferredColorScheme, .dark, "persisted")
        XCTAssertEqual(ThemeManager(defaults: defaults, forced: "light").preferredColorScheme, .light)
    }
}

final class TermsTextTests: XCTestCase {
    func testStructure() {
        let sections = TermsText.blocks.compactMap { block -> String? in
            if case .sectionTitle(let t) = block { return t }
            return nil
        }
        XCTAssertEqual(sections.count, 10)
        XCTAssertEqual(sections.first, "1. What Loore Is")
        XCTAssertEqual(sections.last, "10. Contact")
        XCTAssertEqual(TermsText.version, "2.0")
        XCTAssertTrue(TermsText.blocks.contains(.footnote("*Terms Version: 2.0 — Last updated: February 9, 2026*")))
    }

    func testInlineMarkdownParses() {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        for block in TermsText.blocks {
            let texts: [String]
            switch block {
            case .paragraph(let t), .footnote(let t), .sectionTitle(let t), .subheading(let t), .title(let t): texts = [t]
            case .bullets(let items), .plainList(let items), .quoteBox(let items): texts = items
            case .summaryBox(let title, let items): texts = [title] + items
            case .table(let header, let rows): texts = header + rows.flatMap { $0 }
            case .rule: texts = []
            }
            for text in texts {
                let parsed = try? AttributedString(markdown: text, options: options)
                XCTAssertNotNil(parsed, text)
                XCTAssertFalse(String(parsed.map { String($0.characters) } ?? "").contains("**"), "unbalanced bold in: \(text)")
            }
        }
    }
}
