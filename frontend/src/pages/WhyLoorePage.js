import React from "react";
import { Link } from "react-router-dom";
import Fade from "../utils/Fade";

export default function WhyLoorePage() {
  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Hero */}
      <div style={{
        minHeight: "60vh", display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        textAlign: "center", padding: "4rem 0 2rem",
      }}>
        <Fade>
          <div style={{
            fontFamily: "var(--sans)", fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "2.5rem",
          }}>Why Loore</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.4rem)", lineHeight: 1.2,
            color: "var(--text-primary)", maxWidth: 650, marginBottom: "1.2rem",
          }}>
            A place to <em style={{ fontStyle: "italic", color: "var(--accent)" }}>become yourself</em>.
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 520,
          }}>
            AI is powerful. Loore puts that power in service of something
            personal — understanding who you are and authoring who you're becoming.
          </p>
        </Fade>
      </div>

      {/* The core idea */}
      <div style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 0 4rem" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: "var(--text-primary)", marginBottom: "1.5rem",
          }}>
            AI for <em style={{ fontStyle: "italic", color: "var(--accent)" }}>self-understanding</em>
          </h2>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: "var(--text-secondary)", marginBottom: "1.5rem",
          }}>
            Most AI apps are designed to answer questions. Loore is designed to help
            you understand yourself. Everything you write contributes to a deepening
            picture — your patterns, your values, your blind spots. The AI remembers
            not just to serve better answers, but to help you see more clearly.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: "var(--text-secondary)",
          }}>
            Your lore compounds. And so does the quality of reflection you get back.
          </p>
        </Fade>
      </div>

      {/* What's different */}
      <div style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 0 4rem" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: "var(--text-primary)", marginBottom: "1.5rem",
          }}>
            What Loore <em style={{ fontStyle: "italic", color: "var(--accent)" }}>gives you</em>
          </h2>
        </Fade>

        <Fade delay={0.12}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.95rem",
              color: "var(--text-primary)", marginBottom: "0.6rem",
            }}>Conversations that compound</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: "var(--text-muted)",
            }}>
              Everything you write contributes to a living understanding of who you
              are — your values, patterns, blind spots, recurring themes. AI reflection
              gets more personal and useful the longer you use it. Your lore isn't
              just stored — it's woven into every interaction.
            </p>
          </div>
        </Fade>

        <Fade delay={0.16}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.95rem",
              color: "var(--text-primary)", marginBottom: "0.6rem",
            }}>Context you can manage</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: "var(--text-muted)",
            }}>
              Loore gives you complete control over what the AI sees. Branch
              conversations, link threads together, quote across contexts, share
              your personal profile for instant depth. You start your day with a
              private journal — later that day, parts of it might inform a
              conversation with your lawyer, a to-do prioritization, or a
              reflection session. Each with exactly the right context.
            </p>
          </div>
        </Fade>

        <Fade delay={0.2}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.95rem",
              color: "var(--text-primary)", marginBottom: "0.6rem",
            }}>Choose your intelligence</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: "var(--text-muted)",
            }}>
              Use Claude, GPT, or other models within the same conversation.
              Branch a response to try a different model on the same prompt.
              Compare perspectives and choose the voice that resonates — all
              without switching apps or re-explaining context.
            </p>
          </div>
        </Fade>

        <Fade delay={0.24}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.95rem",
              color: "var(--text-primary)", marginBottom: "0.6rem",
            }}>Capture thinking, not just typing</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: "var(--text-muted)",
            }}>
              Voice notes are transcribed instantly into searchable, editable text.
              Your best thinking often happens away from the keyboard — on a walk,
              in the shower, lying in bed. Loore meets you where your mind already is.
            </p>
          </div>
        </Fade>
      </div>

      {/* The deeper reason */}
      <div style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 0 3rem" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: "var(--text-primary)", marginBottom: "1.5rem",
          }}>
            The <em style={{ fontStyle: "italic", color: "var(--accent)" }}>bigger picture</em>
          </h2>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: "var(--text-secondary)", marginBottom: "1.5rem",
          }}>
            AI is rapidly gaining agency. Loore is designed to help <em style={{ fontStyle: "italic",
            color: "var(--text-primary)" }}>you</em> gain yours — become more aware of your own
            patterns, more intentional about your choices, more articulate about who
            you are and what you want.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: "var(--text-secondary)",
          }}>
            An AI partner in self-authorship.
            Loore helps you find your own answers — and remember them.
          </p>
        </Fade>
      </div>

      {/* CTA */}
      <div style={{
        maxWidth: 540, margin: "0 auto", padding: "3rem 0 5rem",
        textAlign: "center",
      }}>
        <Fade>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 300, fontStyle: "italic",
            fontSize: "clamp(1.2rem, 2.5vw, 1.55rem)",
            lineHeight: 1.5, color: "var(--text-secondary)", marginBottom: "2.5rem",
          }}>
            And this is just the Alpha. There's so much more to come.
          </p>
        </Fade>
        <Fade delay={0.1}>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "1.5rem" }}>
            <Link to="/login?returnUrl=%2F" style={{
              display: "inline-flex", alignItems: "center", gap: "0.6rem",
              fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.92rem",
              letterSpacing: "0.05em", padding: "12px 30px",
              border: "1px solid var(--accent)", background: "transparent",
              color: "var(--accent)", textDecoration: "none", cursor: "pointer",
            }}>
              <span>Join the Alpha</span>
              <span style={{ fontSize: "1rem" }}>&rarr;</span>
            </Link>
            <Link to="/vision" style={{
              display: "inline-flex", alignItems: "center", gap: "0.6rem",
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
              padding: "12px 0", color: "var(--text-muted)", textDecoration: "none",
              borderBottom: "1px solid var(--border)",
            }}>
              Read the full vision &rarr;
            </Link>
          </div>
        </Fade>
      </div>
    </div>
  );
}
