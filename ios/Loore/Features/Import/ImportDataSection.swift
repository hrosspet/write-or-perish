import SwiftUI
import UniformTypeIdentifiers

/// The four archive importers with their dialogs (web `ImportData`): file
/// picker, extract/analyze stages, the confirm dialogs with privacy and AI
/// usage (import defaults: Private, None), the deleted-content question and
/// "Import Finished".
struct ImportDataSection: View {
    /// In the Welcome sheet: same options, no page chrome.
    var compact = false

    @Environment(AppState.self) private var app
    @State private var model: ImportModel?

    var body: some View {
        Group {
            if let model {
                ImportOptions(model: model)
            } else {
                Color.clear.frame(height: 1)
            }
        }
        .onAppear { if model == nil { model = ImportModel(app: app) } }
    }
}

private struct ImportOptions: View {
    @Bindable var model: ImportModel
    @State private var pickerKind: ImportKind?

    var body: some View {
        VStack(alignment: .leading, spacing: 9.6) {
            if let error = model.error {
                Text(error)
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.accent)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("import.error")
            }
            if model.busy && model.analysis == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small).tint(LooreColor.textSecondary)
                    Text(model.stage.flatMap { ImportModel.stageLabels[$0] } ?? "Working…")
                }
                .font(LooreFont.sans(14.7, .light))
                .foregroundStyle(LooreColor.textSecondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .accessibilityIdentifier("import.stage")
            } else {
                #if DEBUG
                // UI tests: `-LooreDebugImportFile <path> -LooreDebugImportKind markdown|claude|chatgpt|twitter`
                // stands in for the system file picker.
                if let path = UserDefaults.standard.string(forKey: "LooreDebugImportFile"),
                   let kind = UserDefaults.standard.string(forKey: "LooreDebugImportKind").flatMap(ImportKind.init) {
                    Button("Debug: import \((path as NSString).lastPathComponent)") {
                        Task { await model.picked(URL(fileURLWithPath: path), kind: kind) }
                    }
                    .font(LooreFont.meta)
                    .accessibilityIdentifier("import.debugFile")
                }
                #endif
                ForEach(ImportKind.allCases) { kind in
                    Button { pickerKind = kind } label: {
                        Text(kind.buttonTitle)
                            .font(LooreFont.sans(14.7, .light))
                            .foregroundStyle(LooreColor.textSecondary)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 14)
                            .padding(.horizontal, 20)
                            .overlay(RoundedRectangle(cornerRadius: LooreRadius.small).strokeBorder(LooreColor.border))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("import.\(kind.rawValue)")
                }
            }
        }
        .fileImporter(isPresented: Binding(get: { pickerKind != nil }, set: { if !$0 { pickerKind = nil } }),
                      allowedContentTypes: [.zip]) { outcome in
            let kind = pickerKind
            pickerKind = nil
            guard let kind, case .success(let url) = outcome else { return }
            Task { await model.picked(url, kind: kind) }
        }
        // One presenter whose content switches (chained dialogs, PROGRESS.md M2 note).
        .looreDialog(isPresented: Binding(get: { model.analysis != nil || model.result != nil },
                                          set: { shown in
                                              guard !shown else { return }
                                              if model.result != nil { Task { await model.acknowledgeResult() } }
                                          }),
                     dismissOnBackdropTap: model.result != nil) {
            if let result = model.result {
                ImportFinishedDialog(result: result) { Task { await model.acknowledgeResult() } }
            } else if let count = model.deletedMatches {
                DeletedContentDialog(count: count) { choice in
                    Task { await model.resolveDeleted(choice) }
                }
            } else if let analysis = model.analysis {
                ConfirmImportDialog(model: model, analysis: analysis)
            }
        }
    }
}

/// Serif title and the short accent rule (web `ModalTitle`).
private struct ImportDialogTitle: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(text)
                .font(LooreFont.dialogTitle)
                .foregroundStyle(LooreColor.textPrimary)
            Rectangle().fill(LooreColor.accent.opacity(0.6)).frame(width: 32, height: 1)
        }
    }
}

/// "Confirm Import" / "Confirm Claude Import" / "Confirm ChatGPT Import" / "Confirm Twitter Import".
private struct ConfirmImportDialog: View {
    @Bindable var model: ImportModel
    let analysis: ImportAnalysis

    var body: some View {
        LooreDialogCard {
            ImportDialogTitle(text: title)
            summary
            if analysis.kind == .markdown || analysis.kind == .twitter {
                if analysis.kind == .twitter {
                    Button { model.includeReplies.toggle() } label: {
                        HStack(spacing: 8) {
                            Image(systemName: model.includeReplies ? "checkmark.square" : "square")
                            Text("Include replies (\(analysis.replyCount))")
                        }
                        .font(LooreFont.sans(14.4, .light))
                        .foregroundStyle(LooreColor.textSecondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityValue(model.includeReplies ? "Checked" : "Not checked")
                }
                radioGroup("Import Type", selection: $model.importType, options: [
                    ("separate_nodes", analysis.kind == .twitter
                        ? "Import as separate top-level nodes (one node per tweet)"
                        : "Import as separate top-level nodes (one thread per file)"),
                    ("single_thread", analysis.kind == .twitter
                        ? "Import as a single thread (all tweets connected sequentially)"
                        : "Import as a single thread (all files connected sequentially)"),
                ])
                if analysis.kind == .markdown {
                    radioGroup("Date Ordering", selection: $model.dateOrdering, options: [
                        ("modified", "Order by modification date"),
                        ("created", "Order by creation date"),
                    ])
                }
            }
            PrivacySelectorView(privacy: $model.privacy, aiUsage: $model.aiUsage, disabled: model.busy)
            if let error = model.error {
                Text(error).font(LooreFont.sans(13.6, .light)).foregroundStyle(LooreColor.accent)
            }
            HStack(spacing: 10) {
                Button {
                    Task { await model.confirm() }
                } label: {
                    if model.busy {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.mini).tint(LooreColor.accent)
                            Text(progressLabel)
                        }
                    } else {
                        Text("Confirm Import")
                    }
                }
                .buttonStyle(.loorePrimary)
                .disabled(model.busy)
                .accessibilityIdentifier("import.confirm")
                Button("Cancel") { model.cancel() }
                    .buttonStyle(.looreOutline)
                    .disabled(model.busy)
                    .accessibilityIdentifier("import.cancel")
            }
            .padding(.top, 4)
        }
    }

    private var title: String {
        switch analysis.kind {
        case .markdown: return "Confirm Import"
        case .claude: return "Confirm Claude Import"
        case .chatgpt: return "Confirm ChatGPT Import"
        case .twitter: return "Confirm Twitter Import"
        }
    }

    private var progressLabel: String {
        if let progress = model.progress, analysis.kind == .twitter {
            return "Importing… \(progress.done) / \(progress.total)"
        }
        return "Importing…"
    }

    @ViewBuilder private var summary: some View {
        VStack(alignment: .leading, spacing: 6) {
            switch analysis.kind {
            case .markdown:
                line(Text("Found ") + bold(analysis.totalFiles) + Text(" .md file(s) (\(jsLocaleNumber(analysis.totalSize)) bytes)"))
                line(Text("Estimated tokens: ") + bold(analysis.totalTokens))
            case .claude, .chatgpt:
                line(Text("Found ") + bold(analysis.totalConversations) + Text(" conversation(s) with ")
                     + bold(analysis.totalMessages) + Text(" messages"))
                line(Text("Estimated tokens: ") + bold(analysis.totalTokens))
                line(Text("Each conversation will be imported as a separate thread."))
            case .twitter:
                line(Text("Found ") + bold(analysis.totalTweets)
                     + Text(" tweets (\(analysis.originalCount) original, \(analysis.replyCount) replies)"))
                if analysis.skippedRetweets > 0 {
                    line(Text("Skipped \(analysis.skippedRetweets) retweets"))
                }
                line(Text("Estimated tokens: ")
                     + bold(model.includeReplies ? analysis.totalTokens : analysis.originalTokens)
                     + Text(" (\(model.includeReplies ? analysis.totalTweets : analysis.originalCount) tweets)"))
            }
        }
        .accessibilityElement(children: .contain)
            .accessibilityIdentifier("import.summary")
    }

    private func bold(_ n: Int) -> Text {
        Text(jsLocaleNumber(n)).font(LooreFont.sans(14.4, .medium)).foregroundColor(LooreColor.textPrimary)
    }

    private func line(_ text: Text) -> some View {
        text
            .font(LooreFont.sans(14.4, .light))
            .foregroundStyle(LooreColor.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func radioGroup(_ label: String, selection: Binding<String>, options: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            FieldLabel(text: label)
            ForEach(options, id: \.0) { value, title in
                Button { selection.wrappedValue = value } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: selection.wrappedValue == value ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(selection.wrappedValue == value ? LooreColor.accent : LooreColor.textMuted)
                        Text(title)
                            .foregroundStyle(LooreColor.textSecondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .font(LooreFont.sans(14.4, .light))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(model.busy)
                .accessibilityAddTraits(selection.wrappedValue == value ? .isSelected : [])
            }
        }
    }
}

/// "Previously Deleted Content" (409 `deleted_content_matches`).
private struct DeletedContentDialog: View {
    let count: Int
    let onChoice: (String?) -> Void

    var body: some View {
        let many = count != 1
        LooreDialogCard {
            ImportDialogTitle(text: "Previously Deleted Content")
            (Text("\(count)").font(LooreFont.sans(14.7, .medium)).foregroundColor(LooreColor.textPrimary)
             + Text(" message\(many ? "s" : "") in this import \(many ? "match" : "matches") content you previously deleted. Restore \(many ? "them" : "it"), or keep \(many ? "them" : "it") deleted?"))
                .font(LooreFont.body)
                .foregroundStyle(LooreColor.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 8) {
                Button("Restore deleted content") { onChoice("restore") }.buttonStyle(.loorePrimary)
                Button("Keep it deleted") { onChoice("skip") }.buttonStyle(.looreOutline)
                Button("Cancel import") { onChoice(nil) }.buttonStyle(.looreQuiet)
            }
        }
    }
}

/// "Import Finished" with the big serif numbers and the notes.
private struct ImportFinishedDialog: View {
    let result: ImportResult
    let onOK: () -> Void

    var body: some View {
        LooreDialogCard {
            ImportDialogTitle(text: "Import Finished")
            HStack(spacing: 28) {
                ForEach(result.stats, id: \.label) { stat in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(jsLocaleNumber(stat.value))
                            .font(LooreFont.serif(35.2, .light, relativeTo: .largeTitle))
                            .foregroundStyle(stat.highlight ? LooreColor.accent : LooreColor.textPrimary)
                        Text(stat.label.uppercased())
                            .font(LooreFont.sans(11.2, .regular))
                            .tracking(1.1)
                            .foregroundStyle(LooreColor.textMuted)
                    }
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("import.finished")
            ForEach(result.notes, id: \.self) { note in
                Text(note)
                    .font(LooreFont.sans(13.6, .light))
                    .foregroundStyle(LooreColor.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Button("OK", action: onOK)
                .buttonStyle(.loorePrimary)
                .accessibilityIdentifier("import.ok")
        }
    }
}
