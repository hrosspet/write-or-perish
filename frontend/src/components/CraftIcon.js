import React from "react";

/**
 * Sliders glyph that marks craft mode: the toggle itself and every menu
 * item that only shows while it is on. One icon in both places is how
 * a user who never learned the name still ties the extra entries back
 * to the switch. Inline SVG like the theme toggle so it inherits
 * currentColor and needs no icon package.
 */
export default function CraftIcon({ size = 14, style }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ flexShrink: 0, ...style }}
    >
      <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" />
      <path d="M1 14h6M9 8h6M17 16h6" />
    </svg>
  );
}
