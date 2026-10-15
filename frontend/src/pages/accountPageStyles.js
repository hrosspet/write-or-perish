// Shared layout of the one-message pages around account deletion (#269):
// confirm from the email link, deleted, restore.
export const pageStyle = {
  minHeight: "70vh", display: "flex", flexDirection: "column",
  alignItems: "center", justifyContent: "center",
  padding: "4rem 1.5rem", textAlign: "center",
};

export const headingStyle = {
  fontFamily: "var(--serif)", fontWeight: 300,
  fontSize: "clamp(1.6rem, 4vw, 2.2rem)", lineHeight: 1.2,
  color: "var(--text-primary)", maxWidth: 520, marginBottom: "1.2rem",
};

export const textStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1rem",
  lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 480,
  marginBottom: "1.4rem", overflowWrap: "anywhere",
};

export const linkStyle = {
  fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
  color: "var(--accent)", textDecoration: "none",
  borderBottom: "1px solid var(--accent-glow)", paddingBottom: 2,
};

export const choiceRowStyle = {
  display: "flex", flexDirection: "column", gap: "8px",
  width: "100%", maxWidth: 360, marginTop: "0.6rem",
};

export const choiceStyle = {
  fontFamily: "var(--sans)", fontSize: "0.92rem", fontWeight: 400,
  padding: "10px 16px", borderRadius: "6px", cursor: "pointer",
  background: "var(--bg-deep)", border: "1px solid var(--border)",
  color: "var(--text-secondary)",
};
