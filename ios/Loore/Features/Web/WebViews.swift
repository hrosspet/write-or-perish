import SwiftUI
import SafariServices
import WebKit

/// Public web pages (About pages, `/@user`, external links) in an in-app Safari
/// view (design doc §6). Safari views do not share the app's cookies, which is
/// fine: these pages need none.
struct SafariView: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let configuration = SFSafariViewController.Configuration()
        configuration.entersReaderIfAvailable = false
        let controller = SFSafariViewController(url: url, configuration: configuration)
        controller.preferredControlTintColor = LooreColor.accentUI
        controller.preferredBarTintColor = LooreColor.bgSurfaceUI
        controller.dismissButtonStyle = .close
        return controller
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}

/// A web page that needs the signed-in session (Admin; later Connect X and the
/// X bookmarks connect): a `WKWebView` whose cookie store is seeded with the
/// app's cookies before loading (design doc §6).
struct AuthenticatedWebView: UIViewRepresentable {
    let url: URL
    let cookies: [HTTPCookie]
    /// Decide on each navigation (e.g. detect the final `?x_login=` URL). Return
    /// `.cancel` to stop it. Default: allow everything.
    var decide: ((URL) -> WKNavigationActionPolicy)?
    /// When set, the page stays on these hosts: a web link to any other host is
    /// not loaded here but handed to `onLeave` (`leaves`). Frames inside a page
    /// are not affected.
    var stayOnHosts: Set<String>?
    var onLeave: (URL) -> Void = { _ in }
    @Binding var isLoading: Bool

    func makeCoordinator() -> Coordinator {
        Coordinator(decide: decide, stayOnHosts: stayOnHosts, onLeave: onLeave, isLoading: $isLoading)
    }

    /// Whether a navigation takes the page (not a frame inside it) to a web
    /// address outside `hosts`. `inFrame`: the navigation is for a frame inside
    /// the page. Other schemes (about:, blob:, data:) are not web addresses.
    static func leaves(_ url: URL, inFrame: Bool, hosts: Set<String>) -> Bool {
        guard !inFrame, let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
            return false
        }
        guard let host = url.host?.lowercased() else { return true }
        return !hosts.contains(host)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isOpaque = false
        webView.backgroundColor = LooreColor.bgDeepUI
        let store = configuration.websiteDataStore.httpCookieStore
        let group = DispatchGroup()
        for cookie in cookies {
            group.enter()
            store.setCookie(cookie) { group.leave() }
        }
        let request = URLRequest(url: url)
        group.notify(queue: .main) {
            webView.load(request)
        }
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate {
        let decide: ((URL) -> WKNavigationActionPolicy)?
        let stayOnHosts: Set<String>?
        let onLeave: (URL) -> Void
        @Binding var isLoading: Bool

        init(decide: ((URL) -> WKNavigationActionPolicy)?, stayOnHosts: Set<String>?,
             onLeave: @escaping (URL) -> Void, isLoading: Binding<Bool>) {
            self.decide = decide
            self.stayOnHosts = stayOnHosts
            self.onLeave = onLeave
            _isLoading = isLoading
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            // No target frame: a link that asks for a new window.
            if let url = navigationAction.request.url, let stayOnHosts,
               AuthenticatedWebView.leaves(url, inFrame: navigationAction.targetFrame?.isMainFrame == false,
                                           hosts: stayOnHosts) {
                decisionHandler(.cancel)
                onLeave(url)
                return
            }
            guard let url = navigationAction.request.url, let decide else {
                decisionHandler(.allow)
                return
            }
            decisionHandler(decide(url))
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

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            isLoading = false
        }
    }
}

/// The sheet that hosts a `WebPresentation`.
struct WebPresentationView: View {
    let presentation: WebPresentation
    let cookies: [HTTPCookie]
    /// The signed-in page stays on Loore's own hosts; other sites open in the browser.
    let sessionHosts: Set<String>
    @Environment(\.dismiss) private var dismiss
    @State private var isLoading = true

    var body: some View {
        switch presentation.kind {
        case .safari:
            SafariView(url: presentation.url)
                .ignoresSafeArea()
        case .authenticated:
            NavigationStack {
                AuthenticatedWebView(url: presentation.url, cookies: cookies,
                                     stayOnHosts: sessionHosts,
                                     onLeave: { UIApplication.shared.open($0) },
                                     isLoading: $isLoading)
                    .ignoresSafeArea(edges: .bottom)
                    .navigationTitle(presentation.title ?? "")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { dismiss() }
                        }
                        ToolbarItem(placement: .principal) {
                            HStack(spacing: 8) {
                                Text(presentation.title ?? "")
                                    .font(LooreFont.sans(15, .regular))
                                    .foregroundStyle(LooreColor.textPrimary)
                                if isLoading { ProgressView().controlSize(.small) }
                            }
                        }
                    }
            }
        }
    }
}
