import SwiftUI

/// Where a version history comes from (web `VersionHistoryDrawer` props).
struct VersionHistorySource {
    /// "Todo History", "Profile History", "{title} History".
    var title: String
    var loadVersions: () async throws -> [VersionSummary]
    var loadContent: (VersionSummary) async throws -> String
    /// nil hides "Revert to this version".
    var revert: ((VersionSummary) async throws -> Void)?
}

/// The version drawer as a sheet (map E §1.5): a list of versions, newest
/// first, and a detail page per version with a line diff against the
/// next-older version (Changes / Full text) and "Revert to this version".
struct VersionHistorySheet: View {
    let source: VersionHistorySource
    /// Called after a successful revert (the sheet then closes).
    var onReverted: () -> Void = {}

    @Environment(\.dismiss) private var dismiss
    @State private var versions: [VersionSummary] = []
    @State private var loading = true
    @State private var failed = false

    var body: some View {
        NavigationStack {
            Group {
                if loading && versions.isEmpty {
                    LoadingLine()
                } else if failed && versions.isEmpty {
                    ErrorLine(text: "Couldn't load the history.") { Task { await load() } }
                } else {
                    List {
                        ForEach(Array(versions.enumerated()), id: \.element.id) { index, version in
                            NavigationLink(value: version) {
                                VersionRow(version: version, isCurrent: index == 0)
                            }
                            .listRowBackground(LooreColor.bgSurface)
                            .listRowSeparatorTint(LooreColor.border)
                            .accessibilityIdentifier("history.v\(version.versionNumber)")
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .background(LooreColor.bgSurface)
            .navigationTitle(source.title)
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: VersionSummary.self) { version in
                VersionDetailView(source: source, versions: versions, version: version) {
                    onReverted()
                    dismiss()
                }
            }
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text(source.title)
                        .font(LooreFont.serif(17.6, .regular, relativeTo: .headline))
                        .foregroundStyle(LooreColor.textPrimary)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Close") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                }
            }
        }
        .presentationDetents([.large])
        .presentationBackground(LooreColor.bgSurface)
        .task { await load() }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        do {
            versions = try await source.loadVersions()
            failed = false
        } catch {
            failed = true
        }
    }
}

private struct VersionRow: View {
    let version: VersionSummary
    let isCurrent: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                Text("v\(version.versionNumber)")
                    .font(LooreFont.sans(13.6, .regular))
                    .foregroundStyle(LooreColor.textPrimary)
                if isCurrent {
                    Text("current")
                        .font(LooreFont.sans(10.4, .regular))
                        .foregroundStyle(LooreColor.accent)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(LooreColor.accentSubtle, in: RoundedRectangle(cornerRadius: 4))
                }
            }
            Text(VersionHistoryText.secondLine(version))
                .font(LooreFont.meta)
                .foregroundStyle(LooreColor.textMuted)
        }
        .padding(.vertical, 6)
    }
}

enum VersionHistoryText {
    /// `{date, fallback 'File default'} · {label}[ · ~{n} source tokens]`.
    static func secondLine(_ version: VersionSummary) -> String {
        var text = LooreDateFormat.date(version.createdAt, fallback: "File default")
            + " · " + VersionLabels.versionLabel(generatedBy: version.generatedBy,
                                                 generationType: version.generationType)
        if let tokens = version.sourceTokensUsed, tokens > 0 {
            text += " · ~\(jsLocaleNumber(tokens)) source tokens"
        }
        return text
    }
}

/// One version: Changes / Full text against the next-older version.
private struct VersionDetailView: View {
    let source: VersionHistorySource
    let versions: [VersionSummary]
    let version: VersionSummary
    let onReverted: () -> Void

    @Environment(AppState.self) private var app
    @State private var content: String?
    @State private var previous: String?
    @State private var mode: Mode = .diff
    @State private var reverting = false
    /// Computed once when both sides have loaded (an LCS over up to 1500 lines).
    @State private var diff: VersionDiff?

    enum Mode { case diff, full }

    private var index: Int { versions.firstIndex(of: version) ?? 0 }
    private var isOldest: Bool { index == versions.count - 1 }
    private var older: VersionSummary? { index + 1 < versions.count ? versions[index + 1] : nil }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                controls
                if let content {
                    if mode == .diff, let diff {
                        DiffRowsView(diff: diff)
                    } else {
                        Text(content)
                            .font(LooreFont.sans(12.8, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                            .lineSpacing(7.5)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else {
                    Text("Loading...")
                        .font(LooreFont.sans(13.6, .light))
                        .foregroundStyle(LooreColor.textMuted)
                }
            }
            .padding(.horizontal, LooreSpacing.gutter)
            .padding(.vertical, 20)
        }
        .background(LooreColor.bgSurface)
        .navigationTitle("v\(version.versionNumber)")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    @ViewBuilder private var controls: some View {
        let revertable = source.revert != nil && index > 0
        if diff != nil || isOldest || revertable {
            HStack(spacing: 12) {
                if let diff {
                    tab("Changes (\(diff.changedCount))", .diff)
                    tab("Full text", .full)
                }
                if isOldest {
                    Text("Initial version")
                        .font(LooreFont.sans(12, .regular))
                        .tracking(0.5)
                        .foregroundStyle(LooreColor.textMuted)
                }
                Spacer(minLength: 0)
                if revertable {
                    Button(reverting ? "Reverting…" : "Revert to this version") { revert() }
                        .font(LooreFont.sans(11.5, .regular))
                        .foregroundStyle(LooreColor.textMuted)
                        .padding(.vertical, 4)
                        .padding(.horizontal, 10)
                        .overlay(RoundedRectangle(cornerRadius: LooreRadius.control).strokeBorder(LooreColor.border))
                        .buttonStyle(.plain)
                        .disabled(reverting)
                        .accessibilityIdentifier("history.revert")
                }
            }
        }
    }

    private func tab(_ title: String, _ value: Mode) -> some View {
        Button { mode = value } label: {
            Text(title)
                .font(LooreFont.sans(12, .regular))
                .tracking(0.5)
                .foregroundStyle(mode == value ? LooreColor.accent : LooreColor.textMuted)
                .padding(.vertical, 2)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(mode == value ? LooreColor.accent : .clear).frame(height: 1)
                }
                .frame(minHeight: 32)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(mode == value ? .isSelected : [])
    }

    private func load() async {
        guard content == nil else { return }
        async let mine = try? source.loadContent(version)
        async let theirs: String? = older == nil ? nil : (try? await source.loadContent(older!))
        let (a, b) = await (mine, theirs)
        content = a
        previous = b
        if let a, let b, !isOldest {
            let computed = await Task.detached(priority: .userInitiated) { VersionDiff(old: b, new: a) }.value
            diff = computed
            mode = computed.isHeavyRewrite ? .full : .diff
        }
    }

    private func revert() {
        guard let revert = source.revert, !reverting else { return }
        reverting = true
        Task {
            defer { reverting = false }
            do {
                try await revert(version)
                onReverted()
            } catch let error as APIError {
                app.toasts.show(error.userMessage(fallback: "Couldn't revert."))
            } catch {
                app.toasts.show("Couldn't revert.")
            }
        }
    }
}

/// The diff rows (web drawer): added lines on a success tint, deleted lines on
/// an error tint with line-through, changed words in a stronger tint, folded
/// runs as "⋯ n unchanged lines ⋯".
struct DiffRowsView: View {
    let diff: VersionDiff

    var body: some View {
        if diff.hasNoChanges {
            Text("No changes from the previous version.")
                .font(LooreFont.sans(13.6, .light))
                .foregroundStyle(LooreColor.textMuted)
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(diff.rows.enumerated()), id: \.offset) { _, row in
                    switch row {
                    case .skip(let count):
                        Text(LineDiff.skipLabel(count: count))
                            .font(LooreFont.sans(11.2, .light))
                            .foregroundStyle(LooreColor.textMuted.opacity(0.7))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    case .line(let op):
                        line(op)
                    }
                }
            }
            .textSelection(.enabled)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("history.diff")
        }
    }

    private func line(_ op: LineDiff.Op) -> some View {
        Self.text(op)
            .font(LooreFont.sans(12.8, .light))
            .lineSpacing(7.5)
            .padding(.horizontal, 6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background(op.type), in: RoundedRectangle(cornerRadius: 2))
            .accessibilityLabel(Self.accessibilityPrefix(op.type) + op.text)
    }

    static func accessibilityPrefix(_ kind: LineDiff.Kind) -> String {
        switch kind {
        case .add: return "Added: "
        case .del: return "Removed: "
        case .same: return ""
        }
    }

    private func background(_ kind: LineDiff.Kind) -> Color {
        switch kind {
        case .add: return LooreColor.success.opacity(0.12)
        case .del: return LooreColor.error.opacity(0.10)
        case .same: return .clear
        }
    }

    /// The line as styled runs (segments when the word refine kept them).
    static func text(_ op: LineDiff.Op) -> Text {
        let base: Color = op.type == .add ? LooreColor.textPrimary : LooreColor.textMuted
        guard let segments = op.segments else {
            let t = Text(op.text.isEmpty ? " " : op.text).foregroundColor(base)
            return op.type == .del ? t.strikethrough(color: LooreColor.error.opacity(0.45)) : t
        }
        return segments.reduce(Text("")) { result, segment in
            var run = AttributedString(segment.text)
            run.foregroundColor = base
            if segment.changed {
                switch op.type {
                case .add:
                    run.backgroundColor = LooreColor.success.opacity(0.32)
                case .del:
                    run.backgroundColor = LooreColor.error.opacity(0.28)
                    run.strikethroughStyle = .single
                    run.strikethroughColor = UIColor(LooreColor.error.opacity(0.55))
                case .same:
                    break
                }
            }
            return result + Text(run)
        }
    }
}
