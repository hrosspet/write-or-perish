import SwiftUI

/// The blocking Terms screen (web `TermsModal`): shown over everything while the
/// signed-in user's accepted terms are out of date. No close, no swipe-away.
/// "I Agree" → `POST /api/terms/accept`.
struct TermsView: View {
    @Environment(AppState.self) private var app
    @State private var accepting = false
    @State private var error: String?

    var body: some View {
        ZStack {
            Rectangle()
                .fill(.ultraThinMaterial)
                .overlay(LooreColor.dialogBackdrop)
                .ignoresSafeArea()
            // Like the web modal: the card stays put and its text scrolls inside it.
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(TermsText.blocks.enumerated()), id: \.offset) { _, block in
                        TermsBlockView(block: block)
                    }
                    if let error {
                        Text(error)
                            .font(LooreFont.body)
                            .foregroundStyle(LooreColor.accent)
                            .padding(.top, 16)
                            .accessibilityIdentifier("terms.error")
                    }
                    Button(accepting ? "Processing..." : "I Agree", action: accept)
                        .buttonStyle(LooreButtonStyle(kind: .primary, font: LooreFont.sans(16, .light)))
                        .disabled(accepting)
                        .padding(.top, 20)
                        .accessibilityIdentifier("terms.agree")
                }
                .padding(LooreSpacing.dialog)
            }
            .scrollBounceBehavior(.basedOnSize)
            .background(LooreColor.bgCard)
            .clipShape(RoundedRectangle(cornerRadius: LooreRadius.large))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.large).strokeBorder(LooreColor.border))
            .frame(maxWidth: 800)
            .padding(.horizontal, LooreSpacing.sm)
            .padding(.vertical, LooreSpacing.md)
            .tint(LooreColor.accent)
        }
        .accessibilityAddTraits(.isModal)
    }

    private func accept() {
        guard !accepting else { return }
        accepting = true
        error = nil
        Task {
            do {
                try await app.acceptTerms()
            } catch {
                self.error = "Error accepting terms. Please try again."
            }
            accepting = false
        }
    }
}

private struct TermsBlockView: View {
    let block: TermsBlock

    var body: some View {
        switch block {
        case .title(let text):
            Text(text)
                .font(LooreFont.serif(25.6, .light, relativeTo: .title))
                .foregroundStyle(LooreColor.textPrimary)
                .padding(.bottom, 12)
                .accessibilityAddTraits(.isHeader)
        case .summaryBox(let title, let items):
            VStack(alignment: .leading, spacing: 8) {
                Text(title)
                    .font(LooreFont.sans(17.6, .bold))
                    .foregroundStyle(LooreColor.textSecondary)
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("\(index + 1).")
                            .font(LooreFont.body)
                            .foregroundStyle(LooreColor.textSecondary)
                        TermsInline(text: item)
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
            .padding(.top, 10)
        case .quoteBox(let paragraphs):
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(paragraphs.enumerated()), id: \.offset) { _, p in
                    TermsInline(text: p)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(LooreColor.bgDeep, in: RoundedRectangle(cornerRadius: LooreRadius.small))
            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
            .padding(.top, 10)
            .padding(.bottom, 8)
        case .sectionTitle(let text):
            Text(text)
                .font(LooreFont.sans(15, .bold))
                .foregroundStyle(LooreColor.textSecondary)
                .padding(.top, 24)
                .padding(.bottom, 8)
                .accessibilityAddTraits(.isHeader)
        case .subheading(let text):
            Text(text)
                .font(LooreFont.sans(15, .regular))
                .foregroundStyle(LooreColor.textSecondary)
                .padding(.top, 16)
                .padding(.bottom, 4)
        case .paragraph(let text):
            TermsInline(text: text)
                .padding(.bottom, 8)
        case .bullets(let items):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text("•").font(LooreFont.body).foregroundStyle(LooreColor.textSecondary)
                        TermsInline(text: item)
                    }
                }
            }
            .padding(.leading, 4)
            .padding(.bottom, 10)
        case .plainList(let items):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    TermsInline(text: item)
                }
            }
            .padding(.leading, 12)
            .padding(.bottom, 10)
        case .table(let header, let rows):
            VStack(alignment: .leading, spacing: 0) {
                TermsRow(cells: header, isHeader: true)
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    TermsRow(cells: row, isHeader: false, isLast: index == rows.count - 1)
                }
            }
            .padding(.vertical, 8)
        case .rule:
            HairlineDivider().padding(.vertical, 24)
        case .footnote(let text):
            TermsInline(text: text, font: LooreFont.sans(13.6, .light))
                .padding(.bottom, 12)
        }
    }
}

private struct TermsRow: View {
    let cells: [String]
    let isHeader: Bool
    var isLast = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
                TermsInline(text: cell, font: isHeader ? LooreFont.sans(14.7, .regular) : LooreFont.body)
                    .frame(maxWidth: index == 0 ? 140 : .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 8)
        .overlay(alignment: .bottom) {
            if !isLast { HairlineDivider() }
        }
    }
}

/// A terms line: inline Markdown with the web's emphasis (bold in `text-primary`).
private struct TermsInline: View {
    let text: String
    var font: Font = LooreFont.body

    var body: some View {
        Text(Self.attributed(text))
            .font(font)
            .foregroundStyle(LooreColor.textSecondary)
            .lineSpacing(4)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    static func attributed(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        var result = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        for run in result.runs {
            if let intent = run.inlinePresentationIntent, intent.contains(.stronglyEmphasized) {
                // The web's <strong> on a 300 body is weight 400 ("bolder"), in text-primary.
                // Drop the intent so SwiftUI does not embolden the font a second time.
                result[run.range].inlinePresentationIntent = intent.subtracting(.stronglyEmphasized)
                result[run.range].foregroundColor = LooreColor.textPrimary
                result[run.range].font = LooreFont.sans(14.7, .regular)
            }
        }
        return result
    }
}
