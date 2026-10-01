import SwiftUI
import WebKit

/// A small web view for third-party embeds on the reference page (web
/// `TweetEmbed`, `YouTubeEmbed`). It loads an HTML string, reports
/// `{status, height}` messages from the page, and opens tapped links outside.
struct EmbedWebView: UIViewRepresentable {
    let html: String
    /// The page's origin (YouTube needs a referrer; widgets.js an https page).
    let baseURL: URL?
    var onMessage: (String, CGFloat?) -> Void = { _, _ in }
    var onLink: (URL) -> Void = { _ in }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.allowsInlineMediaPlayback = true
        configuration.mediaTypesRequiringUserActionForPlayback = .all
        configuration.userContentController.add(context.coordinator, name: "loore")
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

    static func dismantleUIView(_ uiView: WKWebView, coordinator: Coordinator) {
        uiView.configuration.userContentController.removeScriptMessageHandler(forName: "loore")
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        var parent: EmbedWebView

        init(parent: EmbedWebView) { self.parent = parent }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any] else { return }
            let status = body["status"] as? String ?? ""
            let height = (body["height"] as? NSNumber).map { CGFloat(truncating: $0) }
            DispatchQueue.main.async { self.parent.onMessage(status, height) }
        }

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                parent.onLink(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }

        /// `target=_blank` links (the tweet card) come here.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { parent.onLink(url) }
            return nil
        }
    }
}

/// A tweet rendered by X's widgets.js (`createTweet`, dnt, centred, no
/// conversation). Reports "shown" or "unavailable" like the web component.
struct TweetEmbedView: View {
    let tweetId: String
    let dark: Bool
    let onStatus: (String) -> Void
    @Environment(AppState.self) private var app
    @State private var height: CGFloat = 120
    @State private var status = "loading"

    var body: some View {
        EmbedWebView(html: html, baseURL: app.environment.frontendOrigin,
                     onMessage: { newStatus, newHeight in
                         if let newHeight, newHeight > 0 { height = newHeight }
                         if !newStatus.isEmpty, newStatus != status {
                             status = newStatus
                             onStatus(newStatus)
                         }
                     },
                     onLink: { app.open(.external($0)) })
            .frame(height: status == "unavailable" ? 0 : height)
            .opacity(status == "unavailable" ? 0 : 1)
            .task {
                // widgets.js that never answers counts as unavailable (the stored text shows).
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if status == "loading" {
                    status = "unavailable"
                    onStatus("unavailable")
                }
            }
    }

    private var html: String {
        let id = tweetId.filter(\.isNumber)
        return """
        <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <style>html,body{margin:0;padding:0;background:transparent}#t{display:flex;justify-content:center}</style>
        </head><body><div id="t"></div>
        <script>
        function post(s){var h=document.getElementById('t').getBoundingClientRect().height;
          window.webkit.messageHandlers.loore.postMessage({status:s,height:h});}
        var sc=document.createElement('script');sc.src='https://platform.twitter.com/widgets.js';sc.async=true;
        sc.onerror=function(){post('unavailable')};
        sc.onload=function(){ if(!window.twttr||!twttr.ready){post('unavailable');return;}
          twttr.ready(function(t){ t.widgets.createTweet('\(id)',document.getElementById('t'),
            {theme:'\(dark ? "dark" : "light")',dnt:true,align:'center',conversation:'none'})
            .then(function(n){ post(n?'shown':'unavailable');
              if(n){ new ResizeObserver(function(){post('shown')}).observe(document.getElementById('t')); } })
            .catch(function(){post('unavailable')}); }); };
        document.head.appendChild(sc);
        </script></body></html>
        """
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
