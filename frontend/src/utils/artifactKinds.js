// Canonical order of the built-in (default) artifact kinds. These are the
// out-of-the-box artifacts the AI maintains; they're pinned above the divider
// in the nav dropdown (after Profile/Todo) and listed first (in this order)
// on the Artifacts page. User/AI-created custom kinds follow, alphabetically.
// `intentions` is added by #202 and slots in automatically once that kind
// exists. `ai_preferences` was folded into the artifact model in #158 Slice 5
// (it used to be a hardcoded dropdown link to /ai-preferences); kept last to
// preserve its prior position at the end of the curated group.
// Full dropdown order: Profile, Todo, [these].
export const BUILTIN_KIND_ORDER = [
  'intentions',
  'predictions',
  'memory',
  'scratchpad',
  'ai_preferences',
];

export const isBuiltinKind = (kind) => BUILTIN_KIND_ORDER.includes(kind);

// Sort comparator: built-in kinds first (in BUILTIN_KIND_ORDER), then custom
// kinds alphabetically by title. Used to keep the dropdown and Artifacts page
// in the same order.
export function compareArtifacts(a, b) {
  const ia = BUILTIN_KIND_ORDER.indexOf(a.kind);
  const ib = BUILTIN_KIND_ORDER.indexOf(b.kind);
  const ra = ia === -1 ? Infinity : ia;
  const rb = ib === -1 ? Infinity : ib;
  if (ra !== rb) return ra - rb;
  return (a.title || a.kind).localeCompare(b.title || b.kind);
}
