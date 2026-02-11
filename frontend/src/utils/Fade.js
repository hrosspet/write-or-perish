import React, { useRef } from "react";
import useOnScreen from "./useOnScreen";

export default function Fade({ children, delay = 0, className = "", style = {} }) {
  const ref = useRef(null);
  const visible = useOnScreen(ref, 0.08);
  return (
    <div
      ref={ref}
      className={className}
      style={{
        opacity: visible ? 1 : 0,
        transform: visible ? "translateY(0)" : "translateY(28px)",
        transition: `opacity 0.9s cubic-bezier(0.22,1,0.36,1) ${delay}s, transform 0.9s cubic-bezier(0.22,1,0.36,1) ${delay}s`,
        ...style,
      }}
    >
      {children}
    </div>
  );
}
