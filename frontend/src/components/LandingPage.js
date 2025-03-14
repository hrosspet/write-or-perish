import React from "react";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function LandingPage() {
  return (
    <div style={{ padding: "20px" }}>
      <h1>Welcome to Write or Perish</h1>
      <p>
        Write or Perish is a digital journal for recording your thoughts,
        stories, and emotions. All entries are public and may be used for training
        future AI models.
      </p>
      <p>
        Please do not include personally sensitive data or any HIPAA‑protected
        information.
      </p>
      <h3>How It Works</h3>
      <ul>
        <li>Create text nodes that form a tree of personal expression.</li>
        <li>
          Optionally request an LLM response to add to the conversation (each LLM
          response counts toward our collective daily 1M token goal).
        </li>
        <li>
          Link nodes together to refer to previous entries and build a rich archive.
        </li>
      </ul>
      <p>
        <a href={`${backendUrl}/auth/login`}>Login with Twitter</a> to begin your journey!
      </p>
    </div>
  );
}

export default LandingPage;