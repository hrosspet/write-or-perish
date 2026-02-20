import React from "react";
import ImportData from "../components/ImportData";

export default function ImportPage() {
  return (
    <div style={{
      maxWidth: 600,
      margin: "0 auto",
      padding: "3rem 1.5rem",
    }}>
      <h2 style={{
        fontFamily: "var(--serif)",
        fontWeight: 300,
        fontSize: "1.4rem",
        color: "var(--text-primary)",
        marginBottom: "1.5rem",
      }}>
        Import Data
      </h2>
      <ImportData inline />
    </div>
  );
}
