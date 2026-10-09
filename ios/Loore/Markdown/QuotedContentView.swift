import SwiftUI

/// Resolved quote data for a node (`GET /api/nodes/<id>/resolve-quotes`).
/// `loaded == false` while the lookup runs: quote markers then show a quiet
/// placeholder instead of the web's literal `{quote:12}` text.
struct QuoteData {
    var loaded = false
    var quotes: [Int: ResolvedQuotes.QuotedNode?] = [:]
    var external: [Int: ResolvedQuotes.QuotedExternal?] = [:]

    static let none = QuoteData(loaded: true)
}

/// Node content with its custom syntax (web `QuotedContent`, map D §3.1):
/// guidance markers substituted, then quote and artifact markers split out,
/// each text piece rendered as its own markdown document.
struct QuotedContentView: View {
    let content: String
    var quotes: QuoteData = .none
    var contextArtifacts: ContextArtifacts?
    var nodeId: Int?
    var style: MarkdownStyle = .focal
    var checklist: ChecklistActions?
    var onExternalReadChange: ((Int, Date?) -> Void)?
    var onExternalFeedbackChange: ((Int, String?) -> Void)?

    @Environment(AppState.self) private var app

    var body: some View {
        let text = ContentSegmenter.applyGuidance(content, shareGuidance: contextArtifacts?.shareGuidance,
                                                  externalGuidance: contextArtifacts?.externalContentGuidance)
        let segments = ContentSegmenter.segments(text)
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(segments.enumerated()), id: \.offset) { _, segment in
                switch segment {
                case .text(let piece):
                    MarkdownView(markdown: piece, style: style, checklist: checklist)
                case .quote(let id):
                    InlineQuoteBubble(quote: quotes.quotes[id] ?? nil, loaded: quotes.loaded) { quoteId in
                        NodePrefetch.shared.openThread(quoteId, app: app)  // opens once the node is in
                    }
                case .externalQuote(let id):
                    ExternalQuoteBubble(quote: quotes.external[id] ?? nil, loaded: quotes.loaded, nodeId: nodeId,
                                        onReadChange: onExternalReadChange,
                                        onFeedbackChange: onExternalFeedbackChange)
                case .artifact(let kind):
                    InlineArtifactSection(kind: kind, artifacts: contextArtifacts)
                }
            }
        }
    }
}

/// Muted chip for unavailable quotes and artifacts.
struct UnavailableChip: View {
    let text: String

    var body: some View {
        Text(text)
            .font(LooreFont.sansOblique(14.4, .regular))
            .foregroundStyle(LooreColor.textMuted)
            .padding(.vertical, 4)
            .padding(.horizontal, 8)
            .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 4))
            .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(LooreColor.borderHover))
            .padding(.vertical, 4)
    }
}

/// `{quote:N}`: a quoted node (web `InlineQuoteBubble`).
struct InlineQuoteBubble: View {
    let quote: ResolvedQuotes.QuotedNode?
    var loaded = true
    let onOpen: (Int) -> Void

    var body: some View {
        if !loaded {
            UnavailableChip(text: "Loading quote…")
        } else if let quote, quote.deleted {
            UnavailableChip(text: "[Quoted node deleted]")
        } else if let quote {
            let text = quote.content ?? ""
            let truncated = text.jsLength > 250 ? text.jsPrefix(250) + "..." : text
            VStack(alignment: .leading, spacing: 0) {
                Text("Quoted from @\(quote.username ?? "")")
                    .font(LooreFont.sansOblique(13.6, .regular))
                    .foregroundStyle(LooreColor.textMuted)
                    .padding(.bottom, 6)
                MarkdownView(markdown: truncated, style: .quote)
                    .padding(.bottom, 8)
                NodeFooterView(username: quote.username ?? "", createdAt: quote.createdAt, childCount: 0)
                    .padding(.top, -12)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
            .overlay(alignment: .leading) {
                UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 6)
                    .fill(LooreColor.accent).frame(width: 3)
            }
            .contentShape(Rectangle())
            .onTapGesture { onOpen(quote.id) }
            .padding(.vertical, 10)
            .accessibilityAddTraits(.isButton)
        } else {
            UnavailableChip(text: "[Quoted node inaccessible]")
        }
    }
}

/// `{quote_ext:N}`: a saved reference quoted in a reply, the tweet card of a
/// gleaning (web `ExternalQuoteBubble`, #435, Peter 2026-10-09). The byline is
/// the author's display name when the archive has one, the @handle and the date
/// (no avatar, no "Saved from" line); the text is cut at 500 characters with "…";
/// then the actions: "Open on X", the ⊕/⊖ verdict and "Mark as read". Tapping
/// the card itself opens the reference's page in Loore (full text, listen
/// button, verdict); the actions never also open it. Only the reference's owner
/// can open that page, so for anyone else the card is not tappable.
///
/// Opening the post is reading it: the owner's "Open on X" marks it read. A
/// verdict marks it read server-side. Opening the reference page in Loore does
/// not (a skim is not a read, #352).
struct ExternalQuoteBubble: View {
    let quote: ResolvedQuotes.QuotedExternal?
    var loaded = true
    var nodeId: Int?
    var onReadChange: ((Int, Date?) -> Void)?
    var onFeedbackChange: ((Int, String?) -> Void)?

    @Environment(AppState.self) private var app
    @State private var readAt: Date?
    @State private var marking = false

    /// Sources whose reference is a tweet: the card names its author.
    static let tweetSources: Set<String> = ["community_archive", "read_pick", "twitter_bookmark", "twitter_like"]
    /// The preview is cut here, with "…"; the reference page has the rest.
    static let previewChars = 500

    static func previewText(_ text: String) -> String {
        text.jsLength > previewChars ? text.jsPrefix(previewChars) + "…" : text
    }

    /// The byline's two parts: the bold name (a tweet's display name, a page's
    /// title) and the muted handle (a tweet's @handle, a page's byline or site).
    static func byline(_ quote: ResolvedQuotes.QuotedExternal) -> (name: String?, handle: String?) {
        func nonEmpty(_ s: String?) -> String? { s.flatMap { $0.isEmpty ? nil : $0 } }
        if tweetSources.contains(quote.source ?? "") {
            return (nonEmpty(quote.authorName), "@\(nonEmpty(quote.authorHandle) ?? "unknown")")
        }
        return (nonEmpty(quote.title), nonEmpty(quote.authorHandle) ?? quote.url.flatMap(ReferenceFooter.host))
    }

    var body: some View {
        if !loaded {
            UnavailableChip(text: "Loading quote…")
        } else if let quote {
            card(quote)
                .onAppear { readAt = quote.readAt }
                .onChange(of: quote.readAt) { _, value in readAt = value }
        } else {
            UnavailableChip(text: "[Quoted reference inaccessible]")
        }
    }

    private func card(_ quote: ResolvedQuotes.QuotedExternal) -> some View {
        let mine = app.user?.id != nil && quote.userId == app.user?.id
        let isTweet = Self.tweetSources.contains(quote.source ?? "")
        let byline = Self.byline(quote)
        return VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 6) {
                FlowLayout(spacing: 8, lineSpacing: 2) {
                    if let name = byline.name {
                        Text(name)
                            .font(LooreFont.sans(13.8, .medium))
                            .foregroundStyle(LooreColor.textPrimary)
                    }
                    if let handle = byline.handle {
                        Text(handle)
                            .font(LooreFont.sans(13.8, .light))
                            .foregroundStyle(LooreColor.textMuted)
                    }
                    if let posted = quote.postedAt {
                        Text("· \(LooreDateFormat.date(posted, relative: false))")
                            .font(LooreFont.sans(13.8, .light))
                            .foregroundStyle(LooreColor.textMuted)
                    }
                }
                MarkdownView(markdown: Self.previewText(quote.content), style: .quote)
                    .padding(.bottom, 4)
            }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(mine ? .isLink : [])
            .accessibilityHint(mine ? "Opens this reference in Loore" : "")
            .accessibilityAction { if mine { openReference(quote) } }
            FlowLayout(spacing: 16, lineSpacing: 4) {
                if quote.url != nil {
                    Button { openOriginal(quote, mine: mine) } label: {
                        Text(isTweet ? "Open on X ↗" : "Open original ↗")
                            .font(LooreFont.sans(12.8, .regular))
                            .foregroundStyle(LooreColor.textMuted)
                            .padding(.vertical, 10)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(isTweet ? "Opens the tweet on X" : "Opens the original")
                    .accessibilityIdentifier("quote.openOriginal")
                }
                if mine {
                    ReferenceFeedbackControl(itemId: quote.id, feedback: quote.feedback, nodeId: nodeId,
                                             shared: quote.feedbackShared == true) { verdict, answeredReadAt in
                        onFeedbackChange?(quote.id, verdict)
                        if let answeredReadAt, readAt == nil {
                            readAt = answeredReadAt
                            onReadChange?(quote.id, answeredReadAt)
                        }
                    }
                    Button(readAt != nil ? "Mark as unread" : "Mark as read") { setRead(quote, !(readAt != nil)) }
                        .buttonStyle(.plain)
                        .font(LooreFont.sans(12.8, .regular))
                        .foregroundStyle(readAt != nil ? LooreColor.textMuted : LooreColor.accentDim)
                        .disabled(marking)
                    if let rated = quote.ratedBefore?.objectValue, quote.feedback == nil,
                       let verdict = rated["feedback"]?.stringValue {
                        let at = rated["at"]?.stringValue.flatMap(LooreDate.parse)
                        Text("You rated this \(verdict) on \(LooreDateFormat.date(at, relative: false))")
                            .font(LooreFont.sansOblique(12.8, .light))
                            .foregroundStyle(LooreColor.textMuted)
                    }
                }
            }
        }
        .padding(.vertical, 12)
        .padding(.horizontal, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(LooreColor.bgSurface, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(LooreColor.border))
        .contentShape(Rectangle())
        // The card body opens the reference page; its buttons keep their own taps.
        .onTapGesture { if mine { openReference(quote) } }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("quote.card")
        .padding(.vertical, 10)
    }

    /// The reference's page in Loore, pushed over the thread (Back returns to it).
    private func openReference(_ quote: ResolvedQuotes.QuotedExternal) {
        app.router.push(.reference(id: quote.id))
    }

    private func openOriginal(_ quote: ResolvedQuotes.QuotedExternal, mine: Bool) {
        guard let link = quote.url, let url = URL(string: link) else { return }
        app.open(.external(url))
        if mine && !marking { setRead(quote, true, via: "open") }
    }

    private func setRead(_ quote: ResolvedQuotes.QuotedExternal, _ want: Bool, via: String? = nil) {
        marking = true
        var body: [String: JSONValue] = [:]
        if let nodeId { body["node_id"] = .int(nodeId) }
        if let via { body["via"] = .string(via) }
        Task {
            defer { marking = false }
            do {
                struct Answer: Decodable { var read_at: Date? }
                let path = APIPath.externalItemRead(quote.id)
                let answer: Answer = want
                    ? try await app.api.post(path, json: .object(body))
                    : try await app.api.send(.json(.delete, path, .object(body)))
                readAt = answer.read_at
                onReadChange?(quote.id, answer.read_at)
            } catch {
                app.toasts.show("Could not update the read mark.", duration: 4)
            }
        }
    }
}

/// Good / bad verdict glyphs for a saved reference (web `ReferenceFeedback`;
/// shared with the reference page in M4). Choosing the selected verdict clears it.
struct ReferenceFeedbackControl: View {
    let itemId: Int
    let feedback: String?
    var nodeId: Int?
    var shared = false
    var size: CGFloat = 16
    /// (new verdict, read_at from the answer)
    var onChange: ((String?, Date?) -> Void)?

    @Environment(AppState.self) private var app
    @State private var value: String?
    @State private var sharedShown = false
    @State private var saving = false

    var body: some View {
        HStack(spacing: 0) {
            glyph("good", label: "Good quote", symbol: "plus.circle")
            glyph("bad", label: "Bad quote", symbol: "minus.circle")
        }
        .onAppear { value = feedback; sharedShown = shared }
        .onChange(of: feedback) { _, new in value = new; sharedShown = shared }
    }

    private func glyph(_ kind: String, label: String, symbol: String) -> some View {
        let selected = value == kind
        return Button { choose(kind) } label: {
            Image(systemName: selected ? symbol + ".fill" : symbol)
                .font(.system(size: size, weight: .light))
                .foregroundStyle(selected ? LooreColor.accent : LooreColor.textMuted)
                .frame(width: 40, height: 40)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(saving)
        .accessibilityLabel(sharedShown && selected ? "\(label) (your rating from another reply)" : label)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func choose(_ kind: String) {
        let target: String? = value == kind ? nil : kind
        saving = true
        var body: [String: JSONValue] = ["feedback": target.map(JSONValue.string) ?? .null]
        if let nodeId { body["node_id"] = .int(nodeId) }
        Task {
            defer { saving = false }
            do {
                struct Answer: Decodable { var feedback: String?; var read_at: Date? }
                let answer: Answer = try await app.api.post(APIPath.externalItemFeedback(itemId), json: .object(body))
                value = answer.feedback
                sharedShown = false
                onChange?(answer.feedback, answer.read_at)
            } catch {
                app.toasts.show("Could not save your feedback.", duration: 4)
            }
        }
    }
}

/// `{user_<kind>}` in a system prompt: a collapsed card that expands to the
/// artifact text the model received (web `InlineArtifactSection`).
struct InlineArtifactSection: View {
    let kind: ArtifactPlaceholder
    let artifacts: ContextArtifacts?
    @State private var expanded = false

    var body: some View {
        if kind == .recentRaw {
            if let raw = artifacts?.recentRaw {
                container {
                    header(chevron: "\u{25A0}", text: "\(kind.label) — \(rangeText(raw))")
                }
            } else {
                UnavailableChip(text: "[\(kind.label) not available]")
            }
        } else if let artifact = artifact {
            container {
                Button {
                    withAnimation(LooreMotion.quick) { expanded.toggle() }
                } label: {
                    header(chevron: expanded ? "\u{25BC}" : "\u{25B6}", text: headerLabel(artifact))
                }
                .buttonStyle(.plain)
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                if expanded {
                    VStack(spacing: 0) {
                        HairlineDivider()
                        MarkdownView(markdown: artifact.content,
                                     style: MarkdownStyle(fontSize: 14.4, weight: .regular, lineHeight: 1.6,
                                                          paragraphMargin: 0.3))
                            .padding(.horizontal, 12)
                            .padding(.top, 10)
                            .padding(.bottom, 10)
                    }
                }
            }
        } else {
            UnavailableChip(text: "[\(kind.label) not available]")
        }
    }

    private var artifact: ContextArtifacts.VersionedText? {
        guard let artifacts else { return nil }
        switch kind {
        case .profile: return artifacts.profile
        case .todo: return artifacts.todo
        case .recent: return artifacts.recent
        case .recentRaw: return nil
        default: return artifacts.artifacts[kind.rawValue]
        }
    }

    private func headerLabel(_ artifact: ContextArtifacts.VersionedText) -> String {
        switch kind {
        case .recent: return kind.label
        default: return artifact.versionNumber.map { "\(kind.label) v\($0)" } ?? kind.label
        }
    }

    private func rangeText(_ raw: ContextArtifacts.RecentRaw) -> String {
        guard let start = raw.coversStart, let end = raw.coversEnd else { return "Date range unavailable" }
        let tokens = Self.formatTokens(raw.sourceTokens).map { " (\($0))" } ?? ""
        return "Covers \(start) to \(end)\(tokens)"
    }

    /// "1.2M tokens" / "10K tokens" / "512 tokens".
    static func formatTokens(_ n: Int?) -> String? {
        guard let n, n > 0 else { return nil }
        func short(_ value: Double) -> String {
            let s = String(format: "%.1f", value)
            return s.hasSuffix(".0") ? String(s.dropLast(2)) : s
        }
        if n >= 1_000_000 { return "\(short(Double(n) / 1_000_000))M tokens" }
        if n >= 1_000 { return "\(short(Double(n) / 1_000))K tokens" }
        return "\(n) tokens"
    }

    private func header(chevron: String, text: String) -> some View {
        HStack(spacing: 6) {
            Text(chevron).font(LooreFont.sans(11.2, .regular)).foregroundStyle(LooreColor.textMuted)
                .accessibilityHidden(true)
            Text(text).font(LooreFont.sans(13.6, .medium)).foregroundStyle(LooreColor.textSecondary)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .contentShape(Rectangle())
    }

    private func container<C: View>(@ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 0, content: content)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgCard, in: RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(LooreColor.border))
            .overlay(alignment: .leading) {
                UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 6).fill(LooreColor.accent).frame(width: 3)
            }
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .padding(.vertical, 10)
    }
}
