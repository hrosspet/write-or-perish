import React, { useState } from 'react';
import api from '../api';

function TermsModal({ onAccepted }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleAgree = () => {
    setLoading(true);
    api.post('/terms/accept')
      .then(response => {
        setLoading(false);
        // Pass the accepted_terms_at back to the parent so it can update global state.
        onAccepted(response.data.accepted_terms_at);
      })
      .catch(err => {
        setLoading(false);
        setError("Error accepting terms. Please try again.");
      });
  };

  const modalStyle = {
    position: "fixed",
    top: 0, left: 0, right: 0, bottom: 0,
    backgroundColor: "rgba(0, 0, 0, 0.7)",
    backdropFilter: "blur(8px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 10000
  };

  const contentStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    padding: "2rem",
    borderRadius: "12px",
    width: "80%",
    maxWidth: "800px",
    color: "var(--text-primary)",
    maxHeight: "80vh",
    overflowY: "auto",
    fontFamily: "var(--sans)",
    fontWeight: 300,
  };

  return (
    <div style={modalStyle}>
      <div style={contentStyle}>
        <h2 style={{ fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.6rem" }}>Terms & Conditions</h2>
        <p style={{ color: "var(--text-secondary)" }}>1. Alpha release</p>
        <ul>
          <li style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
              Write or Perish is currently in its alpha release stage. This early version is provided "as is," without any warranties of any kind. The app may contain bugs, errors, or other issues, and may not perform as expected. By using Write or Perish during this alpha phase, you acknowledge that you are doing so at your own risk; the developers assume no responsibility for any data loss, damages, or other problems that may occur.
          </li>
        </ul>
        <p style={{ color: "var(--text-secondary)" }}>2. Public Data and License</p>
          <ul>
            <li style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
              All content you create (including diary entries, musings, replies, or any inputs you provide) will be made publicly available under the Creative Commons Attribution 4.0 International (CC BY 4.0) license.
            </li>
            <li style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
              This means you grant anyone worldwide permission to use, share, and adapt your content as long as proper credit is given.
            </li>
          </ul>
        <p style={{ color: "var(--text-secondary)" }}>3. No Expectation of Privacy</p>
          <ul>
            <li style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
              Do not include sensitive, confidential, proprietary information, "Protected Health Information," as defined under the HIPAA Privacy Rule (45 C.F.R. Section 160.103), or any personal data of children under 13 or the applicable age of digital consent. Everything you post is public and may be repurposed for research, art, commercial use, or for training AI models.
            </li>
          </ul>
        <p style={{ color: "var(--text-secondary)" }}>4. OpenAI Content Sharing and Development</p>
          <ul>
            <li style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
              By using the OpenAI-powered aspects of our app, you also confirm your agreement to the following Content Sharing Agreement with OpenAI:
            </li>
          </ul>
        <hr style={{ borderColor: "var(--border)" }} />
        <div style={{
          backgroundColor: "var(--bg-deep)",
          padding: "16px",
          borderRadius: "8px",
          marginTop: "10px",
          border: "1px solid var(--border)",
        }}>
          <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
            This Content Sharing Agreement is between OpenAI, L.L.C. ("us" or "we") and you ("Customer"). This Content Sharing Agreement is incorporated into the terms located at  unless the parties have negotiated a separate agreement for the Services, in which case such agreement will govern (the "Business Terms"). Capitalized terms not defined here are defined in the Business Terms or the Data Processing Agreement between the parties in connection with the Services (the "DPA"). This Content Sharing Agreement takes precedence in the event of any conflict.
          </p>
          <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
            Notwithstanding anything set forth in the Business Terms, we may use Customer Content to develop and improve the Services, including for training our models and other research, development, evaluation, and testing purposes ("Development Purposes"). You expressly agree that use of Customer Data for the Development Purposes is not subject to the provisions of the DPA. OpenAI will process Customer Data for Development Purposes as an independent Data Controller. You are responsible for all Input provided by you and your End Users.
          </p>
          <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
            You also represent and warrant that you have the rights, licenses, and permissions necessary – including as applicable that you have provided any notice to End Users, and collected any relevant consent from End Users ("Notice") – to provide the Input to the Services for the Development Purposes. You agree that you and your End Users will not provide any information as Input to the Services that you or your End Users do not want to be used for Development Purposes, such as sensitive, confidential, or proprietary information. You also agree that you will not use the Services to process (a) any data that includes or constitutes "Protected Health Information," as defined under the HIPAA Privacy Rule (45 C.F.R. Section 160.103), or (b) any Personal Data of children under 13 or the applicable age of digital consent. You also agree that you will provide OpenAI a copy of your Notice upon OpenAI's request.
          </p>
        </div>
        <p style={{ color: "var(--text-secondary)", lineHeight: 1.6 }}>
          By clicking "I Agree", you confirm that you have read and understand these terms and that you consent to your content being published and used as described.
        </p>
        {error && <p style={{ color: "var(--accent)" }}>{error}</p>}
        <button
          onClick={handleAgree}
          disabled={loading}
          style={{
            marginTop: "20px",
            borderColor: "var(--accent)",
            color: "var(--accent)",
            padding: "12px 24px",
            fontSize: "1rem",
          }}
        >
          {loading ? "Processing..." : "I Agree"}
        </button>
      </div>
    </div>
  );
}

export default TermsModal;
