import SwiftUI
import WebKit

/// A small web view for the YouTube player on the reference page (web
/// `YouTubeEmbed`). It loads an HTML string and opens tapped links outside.
/// A tweet's page shows the stored text, not X's widget (Peter, 2026-10-13).
struct EmbedWebView: UIViewRepresentable {
    let html: String
    /// The page's origin (YouTube needs a referrer).
    let baseURL: URL?
    var onLink: (URL) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.isScrollEnabled = false
        webView.loadHTMLString(html, baseURL: baseURL)
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.parent = self
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: EmbedWebView

        init(parent: EmbedWebView) { self.parent = parent }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                parent.onLink(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        /// `target=_blank` links ("Watch on YouTube") come here.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { parent.onLink(url) }
            return nil
        }
    }
}

/// The YouTube nocookie player (16:9, rounded, black), as the web's `YouTubeEmbed`.
struct YouTubeEmbedView: View {
    let videoId: String
    var start: Int?
    @Environment(AppState.self) private var app

    var body: some View {
        EmbedWebView(html: html, baseURL: app.environment.frontendOrigin, onLink: { app.open(.external($0)) })
            .aspectRatio(16 / 9, contentMode: .fit)
            .background(Color.black)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .frame(maxWidth: 860)
            .accessibilityLabel("YouTube video")
    }

    private var html: String {
        var src = "https://www.youtube-nocookie.com/embed/\(videoId)?rel=0&playsinline=1"
        if let start, start > 0 { src += "&start=\(start)" }
        return """
        <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <meta name="referrer" content="strict-origin-when-cross-origin">
        <style>html,body{margin:0;height:100%;background:#000}iframe{position:absolute;inset:0;width:100%;height:100%;border:0}</style>
        </head><body><iframe src="\(src)" title="YouTube video"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe></body></html>
        """
    }
}
