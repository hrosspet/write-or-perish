import React, { useState } from "react";
import { Link } from "react-router-dom";

export const ctaButtonStyle = {
  display: "inline-flex",
  alignItems: "center",
  gap: "0.6rem",
  fontFamily: "var(--sans)",
  fontWeight: 400,
  fontSize: "0.95rem",
  letterSpacing: "0.06em",
  padding: "14px 36px",
  border: "1px solid var(--accent)",
  background: "transparent",
  color: "var(--accent)",
  textDecoration: "none",
  cursor: "pointer",
  transition: "all 0.4s cubic-bezier(0.22,1,0.36,1)",
  position: "relative",
  overflow: "hidden",
  boxShadow: "none",
  transform: "translateY(0)",
  borderRadius: 0,
};

export const ctaButtonHoverStyle = {
  background: "var(--accent-subtle)",
  boxShadow: "0 0 30px var(--accent-glow)",
  transform: "translateY(-1px)",
};

export default function CtaButton({ children, to, href, onClick, style, className }) {
  const [hovered, setHovered] = useState(false);

  const composedStyle = {
    ...ctaButtonStyle,
    ...(hovered ? ctaButtonHoverStyle : {}),
    ...style,
  };

  const content = (
    <>
      <span>{children}</span>
      <span style={{
        fontSize: "1.1rem",
        transition: "transform 0.3s ease",
        transform: hovered ? "translateX(3px)" : "translateX(0)",
      }}>&rarr;</span>
    </>
  );

  const props = {
    style: composedStyle,
    className,
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => setHovered(false),
  };

  if (to) return <Link to={to} {...props}>{content}</Link>;
  if (href) return <a href={href} {...props}>{content}</a>;
  return <button {...props} onClick={onClick}>{content}</button>;
}
