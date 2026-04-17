// Mirrors backend/utils/privacy.py: the set of ai_usage values that
// permit AI to read the node's content (chat or train). ai_usage
// 'none' means the node is excluded from any LLM prompt context.
//
// Keep in sync with `AI_ALLOWED` in backend/utils/privacy.py.
export const AI_ALLOWED = new Set(['chat', 'train']);

/** True if the given ai_usage value lets AI see the node. */
export function isAiAllowed(aiUsage) {
  return AI_ALLOWED.has(aiUsage);
}

/**
 * True if every node in the given list has ai_usage in AI_ALLOWED.
 * Use for "can I auto-generate an LLM reply on this thread?" checks —
 * if ANY ancestor is 'none', auto-gen should be suppressed (the
 * omitted content would produce partial / incoherent replies).
 */
export function contextAllowsAi(nodes) {
  if (!Array.isArray(nodes) || nodes.length === 0) return true;
  return nodes.every(n => n && AI_ALLOWED.has(n.ai_usage));
}
