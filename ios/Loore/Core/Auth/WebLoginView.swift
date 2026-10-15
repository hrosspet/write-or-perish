import SwiftUI
import WebKit

/// "Sign in with X" inside a `WKWebView` (design doc §4.2, map B §4.1 option B).
///
/// Loads `/auth/login?next=/profile`; the whole OAuth round trip stays in one
/// cookie jar, so the backend's request-token check passes. When the web view
/// is about to open the frontend after login, the navigation is cancelled and
/// the `session` / `remember_token` cookies are handed to the app, with the
/// page the login was sending the browser to.
struct WebLoginSheet: View {
    let environment: AppEnvironment
    let onFinish: (Result<WebLoginResult, AuthFailure>) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var isLoading = true

    var body: some View {
        NavigationStack {
            WebLoginView(environment: environment, isLoading: $isLoading) { result in
                onFinish(result)
                dismiss()
            }
            .ignoresSafeArea(edges: .bottom)
            .navigationTitle("Sign in with X")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .principal) {
                    if isLoading { ProgressView().controlSize(.small) }
                }
            }
        }
    }
}

/// A finished web login: its auth cookies and the frontend page it landed on
/// (`/profile`, or `/account-restore` for an account in its deletion grace period).
struct WebLoginResult {
    var cookies: [HTTPCookie]
    var landing: URL
}

struct WebLoginView: UIViewRepresentable {
    let environment: AppEnvironment
    @Binding var isLoading: Bool
    let onFinish: (Result<WebLoginResult, AuthFailure>) -> Void

    static func startURL(for environment: AppEnvironment) -> URL {
        var components = URLComponents(url: environment.url(path: APIPath.xLogin), resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "next", value: "/profile")]
        return components.url!
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(environment: environment, isLoading: $isLoading, onFinish: onFinish)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        // A fresh, in-memory jar per attempt: nothing of X's login lingers on the device.
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.load(URLRequest(url: Self.startURL(for: environment)))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate {
        let environment: AppEnvironment
        @Binding var isLoading: Bool
        let onFinish: (Result<WebLoginResult, AuthFailure>) -> Void
        private var finished = false

        init(environment: AppEnvironment, isLoading: Binding<Bool>,
             onFinish: @escaping (Result<WebLoginResult, AuthFailure>) -> Void) {
            self.environment = environment
            _isLoading = isLoading
            self.onFinish = onFinish
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url,
                  WebLoginRouting.isFrontendLanding(url, environment: environment) else {
                decisionHandler(.allow)
                return
            }
            decisionHandler(.cancel)
            guard !finished else { return }
            finished = true
            if let code = WebLoginRouting.loginErrorCode(url) {
                onFinish(.failure(AuthFailure(message: MagicLink.message(for: code))))
                return
            }
            webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                let auth = CookieVault.authCookies(from: cookies, host: self.environment.host)
                if auth.isEmpty {
                    self.onFinish(.failure(AuthFailure(message: "Sign in with X did not finish. Please try again.")))
                } else {
                    self.onFinish(.success(WebLoginResult(cookies: auth, landing: url)))
                }
            }
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            isLoading = true
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            isLoading = false
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            isLoading = false
        }
    }
}

/// Decisions about where a web login has landed (pure, unit-tested).
enum WebLoginRouting {
    /// True when `url` is a frontend page (not the backend's `/auth` or `/api`):
    /// every login, success or failure, ends with a redirect to `FRONTEND_URL…`.
    static func isFrontendLanding(_ url: URL, environment: AppEnvironment) -> Bool {
        let frontend = environment.frontendOrigin
        guard url.scheme == frontend.scheme, url.host == frontend.host,
              (url.port ?? defaultPort(url.scheme)) == (frontend.port ?? defaultPort(frontend.scheme)) else {
            return false
        }
        let path = url.path
        return !(path.hasPrefix("/auth/") || path == "/auth" || path.hasPrefix("/api/"))
    }

    /// The in-app landing of a finished X login: the restore question for an
    /// account in its deletion grace period (#269), else nil (the app stays
    /// on Reflect, as before; the web's `/profile` is not followed).
    static func appLanding(_ url: URL) -> String? {
        AccountDeletion.isRestoreLanding(url.absoluteString) ? AccountDeletion.restorePath : nil
    }

    /// `/login?error=<code>` → code.
    static func loginErrorCode(_ url: URL) -> String? {
        guard url.path == "/login" else { return nil }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first(where: { $0.name == "error" })?.value
    }

    private static func defaultPort(_ scheme: String?) -> Int {
        scheme == "https" ? 443 : 80
    }
}
