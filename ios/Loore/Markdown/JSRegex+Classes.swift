import Foundation

/// JS regex classes spelled out for ICU patterns, for ports that must match
/// the web on every input. ICU's `\s` also matches U+0085 and misses U+FEFF;
/// ICU's `.` also stops at \v, \f and U+0085; ICU's `$` also matches before
/// a final line terminator (use `\z` for JS `$` without the `m` flag).
extension JSRegex {
    /// JS `\s`.
    static let space = #"[\t\n\u000B\f\r    -     　﻿]"#
    /// JS `.` (no `s` flag): anything but a line terminator.
    static let dot = #"[^\n\r  ]"#
}
