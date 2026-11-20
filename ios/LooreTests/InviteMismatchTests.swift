import XCTest
@testable import Loore

/// A pasted sign-in link that came with an invite made for someone else (#439;
/// Peter, 2026-11-13: "They should know the invitation was for somebody else.
/// They should still have the option to sign up without the invitation"). The
/// server holds the sign-in and redirects to `/invite-mismatch?for=<kind>`; the
/// app says so, naming only the kind of sign-in, and offers both ways on.
final class InviteMismatchTests: XCTestCase {
    func testTheHeldSignInIsNeitherASuccessNorAFailure() {
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "https://loore.org/invite-mismatch?for=x"),
                       .inviteMismatch(kind: "x"))
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "http://localhost:3001/invite-mismatch?for=email"),
                       .inviteMismatch(kind: "email"))
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "/invite-mismatch"),
                       .inviteMismatch(kind: nil))
        // Control: the other answers are as before.
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "https://loore.org/dashboard"), .success)
        XCTAssertEqual(MagicLink.outcome(status: 302, location: "https://loore.org/login?error=link_already_used"),
                       .failure(code: "link_already_used"))
    }

    func testTheNoticeNamesTheKindNeverWhom() {
        XCTAssertEqual(MagicLink.inviteMismatchText(kind: "x"),
                       "This invitation was made for a different email address or X account. It was made for an X account. If it was meant for you, sign in with that one. You can also continue without the invitation.")
        XCTAssertEqual(AuthService.InviteMismatch(kind: "email").text,
                       "This invitation was made for a different email address or X account. It was made for an email address. If it was meant for you, sign in with that one. You can also continue without the invitation.")
        XCTAssertEqual(MagicLink.inviteMismatchText(kind: nil),
                       "This invitation was made for a different email address or X account. If it was meant for you, sign in with that one. You can also continue without the invitation.")
        for kind in ["x", "email", nil] {
            let text = MagicLink.inviteMismatchText(kind: kind)
            XCTAssertFalse(text.contains("@"))
            for word in ["pay", "subscri", "$", "web", "Settings"] {
                XCTAssertFalse(text.contains(word), word)
            }
        }
    }
}
