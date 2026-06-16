// Canonical order of the built-in (default) artifact kinds. These are the
// out-of-the-box artifacts the AI maintains; they're pinned above the divider
// in the nav dropdown (between Todo and AI Interaction Preferences) and listed
// first (in this order) on the Artifacts page. User/AI-created custom kinds
// follow, alphabetically. `intentions` is added by #202 and slots in
// automatically once that kind exists.
// Full dropdown order: Profile, Todo, [these], AI Interaction Preferences.
export const BUILTIN_KIND_ORDER = [
  'intentions',
  'predictions',
  'memory',
  'scratchpad',
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
