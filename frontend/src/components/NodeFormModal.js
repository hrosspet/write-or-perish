import React, { useEffect } from "react";
import NodeForm from "./NodeForm";

function NodeFormModal({ title, onClose, nodeFormProps }) {
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed",
        top: 0, left: 0, right: 0, bottom: 0,
        backgroundColor: "rgba(0,0,0,0.7)",
        backdropFilter: "blur(8px)",
        WebkitBackdropFilter: "blur(8px)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "relative",
          background: "var(--bg-card)",
          border: "1px solid var(--border)",
          padding: "2rem",
          borderRadius: "12px",
          width: "1170px",
          maxWidth: "90vw",
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <button
          style={{
            position: "absolute",
            top: "12px",
            right: "16px",
            fontSize: "24px",
            color: "var(--text-muted)",
            cursor: "pointer",
            background: "none",
            border: "none",
            padding: "4px",
            lineHeight: 1,
          }}
          onClick={onClose}
        >
          &times;
        </button>
        <h2 style={{
          fontFamily: "var(--serif)",
          fontSize: "1.4rem",
          fontWeight: 300,
          color: "var(--text-primary)",
          marginBottom: "20px",
        }}>
          {title}
        </h2>
        <NodeForm {...nodeFormProps} />
      </div>
    </div>
  );
}

export default NodeFormModal;
