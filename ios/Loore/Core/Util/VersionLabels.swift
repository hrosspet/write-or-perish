import Foundation

/// Version-history row labels: `generatedByLabel` in
/// `components/VersionHistoryDrawer.js`.
enum VersionLabels {
    /// "Manual edit", "Reverted", "Orient session", "Voice", "Imported", or
    /// by generation type "Auto-updated (g)", "Iterative build (g)",
    /// "Integrated profile (g)", else "Auto-generated (g)". A nil
    /// `generatedBy` prints as "null", as the web's template string does.
    static func versionLabel(generatedBy: String?, generationType: String?) -> String {
        switch generatedBy {
        case "user", "manual": return "Manual edit"
        case "revert": return "Reverted"
        case "orient_session": return "Orient session"
        case "voice_session": return "Voice"
        case "import": return "Imported"
        default: break
        }
        let g = generatedBy ?? "null"
        switch generationType {
        case "update": return "Auto-updated (\(g))"
        case "iterative": return "Iterative build (\(g))"
        case "integration": return "Integrated profile (\(g))"
        default: return "Auto-generated (\(g))"
        }
    }
}
