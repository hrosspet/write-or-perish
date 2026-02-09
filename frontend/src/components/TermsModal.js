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
        onAccepted();
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

  const sectionTitle = {
    color: "var(--text-secondary)",
    fontWeight: 400,
    marginTop: "1.5rem",
    marginBottom: "0.5rem",
  };

  const text = {
    color: "var(--text-secondary)",
    lineHeight: 1.6,
  };

  const highlight = {
    backgroundColor: "var(--bg-deep)",
    padding: "16px",
    borderRadius: "8px",
    marginTop: "10px",
    border: "1px solid var(--border)",
  };

  const tldrItem = {
    color: "var(--text-secondary)",
    lineHeight: 1.6,
    marginBottom: "0.5rem",
  };

  return (
    <div style={modalStyle}>
      <div style={contentStyle}>
        <h2 style={{ fontFamily: "var(--serif)", fontWeight: 300, fontSize: "1.6rem" }}>
          Terms &amp; Conditions
        </h2>

        {/* TL;DR */}
        <div style={highlight}>
          <p style={{ ...sectionTitle, marginTop: 0, fontSize: "1.1rem" }}>
            <strong>TL;DR &mdash; The 5 Things You Need to Know</strong>
          </p>
          <ol style={{ paddingLeft: "1.2rem", margin: 0 }}>
            <li style={tldrItem}>
              <strong style={{ color: "var(--text-primary)" }}>This is alpha software.</strong> Things will break. Data could be lost. No guarantees.
            </li>
            <li style={tldrItem}>
              <strong style={{ color: "var(--text-primary)" }}>Your content is private by default.</strong> Only you can see it, and AI cannot access it, unless you explicitly change the settings.
            </li>
            <li style={tldrItem}>
              <strong style={{ color: "var(--text-primary)" }}>All content is encrypted at rest,</strong> but developers can technically decrypt it (every such event is logged). This is a trust-based model, not a cryptographic guarantee.
            </li>
            <li style={tldrItem}>
              <strong style={{ color: "var(--text-primary)" }}>If you enable AI chat,</strong> your content is sent to third-party AI providers (OpenAI, Anthropic) for processing. If you enable AI training, that's <strong style={{ color: "var(--text-primary)" }}>essentially irrevocable for already-trained models.</strong> Also, for AI training, <strong style={{ color: "var(--text-primary)" }}>don't submit content you don't have the rights for!</strong>
            </li>
            <li style={tldrItem}>
              <strong style={{ color: "var(--text-primary)" }}>You own your content.</strong> We claim no rights to it except what's needed to run the service &mdash; and, if you opt in to training, a license to train our own models too.
            </li>
          </ol>
        </div>

        <hr style={{ borderColor: "var(--border)", margin: "1.5rem 0" }} />

        {/* Section 1 */}
        <p style={sectionTitle}><strong>1. What Loore Is</strong></p>
        <p style={text}>
          Loore is a personal journaling app with optional AI features. It's currently in <strong style={{ color: "var(--text-primary)" }}>alpha release</strong> &mdash; meaning it is early, experimental software shared primarily among a small group of people.
        </p>
        <p style={text}>By using Loore, you agree to the terms below.</p>

        {/* Section 2 */}
        <p style={sectionTitle}><strong>2. Alpha Disclaimer &mdash; Please Read This</strong></p>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>Loore is provided "as is," with no warranties of any kind.</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>The app <strong style={{ color: "var(--text-primary)" }}>will</strong> contain bugs. Features <strong style={{ color: "var(--text-primary)" }}>will</strong> change or disappear without notice.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Data loss is possible.</strong> While we back up the server daily, we make no guarantee that your data will survive any particular incident &mdash; infrastructure failure, migration error, bug, or anything else.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>There is no uptime guarantee.</strong> The service may be unavailable at any time, for any duration, without notice.</li>
          <li style={text}>There is currently <strong style={{ color: "var(--text-primary)" }}>no account deletion feature.</strong> If you need your account deleted, contact us at info@loore.org and we'll handle it manually.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>No SLA, no support guarantees, no refunds.</strong> This is alpha.</li>
        </ul>
        <p style={text}>
          By using Loore, you accept that you are doing so <strong style={{ color: "var(--text-primary)" }}>entirely at your own risk.</strong> The developers assume no responsibility for any data loss, damages, or other problems that may occur.
        </p>

        {/* Section 3 */}
        <p style={sectionTitle}><strong>3. Your Content Is Yours</strong></p>
        <p style={text}>You retain full ownership of everything you create on Loore.</p>
        <p style={text}>
          We claim <strong style={{ color: "var(--text-primary)" }}>no ownership and no license</strong> over your content except for what is minimally necessary to operate the service: storing it, encrypting it, displaying it back to you, and processing it with AI if and only if you have opted into that.
        </p>
        <p style={text}>
          <strong style={{ color: "var(--text-primary)" }}>One exception &mdash; AI training opt-in:</strong> If you enable the "AI training" setting on specific content (see Section 6), you grant both the relevant AI providers <strong style={{ color: "var(--text-primary)" }}>and Loore</strong> a license to use that content for training machine learning models. More details in Section 6.
        </p>

        {/* Section 4 */}
        <p style={sectionTitle}><strong>4. Privacy &amp; How Your Data Is Handled</strong></p>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>What's Private by Default</p>
        <p style={text}>When you create content on Loore, it is <strong style={{ color: "var(--text-primary)" }}>private by default:</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Visibility:</strong> Only you can see it.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>AI access:</strong> No AI can read it.</li>
        </ul>
        <p style={text}>You control both of these settings per piece of content, and you can change them at any time.</p>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>Privacy Controls</p>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>Who can see your content (visibility):</strong></p>
        <ul style={{ paddingLeft: "1.2rem", listStyle: "none" }}>
          <li style={text}>Private (default) &mdash; Only you</li>
          <li style={text}>Circles &mdash; Shared with specific groups <em>(coming soon &mdash; not yet functional)</em></li>
          <li style={text}>Public &mdash; Anyone can see it</li>
        </ul>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>How AI can use your content:</strong></p>
        <ul style={{ paddingLeft: "1.2rem", listStyle: "none" }}>
          <li style={text}>None (default) &mdash; No AI access whatsoever</li>
          <li style={text}>Chat &mdash; AI can read this content to respond to you (not used for training &mdash; see Section 5)</li>
          <li style={text}>Train &mdash; AI providers and Loore may use this content for model training (see Section 6)</li>
        </ul>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>Encryption</p>
        <p style={text}>
          All user content &mdash; text, audio files, transcripts, and profiles &mdash; is <strong style={{ color: "var(--text-primary)" }}>encrypted at rest</strong> using AES-256-GCM with encryption keys managed by Google Cloud KMS (envelope encryption).
        </p>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>What this means in practice:</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>Your data is encrypted on disk and in the database.</li>
          <li style={text}>It must be <strong style={{ color: "var(--text-primary)" }}>decrypted server-side</strong> whenever it needs to be processed &mdash; for displaying it to you, for AI interactions, or for search.</li>
          <li style={text}>The development team has server access and <strong style={{ color: "var(--text-primary)" }}>can technically decrypt your data.</strong> We commit to not doing so except for responding to legal requirements, or with your explicit permission. Every decryption event is logged and auditable.</li>
          <li style={text}>This is a <strong style={{ color: "var(--text-primary)" }}>trust-based model.</strong> We are being transparent about what we can and cannot guarantee, rather than making cryptographic promises we can't back up.</li>
        </ul>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>What is NOT encrypted:</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Semantic embeddings</strong> &mdash; abstract mathematical representations of your content &mdash; are stored unencrypted, isolated by your user ID. These enable search and AI features. They contain semantic information about your content but not the content itself.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Metadata</strong> such as timestamps, content relationships, and privacy settings is not encrypted.</li>
        </ul>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>Who Else Touches Your Data</p>
        <p style={text}>Loore uses the following third-party services that may process your data:</p>
        <table style={{ width: "100%", borderCollapse: "collapse", marginTop: "0.5rem", marginBottom: "0.5rem" }}>
          <thead>
            <tr>
              <th style={{ ...text, textAlign: "left", borderBottom: "1px solid var(--border)", padding: "8px", fontWeight: 400 }}>Service</th>
              <th style={{ ...text, textAlign: "left", borderBottom: "1px solid var(--border)", padding: "8px", fontWeight: 400 }}>What For</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}><strong style={{ color: "var(--text-primary)" }}>Google Cloud Platform (GCP)</strong></td>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}>Server hosting and encryption key management (KMS)</td>
            </tr>
            <tr>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}><strong style={{ color: "var(--text-primary)" }}>OpenAI</strong></td>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}>AI chat, audio transcription, text-to-speech, and (if you opt in) model training</td>
            </tr>
            <tr>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}><strong style={{ color: "var(--text-primary)" }}>Anthropic</strong></td>
              <td style={{ ...text, padding: "8px", borderBottom: "1px solid var(--border)" }}>AI chat</td>
            </tr>
            <tr>
              <td style={{ ...text, padding: "8px" }}><strong style={{ color: "var(--text-primary)" }}>Email provider (SMTP)</strong></td>
              <td style={{ ...text, padding: "8px" }}>Sending login magic links</td>
            </tr>
          </tbody>
        </table>
        <p style={text}>No other third-party services have access to your user content in the deployed application.</p>

        {/* Section 5 */}
        <p style={sectionTitle}><strong>5. AI Chat Mode</strong></p>
        <p style={text}>When you set content to <strong style={{ color: "var(--text-primary)" }}>"Chat"</strong> AI usage:</p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>That content may be sent to OpenAI or Anthropic's servers so the AI can read it and respond to you.</li>
          <li style={text}>Per these providers' API terms of service, content sent through the API is <strong style={{ color: "var(--text-primary)" }}>not used for training</strong> their models.</li>
          <li style={text}>We use separate API keys for chat vs. training to enforce this separation.</li>
          <li style={text}>If you later change the setting to "None," future AI interactions will no longer include that content &mdash; but we cannot recall data already processed in past chat sessions.</li>
        </ul>

        {/* Section 6 */}
        <p style={sectionTitle}><strong>6. AI Training Mode &mdash; Read This Carefully</strong></p>
        <p style={text}>When you set content to <strong style={{ color: "var(--text-primary)" }}>"Train"</strong> AI usage, you are making a significant choice:</p>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>What Happens</p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>Your content will be submitted to AI providers (currently OpenAI) for potential use in <strong style={{ color: "var(--text-primary)" }}>training future AI models.</strong></li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Loore</strong> may also use this content to train its own models in the future.</li>
          <li style={text}>In exchange, OpenAI provides free daily API credits that help keep Loore running.</li>
        </ul>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>What You Must Understand</p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>For models that have already been trained on your data, this is irrevocable.</strong> You can withdraw consent going forward, and your data will not be included in future training runs &mdash; but it cannot be removed from models that have already learned from it.</li>
          <li style={text}>If you change the setting back to "Chat" or "None," future training will stop, but past training cannot be undone.</li>
          <li style={text}>You <strong style={{ color: "var(--text-primary)" }}>must have the legal rights</strong> to any content you mark for training. If it's your original writing, you're fine. If it contains someone else's copyrighted work, song lyrics, or other material you don't have rights to &mdash; <strong style={{ color: "var(--text-primary)" }}>do not enable training on it.</strong> You bear full responsibility for this.</li>
        </ul>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>The License You Grant</p>
        <p style={text}>By enabling "Train" on any content, you grant:</p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>To the relevant AI providers</strong> (currently OpenAI): a license to use that content for development and improvement of their services, including model training, research, evaluation, and testing.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>To Loore:</strong> a license to use that content for training machine learning models, research, and development of the Loore service and related products.</li>
        </ul>
        <p style={text}>These licenses are:</p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Non-exclusive</strong> &mdash; you can do whatever else you want with your content.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Royalty-free</strong> &mdash; no payment is owed by either party.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Revocable for future use</strong> &mdash; you can turn off training and your content will not be included in future training runs.</li>
          <li style={text}><strong style={{ color: "var(--text-primary)" }}>Irrevocable for past training</strong> &mdash; content already used to train a model cannot be extracted from that model.</li>
        </ul>

        <p style={{ ...text, fontWeight: 400, marginTop: "1rem" }}>OpenAI Content Sharing Agreement</p>
        <p style={text}>By using Loore's AI training features, you also agree to OpenAI's Content Sharing Agreement:</p>
        <div style={highlight}>
          <p style={text}>
            This Content Sharing Agreement is between OpenAI, L.L.C. ("us" or "we") and you ("Customer"). This Content Sharing Agreement is incorporated into the terms located at openai.com unless the parties have negotiated a separate agreement for the Services, in which case such agreement will govern (the "Business Terms"). Capitalized terms not defined here are defined in the Business Terms or the Data Processing Agreement between the parties in connection with the Services (the "DPA"). This Content Sharing Agreement takes precedence in the event of any conflict.
          </p>
          <p style={text}>
            Notwithstanding anything set forth in the Business Terms, we may use Customer Content to develop and improve the Services, including for training our models and other research, development, evaluation, and testing purposes ("Development Purposes"). You expressly agree that use of Customer Data for the Development Purposes is not subject to the provisions of the DPA. OpenAI will process Customer Data for Development Purposes as an independent Data Controller. You are responsible for all Input provided by you and your End Users.
          </p>
          <p style={text}>
            You also represent and warrant that you have the rights, licenses, and permissions necessary &ndash; including as applicable that you have provided any notice to End Users, and collected any relevant consent from End Users ("Notice") &ndash; to provide the Input to the Services for the Development Purposes. You agree that you and your End Users will not provide any information as Input to the Services that you or your End Users do not want to be used for Development Purposes, such as sensitive, confidential, or proprietary information. You also agree that you will not use the Services to process (a) any data that includes or constitutes "Protected Health Information," as defined under the HIPAA Privacy Rule (45 C.F.R. Section 160.103), or (b) any Personal Data of children under 13 or the applicable age of digital consent. You also agree that you will provide OpenAI a copy of your Notice upon OpenAI's request.
          </p>
        </div>
        <p style={text}>
          The full agreement is incorporated into and governed by OpenAI's Business Terms at{" "}
          <a href="https://openai.com" target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>openai.com</a>.
        </p>

        {/* Section 7 */}
        <p style={sectionTitle}><strong>7. What You Agree Not to Do</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>Do not submit <strong style={{ color: "var(--text-primary)" }}>Protected Health Information</strong> (as defined under HIPAA) or <strong style={{ color: "var(--text-primary)" }}>personal data of children</strong> under 13 or the applicable age of digital consent.</li>
          <li style={text}>Do not mark content for AI training unless you hold the necessary rights.</li>
          <li style={text}>Do not use Loore for any illegal purpose.</li>
          <li style={text}>Do not attempt to access other users' private content.</li>
        </ul>

        {/* Section 8 */}
        <p style={sectionTitle}><strong>8. Liability</strong></p>
        <p style={text}><strong style={{ color: "var(--text-primary)" }}>To the maximum extent permitted by applicable law:</strong></p>
        <ul style={{ paddingLeft: "1.2rem" }}>
          <li style={text}>Loore and its developers are not liable for any damages arising from your use of the service &mdash; including but not limited to data loss, service interruptions, security incidents, or any consequences of content being used for AI training.</li>
          <li style={text}>This is alpha software. See Section 2.</li>
        </ul>

        {/* Section 9 */}
        <p style={sectionTitle}><strong>9. Changes to These Terms</strong></p>
        <p style={text}>
          We may update these terms as Loore evolves. When we make significant changes, we will notify you (through the app or by email) and ask you to re-accept. Continued use after notification constitutes acceptance.
        </p>

        {/* Section 10 */}
        <p style={sectionTitle}><strong>10. Contact</strong></p>
        <p style={text}>
          For any questions, concerns, or account-related requests: <a href="mailto:info@loore.org" style={{ color: "var(--accent)" }}>info@loore.org</a>
        </p>

        <hr style={{ borderColor: "var(--border)", margin: "1.5rem 0" }} />

        <p style={{ ...text, fontSize: "0.85rem" }}>
          <em>Terms Version: 2.0 &mdash; Last updated: February 9, 2026</em>
        </p>

        <p style={text}>
          By clicking "I Agree", you confirm that you have read and understand these terms and that you consent to the terms described above.
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
