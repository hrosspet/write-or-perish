import Foundation

/// A request for an AI reply (`POST /nodes/<id>/llm`) that failed or was refused,
/// after LLM Response or after an entry with Auto-generate on. Every path that
/// makes the request shows the failure the same way (#747): a toast with the
/// server's sentence, unless the page already says it another way. The web's
/// copy is `frontend/src/utils/llmRequestError.js`.
enum LLMRequestError {
    static let fallbackMessage = "Error requesting LLM response."

    /// The toast for `error`, or nil when there is none to show: the monthly
    /// usage limit raises its banner; an automatic reply refused under a shared
    /// node is not an error (LLM Response stays offered); a thread too long for
    /// another reply (#648) shows in the reply form's limit state, on the page
    /// the refusal came from or on the entry's page.
    static func toastMessage(_ error: Error) -> String? {
        if SpendCap.isSpendCapError(error) { return nil }
        // No AI replies on the account (#438): the "REPLIES PAUSED" banner is on
        // screen and says it; a toast with the same sentence repeats it (#776).
        if case .subscriptionRequired? = error as? APIError { return nil }
        guard let apiError = error as? APIError else { return fallbackMessage }
        if apiError.isAutoGenerateOff || apiError.isThreadTooLong { return nil }
        return apiError.userMessage(fallback: fallbackMessage)
    }
}
