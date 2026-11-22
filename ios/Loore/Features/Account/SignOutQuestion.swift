import SwiftUI

/// The question before a sign-out deletes recorded audio that has not reached
/// Loore (#763). A sign-out button calls `AppState.requestSignOut(question:)`,
/// which signs out at once when this phone holds none of the account's, and
/// the screen carries `.signOutQuestion($question)`. "Show the choices": stay
/// signed in and the audio uploads, or sign out and it is deleted.
struct SignOutQuestion: Equatable {
    var title: String
    var message: String
    var confirm: String
    var cancel: String

    static let signOut = SignOutQuestion(
        title: "Sign out of Loore?",
        message: "Some audio you recorded hasn't reached Loore yet. Signing out deletes it from this phone. If you stay signed in, it uploads when you're back online.",
        confirm: "Sign out and delete it",
        cancel: "Stay signed in")

    /// The More screen's Logout, in its words.
    static let logOut = SignOutQuestion(
        title: "Log out of Loore?",
        message: "Some audio you recorded hasn't reached Loore yet. Logging out deletes it from this phone. If you stay logged in, it uploads when you're back online.",
        confirm: "Log out and delete it",
        cancel: "Stay logged in")
}

extension View {
    /// The screen's sign-out question (#763), shown when `isPresented` is set
    /// by `AppState.requestSignOut(question:)`.
    func signOutQuestion(_ isPresented: Binding<Bool>, _ words: SignOutQuestion = .signOut) -> some View {
        modifier(SignOutQuestionModifier(isPresented: isPresented, words: words))
    }
}

private struct SignOutQuestionModifier: ViewModifier {
    @Environment(AppState.self) private var app
    @Binding var isPresented: Bool
    let words: SignOutQuestion

    func body(content: Content) -> some View {
        content.confirmationDialog(words.title, isPresented: $isPresented, titleVisibility: .visible) {
            Button(words.confirm, role: .destructive) { Task { await app.signOut() } }
                .accessibilityIdentifier("signOutQuestion.confirm")
            Button(words.cancel, role: .cancel) {}
        } message: {
            Text(words.message)
        }
    }
}
