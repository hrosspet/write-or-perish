import React, { useRef } from "react";
import { Link } from "react-router-dom";
import Fade from "../utils/Fade";
import useOnScreen from "../utils/useOnScreen";

function FeatureStep({ number, title, accent, body, detail, soon }) {
  return (
    <div style={{ maxWidth: 640, margin: "0 auto", padding: "5rem 0" }}>
      <Fade>
        <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "1.8rem" }}>
          <span style={{
            fontFamily: "var(--serif)", fontSize: "3rem", fontWeight: 300,
            color: "var(--accent)", opacity: 0.3, lineHeight: 1,
          }}>{number}</span>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.3 }} />
          {soon && (
            <span style={{
              fontFamily: "var(--sans)", fontSize: "0.62rem", fontWeight: 400,
              letterSpacing: "0.15em", textTransform: "uppercase",
              color: "var(--accent)", opacity: 0.6,
              border: "1px solid var(--accent-glow)",
              padding: "3px 10px", borderRadius: 4,
            }}>Coming soon</span>
          )}
        </div>
      </Fade>
      <Fade delay={0.08}>
        <h2 style={{
          fontFamily: "var(--serif)", fontWeight: 300,
          fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.3,
          color: "var(--text-primary)", marginBottom: "1.2rem",
        }}>
          {title} <em style={{ fontStyle: "italic", color: "var(--accent)" }}>{accent}</em>
        </h2>
      </Fade>
      <Fade delay={0.16}>
        <p style={{
          fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
          lineHeight: 1.85, color: "var(--text-secondary)", marginBottom: "1.5rem",
        }}>{body}</p>
      </Fade>
      {detail && (
        <Fade delay={0.24}>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 10, padding: "1.4rem 1.6rem",
          }}>
            <div style={{
              fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.15em",
              textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
              marginBottom: "0.7rem",
            }}>How it works</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
              lineHeight: 1.8, color: "var(--text-muted)",
            }}>{detail}</p>
          </div>
        </Fade>
      )}
    </div>
  );
}

function FlywheelDiagram() {
  const ref = useRef(null);
  const visible = useOnScreen(ref, 0.1);
  const items = [
    { label: "Journal", icon: "\u2726", angle: 270 },
    { label: "Reflect", icon: "\u25C8", angle: 0 },
    { label: "Share", icon: "\u25C7", angle: 90 },
    { label: "Connect", icon: "\u2B21", angle: 180 },
  ];
  return (
    <div ref={ref} style={{
      display: "flex", justifyContent: "center", padding: "3rem 0",
      opacity: visible ? 1 : 0, transition: "opacity 1.2s ease 0.2s",
    }}>
      <div style={{ position: "relative", width: 260, height: 260 }}>
        {/* Orbit ring */}
        <div style={{
          position: "absolute", inset: 20,
          border: "1px solid var(--border)", borderRadius: "50%",
          opacity: visible ? 1 : 0, transition: "opacity 1s ease 0.4s",
        }} />
        {/* Center glow */}
        <div style={{
          position: "absolute", top: "50%", left: "50%",
          transform: "translate(-50%, -50%)",
          width: 60, height: 60, borderRadius: "50%",
          background: "radial-gradient(circle, var(--accent-glow) 0%, transparent 70%)",
          opacity: visible ? 1 : 0, transition: "opacity 1s ease 0.6s",
        }} />
        <div style={{
          position: "absolute", top: "50%", left: "50%",
          transform: "translate(-50%, -50%)",
          fontFamily: "var(--serif)", fontSize: "0.65rem", letterSpacing: "0.2em",
          textTransform: "uppercase", color: "var(--text-muted)",
          opacity: visible ? 1 : 0, transition: "opacity 1s ease 0.8s",
        }}>your lore</div>
        {/* Nodes */}
        {items.map((item, i) => {
          const r = 110;
          const rad = (item.angle * Math.PI) / 180;
          const x = 130 + r * Math.cos(rad);
          const y = 130 + r * Math.sin(rad);
          return (
            <div key={i} style={{
              position: "absolute", left: x, top: y,
              transform: "translate(-50%, -50%)",
              textAlign: "center",
              opacity: visible ? 1 : 0,
              transition: `opacity 0.8s ease ${0.5 + i * 0.15}s, transform 0.8s ease ${0.5 + i * 0.15}s`,
            }}>
              <div style={{
                fontSize: "1.2rem", color: "var(--accent)", marginBottom: 4,
                filter: "drop-shadow(0 0 8px var(--accent-glow))",
              }}>{item.icon}</div>
              <div style={{
                fontFamily: "var(--sans)", fontSize: "0.7rem", fontWeight: 400,
                letterSpacing: "0.08em", color: "var(--text-secondary)",
              }}>{item.label}</div>
            </div>
          );
        })}
        {/* Arrows between nodes */}
        <svg style={{
          position: "absolute", inset: 0, width: 260, height: 260,
          opacity: visible ? 0.25 : 0, transition: "opacity 1s ease 0.8s",
        }} viewBox="0 0 260 260">
          <defs>
            <marker id="arrowhead" markerWidth="6" markerHeight="4" refX="5" refY="2" orient="auto">
              <polygon points="0 0, 6 2, 0 4" fill="var(--accent)" />
            </marker>
          </defs>
          <path d="M 170 35 A 110 110 0 0 1 240 100" fill="none" stroke="var(--accent)" strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 240 160 A 110 110 0 0 1 170 225" fill="none" stroke="var(--accent)" strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 90 225 A 110 110 0 0 1 20 160" fill="none" stroke="var(--accent)" strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 20 100 A 110 110 0 0 1 90 35" fill="none" stroke="var(--accent)" strokeWidth="1" markerEnd="url(#arrowhead)" />
        </svg>
      </div>
    </div>
  );
}

export default function VisionPage() {
  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Hero */}
      <div style={{
        minHeight: "70vh", display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", textAlign: "center",
        padding: "4rem 0 2rem",
      }}>
        <Fade>
          <div style={{
            fontFamily: "var(--sans)", fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "2.5rem",
          }}>The Vision</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.8rem)", lineHeight: 1.15,
            color: "var(--text-primary)", maxWidth: 720, marginBottom: "1.2rem",
          }}>
            From private reflection to<br />
            <em style={{ fontStyle: "italic", color: "var(--accent)" }}>effortless connection</em>
          </h1>
        </Fade>
        <Fade delay={0.2}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "clamp(0.95rem, 2vw, 1.1rem)",
            color: "var(--text-secondary)", maxWidth: 520, lineHeight: 1.75,
          }}>
            Loore is a complete ecosystem for self-authorship. It begins with a journal
            and grows into something much larger — a living cycle of reflection, insight,
            sharing, and meaningful connection.
          </p>
        </Fade>
      </div>

      {/* Flywheel */}
      <FlywheelDiagram />

      {/* The four features */}
      <FeatureStep
        number="01"
        title="Effortless"
        accent="journaling."
        body="Capture your thoughts the moment they arrive — by typing, by voice, or by importing what you've already written. Loore transcribes, organizes, and preserves everything in a branching tree of conversations you can explore, fork, and revisit. No friction. No lost threads."
        detail="Voice notes are transcribed instantly. Conversations branch naturally — follow one thread of thought, then return to explore another. Import your existing journals, Obsidian notes, even tweets. Your lore begins wherever you already are."
      />
      <FeatureStep
        number="02"
        title="AI that helps you"
        accent="see yourself."
        body="This isn't a chatbot that answers questions. It's a mirror that gets sharper over time. As your lore grows, Loore's reflections become deeply personal — surfacing patterns you couldn't see, naming what was vague, connecting threads across weeks and months of your life."
        detail="Loore builds a living understanding of who you are — your values, patterns, blind spots, recurring themes. Every reflection draws on this growing understanding. You can use multiple AI models within the same conversation and choose the voice that resonates."
      />
      <FeatureStep
        number="03"
        soon
        title="Your lore becomes an"
        accent="offering."
        body="As you clarify who you are, sharing becomes natural — not performance, but offering. Loore helps you transform private insights into something others can receive: a tweet, an essay, an update to people who matter. You choose what, where, and to whom. Nothing leaves without your say."
        detail="A journal entry about burnout might contain a universal insight worth sharing publicly, a personal update for close friends, and a need you'd like to express. Loore sees all three and helps you route each piece to the right audience in the right format."
      />
      <FeatureStep
        number="04"
        soon
        title="Find your"
        accent="people."
        body="Express what you need, what you're exploring, what you can offer — and let Loore connect you with people whose intentions complement yours. Not networking. Not algorithms optimizing for engagement. Real connection rooted in what you actually care about."
        detail="The Intention Market matches complementary needs and offerings: someone exploring consciousness finds a thinking partner; someone navigating burnout connects with someone who's been through it. Serendipity through authenticity."
      />

      {/* Thesis */}
      <div style={{ maxWidth: 600, margin: "0 auto", padding: "4rem 0 5rem", textAlign: "center" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.4, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.1}>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.3rem, 3vw, 1.8rem)",
            lineHeight: 1.45, color: "var(--text-secondary)", marginBottom: "1rem",
          }}>
            Current systems for organizing humans are
            inherently misaligned — low bandwidth, limited on reflection,
            communication, and emotional processing.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 400,
            fontSize: "clamp(1.3rem, 3vw, 1.8rem)",
            lineHeight: 1.45, color: "var(--text-primary)",
          }}>
            Loore is infrastructure for something better.
          </p>
        </Fade>
        <Fade delay={0.26}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1rem",
            lineHeight: 1.8, color: "var(--text-muted)", maxWidth: 480,
            margin: "2rem auto 2.5rem",
          }}>
            Empower individuals to awaken themselves — as part of humanity
            awakening to itself. That's the direction. Journaling is where it starts.
          </p>
        </Fade>
        <Fade delay={0.32}>
          <Link to="/login?returnUrl=%2F" style={{
            display: "inline-flex", alignItems: "center", gap: "0.6rem",
            fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.95rem",
            letterSpacing: "0.06em", padding: "14px 36px",
            border: "1px solid var(--accent)", background: "transparent",
            color: "var(--accent)", textDecoration: "none", cursor: "pointer",
          }}>
            <span>Join the Alpha</span>
            <span style={{ fontSize: "1.1rem" }}>&rarr;</span>
          </Link>
        </Fade>
      </div>
    </div>
  );
}
