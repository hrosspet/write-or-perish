import SwiftUI
import Observation

/// The options a model picker shows (port of `ModelSelector.pickerOptions`, #355).
enum ModelPickerOptions: Equatable {
    struct Group: Equatable {
        var label: String
        var models: [ModelInfo]
    }

    case flat(models: [ModelInfo], more: Bool)
    case grouped([Group])

    static let providers = [("anthropic", "Anthropic"), ("openai", "OpenAI")]

    /// The models a picker of this purpose may offer (web `offeredModels`): the read
    /// models for a Read, the chat models otherwise. A read-only model is in the
    /// Read picker only.
    static func offered(_ models: [ModelInfo], purpose: ModelPurpose) -> [ModelInfo] {
        models.filter { purpose == .read ? $0.read : $0.chat }
    }

    /// read: only read models. chat collapsed: featured (Anthropic first) with a
    /// non-featured selection first, then "More models…". chat expanded: all chat
    /// models, grouped.
    static func make(models: [ModelInfo], selectedId: String?, purpose: ModelPurpose, expanded: Bool) -> ModelPickerOptions {
        let candidates = offered(models, purpose: purpose)
        if purpose == .read { return .flat(models: candidates, more: false) }
        if expanded {
            return .grouped(providers.map { id, label in Group(label: label, models: candidates.filter { $0.provider == id }) }
                .filter { !$0.models.isEmpty })
        }
        let byProvider = providers.flatMap { id, _ in candidates.filter { $0.provider == id } }
        let featured = byProvider.filter(\.featured)
        let selected = candidates.first { $0.id == selectedId }
        let short = selected.map { !$0.featured ? [$0] + featured : featured } ?? featured
        return .flat(models: short, more: short.count < candidates.count)
    }

    var allModels: [ModelInfo] {
        switch self {
        case .flat(let models, _): return models
        case .grouped(let groups): return groups.flatMap(\.models)
        }
    }

    /// The default the backend suggests replaces the selection when it is a
    /// thread predecessor, or when the picker cannot offer the selection (a
    /// deprecated model, a chat model in the Read picker, a read-only model in a
    /// chat picker). Returns the new selection, or nil to keep the current one.
    static func appliedSuggestion(models: [ModelInfo], suggestion: SuggestedModel, selected: String?,
                                  purpose: ModelPurpose) -> String? {
        let selectable = offered(models, purpose: purpose).contains { $0.id == selected }
        guard suggestion.source == "predecessor" || !selectable else { return nil }
        guard let suggested = suggestion.suggestedModel, suggested != selected else { return nil }
        return suggested
    }
}

enum ModelPurpose { case chat, read }

/// `GET /api/nodes/models`, fetched once per session and backend.
@MainActor
@Observable
final class ModelCatalog {
    static let shared = ModelCatalog()
    private(set) var models: [ModelInfo]?
    @ObservationIgnored private var loadedFor: String?

    func load(_ api: APIClient) async {
        let key = api.environment.rawValue
        if models != nil && loadedFor == key { return }
        if let answer: ModelsResponse = try? await api.get(APIPath.nodeModels) {
            models = answer.models
            loadedFor = key
        }
    }
}

/// The model picker joined to the right of an action button (web `ModelSelector`).
/// As wide as the model's name, like the web's inline-flex button (#398).
struct ModelPicker: View {
    let nodeId: Int?
    @Binding var selectedModel: String?
    var purpose: ModelPurpose = .chat
    var disabled = false
    /// A quiet text button (the Voice screen's Glean, #475) instead of the half
    /// joined to an action button's right edge.
    var standalone = false

    @Environment(AppState.self) private var app
    @State private var catalog = ModelCatalog.shared
    @State private var suggestionLoaded = false
    @State private var open = false
    @State private var expanded = false

    var body: some View {
        let models = catalog.models
        let selected = models?.first { $0.id == selectedModel }
        let inactive = !suggestionLoaded || disabled || models == nil
        Button {
            open = true
        } label: {
            HStack(spacing: standalone ? 6 : 10) {
                Text(suggestionLoaded && models != nil ? (selected?.name ?? "") : "…")
                    .lineLimit(1)
                Image(systemName: "chevron.down").font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(LooreColor.textMuted)
            }
            .font(standalone ? LooreFont.sans(13.6, .light) : LooreFont.button)
            .foregroundStyle(standalone ? LooreColor.textMuted : LooreColor.textSecondary)
            .padding(.vertical, 10)
            .padding(.horizontal, 14)
            .overlay {
                if !standalone {
                    UnevenRoundedRectangle(bottomTrailingRadius: LooreRadius.control,
                                           topTrailingRadius: LooreRadius.control)
                        .strokeBorder(LooreColor.border)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(inactive)
        .opacity(inactive && suggestionLoaded ? 0.45 : 1)
        .accessibilityLabel("\(purpose == .read ? "Model for Glean" : "Model"): \(selected?.name ?? "")")
        .popover(isPresented: $open, attachmentAnchor: .rect(.bounds), arrowEdge: .bottom) {
            list
                .presentationCompactAdaptation(.popover)
                .onDisappear { expanded = false }
        }
        .task(id: nodeId) { await loadSuggestion() }
    }

    private var list: some View {
        let options = ModelPickerOptions.make(models: catalog.models ?? [], selectedId: selectedModel,
                                              purpose: purpose, expanded: expanded)
        return ScrollView {
            VStack(alignment: .leading, spacing: 2) {
                switch options {
                case .flat(let models, let more):
                    ForEach(models) { row($0) }
                    if more {
                        Button {
                            withAnimation(LooreMotion.quick) { expanded = true }
                        } label: {
                            Text("More models…")
                                .font(LooreFont.sans(14, .regular))
                                .foregroundStyle(LooreColor.textSecondary)
                                .padding(.vertical, 8)
                                .padding(.horizontal, 12)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                case .grouped(let groups):
                    ForEach(groups, id: \.label) { group in
                        Text(group.label)
                            .font(LooreFont.serif(16, .regular))
                            .foregroundStyle(LooreColor.accent)
                            .padding(.horizontal, 12)
                            .padding(.top, 8)
                            .accessibilityAddTraits(.isHeader)
                        ForEach(group.models) { row($0) }
                    }
                }
            }
            .padding(6)
        }
        .frame(minWidth: 220, maxHeight: 320)
        .background(LooreColor.bgCard)
    }

    private func row(_ model: ModelInfo) -> some View {
        Button {
            if model.id != selectedModel { selectedModel = model.id }
            open = false
        } label: {
            HStack(spacing: 16) {
                Text(model.name)
                Spacer()
                if model.id == selectedModel {
                    Image(systemName: "checkmark").font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(LooreColor.accent)
                }
            }
            .font(LooreFont.sans(14, .regular))
            .foregroundStyle(model.id == selectedModel ? LooreColor.textPrimary : LooreColor.textSecondary)
            .padding(.vertical, 8)
            .padding(.horizontal, 12)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(model.id == selectedModel ? .isSelected : [])
    }

    private func loadSuggestion() async {
        await catalog.load(app.api)
        let path = nodeId.map(APIPath.suggestedModel) ?? APIPath.defaultModel
        let query = purpose == .read ? [URLQueryItem(name: "purpose", value: "read")] : []
        let suggestion: SuggestedModel? = try? await app.api.get(path, query: query)
        suggestionLoaded = true
        if let suggestion, let models = catalog.models,
           let applied = ModelPickerOptions.appliedSuggestion(models: models, suggestion: suggestion,
                                                              selected: selectedModel, purpose: purpose) {
            selectedModel = applied
        }
    }
}
