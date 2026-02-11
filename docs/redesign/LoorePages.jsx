import React, { useState, useEffect, useRef } from "react";

/* ─── Intersection-observer fade-in ─── */
function useOnScreen(ref, threshold = 0.12) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) setVisible(true); },
      { threshold }
    );
    const el = ref.current;
    if (el) observer.observe(el);
    return () => { if (el) observer.unobserve(el); };
  }, [ref, threshold]);
  return visible;
}

function Fade({ children, delay = 0, className = "", style = {} }) {
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

/* ─── Shared Design Tokens ─── */
const T = {
  bgDeep: "#0e0d0b",
  bgSurface: "#151412",
  bgCard: "#1a1917",
  bgCardHover: "#211f1b",
  textPrimary: "#e8e2d6",
  textSecondary: "#9e9688",
  textMuted: "#6b655b",
  accent: "#c4956a",
  accentDim: "#a07a55",
  accentGlow: "#c4956a33",
  accentSubtle: "#c4956a18",
  border: "#252320",
  borderHover: "#353128",
  serif: "'Cormorant Garamond', Georgia, serif",
  sans: "'Outfit', system-ui, sans-serif",
};

/* ═══════════════════════════════════════════
   PAGE 1: VISION
   ═══════════════════════════════════════════ */

function FeatureStep({ number, title, accent, body, detail, soon }) {
  return (
    <div style={{ maxWidth: 640, margin: "0 auto", padding: "5rem 0" }}>
      <Fade>
        <div style={{ display: "flex", alignItems: "center", gap: "1rem", marginBottom: "1.8rem" }}>
          <span style={{
            fontFamily: T.serif, fontSize: "3rem", fontWeight: 300,
            color: T.accent, opacity: 0.3, lineHeight: 1,
          }}>{number}</span>
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.3 }} />
          {soon && (
            <span style={{
              fontFamily: T.sans, fontSize: "0.62rem", fontWeight: 400,
              letterSpacing: "0.15em", textTransform: "uppercase",
              color: T.accent, opacity: 0.6,
              border: `1px solid ${T.accentGlow}`,
              padding: "3px 10px", borderRadius: 4,
            }}>Coming soon</span>
          )}
        </div>
      </Fade>
      <Fade delay={0.08}>
        <h2 style={{
          fontFamily: T.serif, fontWeight: 300,
          fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.3,
          color: T.textPrimary, marginBottom: "1.2rem",
        }}>
          {title} <em style={{ fontStyle: "italic", color: T.accent }}>{accent}</em>
        </h2>
      </Fade>
      <Fade delay={0.16}>
        <p style={{
          fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
          lineHeight: 1.85, color: T.textSecondary, marginBottom: "1.5rem",
        }}>{body}</p>
      </Fade>
      {detail && (
        <Fade delay={0.24}>
          <div style={{
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 10, padding: "1.4rem 1.6rem",
          }}>
            <div style={{
              fontFamily: T.sans, fontSize: "0.7rem", letterSpacing: "0.15em",
              textTransform: "uppercase", color: T.accent, opacity: 0.7,
              marginBottom: "0.7rem",
            }}>How it works</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
              lineHeight: 1.8, color: T.textMuted,
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
    { label: "Journal", icon: "✦", angle: 270 },
    { label: "Reflect", icon: "◈", angle: 0 },
    { label: "Share", icon: "◇", angle: 90 },
    { label: "Connect", icon: "⬡", angle: 180 },
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
          border: `1px solid ${T.border}`, borderRadius: "50%",
          opacity: visible ? 1 : 0, transition: "opacity 1s ease 0.4s",
        }} />
        {/* Center glow */}
        <div style={{
          position: "absolute", top: "50%", left: "50%",
          transform: "translate(-50%, -50%)",
          width: 60, height: 60, borderRadius: "50%",
          background: `radial-gradient(circle, ${T.accentGlow} 0%, transparent 70%)`,
          opacity: visible ? 1 : 0, transition: "opacity 1s ease 0.6s",
        }} />
        <div style={{
          position: "absolute", top: "50%", left: "50%",
          transform: "translate(-50%, -50%)",
          fontFamily: T.serif, fontSize: "0.65rem", letterSpacing: "0.2em",
          textTransform: "uppercase", color: T.textMuted,
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
                fontSize: "1.2rem", color: T.accent, marginBottom: 4,
                filter: `drop-shadow(0 0 8px ${T.accentGlow})`,
              }}>{item.icon}</div>
              <div style={{
                fontFamily: T.sans, fontSize: "0.7rem", fontWeight: 400,
                letterSpacing: "0.08em", color: T.textSecondary,
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
              <polygon points="0 0, 6 2, 0 4" fill={T.accent} />
            </marker>
          </defs>
          {/* Curved arrows following the circle */}
          <path d="M 170 35 A 110 110 0 0 1 240 100" fill="none" stroke={T.accent} strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 240 160 A 110 110 0 0 1 170 225" fill="none" stroke={T.accent} strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 90 225 A 110 110 0 0 1 20 160" fill="none" stroke={T.accent} strokeWidth="1" markerEnd="url(#arrowhead)" />
          <path d="M 20 100 A 110 110 0 0 1 90 35" fill="none" stroke={T.accent} strokeWidth="1" markerEnd="url(#arrowhead)" />
        </svg>
      </div>
    </div>
  );
}

function VisionPage() {
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
            fontFamily: T.sans, fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: T.textMuted, marginBottom: "2.5rem",
          }}>The Vision</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.8rem)", lineHeight: 1.15,
            color: T.textPrimary, maxWidth: 720, marginBottom: "1.2rem",
          }}>
            From private reflection to<br />
            <em style={{ fontStyle: "italic", color: T.accent }}>effortless connection</em>
          </h1>
        </Fade>
        <Fade delay={0.2}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "clamp(0.95rem, 2vw, 1.1rem)",
            color: T.textSecondary, maxWidth: 520, lineHeight: 1.75,
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
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.4, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.1}>
          <p style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.3rem, 3vw, 1.8rem)",
            lineHeight: 1.45, color: T.textSecondary, marginBottom: "1rem",
          }}>
            Current systems for organizing humans are
            inherently misaligned — low bandwidth, limited on reflection,
            communication, and emotional processing.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.serif, fontWeight: 400,
            fontSize: "clamp(1.3rem, 3vw, 1.8rem)",
            lineHeight: 1.45, color: T.textPrimary,
          }}>
            Loore is infrastructure for something better.
          </p>
        </Fade>
        <Fade delay={0.26}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1rem",
            lineHeight: 1.8, color: T.textMuted, maxWidth: 480,
            margin: "2rem auto 2.5rem",
          }}>
            Empower individuals to awaken themselves — as part of humanity
            awakening to itself. That's the direction. Journaling is where it starts.
          </p>
        </Fade>
        <Fade delay={0.32}>
          <a href="/login?returnUrl=%2F" style={{
            display: "inline-flex", alignItems: "center", gap: "0.6rem",
            fontFamily: T.sans, fontWeight: 400, fontSize: "0.95rem",
            letterSpacing: "0.06em", padding: "14px 36px",
            border: `1px solid ${T.accent}`, background: "transparent",
            color: T.accent, textDecoration: "none", cursor: "pointer",
          }}>
            <span>Join the Alpha</span>
            <span style={{ fontSize: "1.1rem" }}>→</span>
          </a>
        </Fade>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   PAGE 2: ALPHA THANK YOU
   ═══════════════════════════════════════════ */

function AlphaThankYou() {
  return (
    <div style={{
      minHeight: "100vh", display: "flex", flexDirection: "column",
      alignItems: "center", justifyContent: "center",
      padding: "4rem 2rem", textAlign: "center",
    }}>
      {/* Warm glow */}
      <div style={{
        position: "absolute", top: "30%", left: "50%",
        transform: "translate(-50%, -50%)",
        width: 500, height: 500, borderRadius: "50%",
        background: `radial-gradient(circle, ${T.accentGlow} 0%, transparent 65%)`,
        pointerEvents: "none", opacity: 0.4,
      }} />

      <Fade>
        <div style={{
          fontFamily: T.serif, fontSize: "2.5rem", color: T.accent,
          marginBottom: "2rem", opacity: 0.6, lineHeight: 1,
        }}>✦</div>
      </Fade>
      <Fade delay={0.1}>
        <h1 style={{
          fontFamily: T.serif, fontWeight: 300,
          fontSize: "clamp(2rem, 5vw, 3.2rem)", lineHeight: 1.2,
          color: T.textPrimary, maxWidth: 600, marginBottom: "1.5rem",
        }}>
          You're part of this now.
        </h1>
      </Fade>
      <Fade delay={0.2}>
        <p style={{
          fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
          lineHeight: 1.8, color: T.textSecondary, maxWidth: 480,
          marginBottom: "3rem",
        }}>
          Thank you for signing up for the Loore Alpha. We're letting people in
          gradually — to keep things intimate and to give each person the attention
          they deserve as they begin.
        </p>
      </Fade>

      <Fade delay={0.28}>
        <div style={{
          background: T.bgCard, border: `1px solid ${T.border}`,
          borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
          textAlign: "left", marginBottom: "3rem",
        }}>
          <div style={{
            fontFamily: T.sans, fontSize: "0.7rem", letterSpacing: "0.15em",
            textTransform: "uppercase", color: T.accent, opacity: 0.7,
            marginBottom: "1.2rem",
          }}>What happens next</div>

          <div style={{ display: "flex", flexDirection: "column", gap: "1.2rem" }}>
            {[
              { num: "01", text: "We'll send you an email when your spot opens up. It shouldn't be long." },
              { num: "02", text: "You'll get a warm welcome with everything you need to begin." },
              { num: "03", text: "Start journaling — by text or voice — and let your lore unfold." },
            ].map(({ num, text }) => (
              <div key={num} style={{ display: "flex", gap: "1rem", alignItems: "flex-start" }}>
                <span style={{
                  fontFamily: T.serif, fontSize: "1.1rem", fontWeight: 300,
                  color: T.accent, opacity: 0.5, flexShrink: 0, marginTop: 2,
                }}>{num}</span>
                <p style={{
                  fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
                  lineHeight: 1.7, color: T.textSecondary,
                }}>{text}</p>
              </div>
            ))}
          </div>
        </div>
      </Fade>

      <Fade delay={0.36}>
        <p style={{
          fontFamily: T.serif, fontWeight: 300, fontStyle: "italic",
          fontSize: "1.2rem", lineHeight: 1.5, color: T.textMuted,
          maxWidth: 420, marginBottom: "2.5rem",
        }}>
          In the meantime — you might notice yourself already paying closer attention
          to the story you're living. That's the process beginning.
        </p>
      </Fade>

      <Fade delay={0.42}>
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", gap: "1rem",
        }}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
            color: T.textSecondary, marginBottom: "0.3rem",
          }}>
            You can learn more about where Loore is headed.
          </p>
          <a href="/vision" style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.88rem",
            color: T.accent, textDecoration: "none",
            borderBottom: `1px solid ${T.accentGlow}`,
            paddingBottom: 2,
          }}>
            Read the vision →
          </a>
        </div>
      </Fade>
    </div>
  );
}

/* ═══════════════════════════════════════════
   PAGE 3: WELCOME (for new Alpha users)
   ═══════════════════════════════════════════ */

function WelcomePage() {
  const [promptHovered, setPromptHovered] = useState(false);

  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Hero welcome */}
      <div style={{
        minHeight: "55vh", display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        textAlign: "center", padding: "4rem 0 2rem",
      }}>
        <Fade>
          <div style={{
            fontFamily: T.serif, fontSize: "2rem", color: T.accent,
            marginBottom: "1.5rem", opacity: 0.5,
          }}>✦</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.2rem)", lineHeight: 1.2,
            color: T.textPrimary, maxWidth: 600, marginBottom: "1.2rem",
          }}>
            Welcome to <em style={{ fontStyle: "italic", color: T.accent }}>Loore</em>.
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.8, color: T.textSecondary, maxWidth: 500,
          }}>
            You're one of the first people here. This is an alpha —
            things are raw, evolving, alive. Your experience and your
            feedback shape what Loore becomes.
          </p>
        </Fade>
      </div>

      {/* The journaling prompt — the main CTA */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "1rem 0 4rem" }}>
        <Fade>
          <div style={{
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 14, padding: "2.2rem 2rem", textAlign: "center",
            position: "relative", overflow: "hidden",
          }}>
            {/* Subtle glow behind */}
            <div style={{
              position: "absolute", top: "-30%", left: "50%",
              transform: "translateX(-50%)",
              width: 300, height: 200, borderRadius: "50%",
              background: `radial-gradient(circle, ${T.accentGlow} 0%, transparent 70%)`,
              pointerEvents: "none", opacity: 0.5,
            }} />

            <div style={{
              fontFamily: T.sans, fontSize: "0.68rem", letterSpacing: "0.18em",
              textTransform: "uppercase", color: T.accent, opacity: 0.6,
              marginBottom: "1.2rem", position: "relative",
            }}>Your first entry</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300,
              fontSize: "clamp(1.1rem, 2.5vw, 1.3rem)", lineHeight: 1.6,
              color: T.textPrimary, maxWidth: 440, margin: "0 auto 1.8rem",
              position: "relative",
            }}>
              What brought you to Loore — and what are you hoping to
              find here?
            </p>
            <a
              href="/app/new"
              onMouseEnter={() => setPromptHovered(true)}
              onMouseLeave={() => setPromptHovered(false)}
              style={{
                display: "inline-flex", alignItems: "center", gap: "0.6rem",
                fontFamily: T.sans, fontWeight: 400, fontSize: "0.92rem",
                letterSpacing: "0.05em", padding: "12px 30px",
                border: `1px solid ${T.accent}`,
                background: promptHovered ? T.accentSubtle : "transparent",
                color: T.accent, textDecoration: "none", cursor: "pointer",
                transition: "all 0.4s cubic-bezier(0.22,1,0.36,1)",
                boxShadow: promptHovered ? `0 0 30px ${T.accentGlow}` : "none",
                position: "relative",
              }}
            >
              <span>Start writing</span>
              <span style={{ fontSize: "1rem" }}>→</span>
            </a>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.78rem",
              color: T.textMuted, marginTop: "1rem", position: "relative",
            }}>
              You can type or record a voice note — whatever feels natural.
            </p>
          </div>
        </Fade>
      </div>

      {/* Import CTA */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 2.5rem" }}>
        <Fade>
          <div style={{
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 14, padding: "2.2rem 2rem", textAlign: "center",
            position: "relative", overflow: "hidden",
          }}>
            {/* Subtle glow behind */}
            <div style={{
              position: "absolute", top: "-30%", left: "50%",
              transform: "translateX(-50%)",
              width: 300, height: 200, borderRadius: "50%",
              background: `radial-gradient(circle, ${T.accentGlow} 0%, transparent 70%)`,
              pointerEvents: "none", opacity: 0.5,
            }} />

            <div style={{
              fontFamily: T.sans, fontSize: "0.68rem", letterSpacing: "0.18em",
              textTransform: "uppercase", color: T.accent, opacity: 0.6,
              marginBottom: "1.2rem", position: "relative",
            }}>Already have a journal?</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300,
              fontSize: "clamp(1.1rem, 2.5vw, 1.3rem)", lineHeight: 1.6,
              color: T.textPrimary, maxWidth: 440, margin: "0 auto 1.8rem",
              position: "relative",
            }}>
              Import your Obsidian journals, markdown files, or exported tweets.
              Your lore doesn't start from zero.
            </p>
            <a href="/app/import" style={{
              display: "inline-flex", alignItems: "center", gap: "0.6rem",
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.92rem",
              letterSpacing: "0.05em", padding: "12px 30px",
              border: `1px solid ${T.accent}`,
              background: "transparent",
              color: T.accent, textDecoration: "none", cursor: "pointer",
              transition: "all 0.4s cubic-bezier(0.22,1,0.36,1)",
              position: "relative",
            }}>
              <span>Import files</span>
              <span style={{ fontSize: "1rem" }}>→</span>
            </a>
          </div>
        </Fade>
      </div>

      {/* How To link */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 3rem", textAlign: "center" }}>
        <Fade delay={0.08}>
          <a href="/how-to" style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.88rem",
            color: T.accent, textDecoration: "none",
            borderBottom: `1px solid ${T.accentGlow}`,
            paddingBottom: 2,
          }}>
            See practical tips &amp; workflows →
          </a>
        </Fade>
      </div>

      {/* Gentle closing */}
      <div style={{
        maxWidth: 500, margin: "0 auto", padding: "2rem 0 5rem",
        textAlign: "center",
      }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.3, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <p style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.2rem, 2.5vw, 1.5rem)",
            lineHeight: 1.5, color: T.textSecondary, marginBottom: "0.8rem",
          }}>
            There's no wrong way to do this.
          </p>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.95rem",
            lineHeight: 1.8, color: T.textMuted, maxWidth: 420, margin: "0 auto 1.5rem",
          }}>
            Write about today. Talk about a dream. Process something that's been
            sitting in you. Loore will meet you wherever you are.
          </p>
        </Fade>
        <Fade delay={0.2}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.85rem",
            color: T.textMuted, opacity: 0.7,
          }}>
            If something's broken or feels wrong, tell us.
            <br />This is ours to shape together.
          </p>
        </Fade>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   PAGE 4: WHY LOORE
   ═══════════════════════════════════════════ */

function WhyLoorePage() {
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
            fontFamily: T.sans, fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: T.textMuted, marginBottom: "2.5rem",
          }}>Why Loore</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.4rem)", lineHeight: 1.2,
            color: T.textPrimary, maxWidth: 650, marginBottom: "1.2rem",
          }}>
            A place to <em style={{ fontStyle: "italic", color: T.accent }}>become yourself</em>.
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.8, color: T.textSecondary, maxWidth: 520,
          }}>
            AI is powerful. Loore puts that power in service of something
            personal — understanding who you are and authoring who you're becoming.
          </p>
        </Fade>
      </div>

      {/* The core idea */}
      <div style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 0 4rem" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: T.textPrimary, marginBottom: "1.5rem",
          }}>
            AI for <em style={{ fontStyle: "italic", color: T.accent }}>self-understanding</em>
          </h2>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: T.textSecondary, marginBottom: "1.5rem",
          }}>
            Most AI apps are designed to answer questions. Loore is designed to help
            you understand yourself. Everything you write contributes to a deepening
            picture — your patterns, your values, your blind spots. The AI remembers
            not just to serve better answers, but to help you see more clearly.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: T.textSecondary,
          }}>
            Your lore compounds. And so does the quality of reflection you get back.
          </p>
        </Fade>
      </div>

      {/* What's different */}
      <div style={{ maxWidth: 640, margin: "0 auto", padding: "2rem 0 4rem" }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: T.textPrimary, marginBottom: "1.5rem",
          }}>
            What Loore <em style={{ fontStyle: "italic", color: T.accent }}>gives you</em>
          </h2>
        </Fade>

        <Fade delay={0.12}>
          <div style={{
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.95rem",
              color: T.textPrimary, marginBottom: "0.6rem",
            }}>Conversations that compound</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: T.textMuted,
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
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.95rem",
              color: T.textPrimary, marginBottom: "0.6rem",
            }}>Context you can manage</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: T.textMuted,
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
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.95rem",
              color: T.textPrimary, marginBottom: "0.6rem",
            }}>Choose your intelligence</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: T.textMuted,
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
            background: T.bgCard, border: `1px solid ${T.border}`,
            borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
          }}>
            <div style={{
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.95rem",
              color: T.textPrimary, marginBottom: "0.6rem",
            }}>Capture thinking, not just typing</div>
            <p style={{
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.9rem",
              lineHeight: 1.75, color: T.textMuted,
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
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.4, margin: "0 0 3rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <h2 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.5rem, 3.5vw, 2.1rem)", lineHeight: 1.35,
            color: T.textPrimary, marginBottom: "1.5rem",
          }}>
            The <em style={{ fontStyle: "italic", color: T.accent }}>bigger picture</em>
          </h2>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: T.textSecondary, marginBottom: "1.5rem",
          }}>
            AI is rapidly gaining agency. Loore is designed to help <em style={{ fontStyle: "italic",
            color: T.textPrimary }}>you</em> gain yours — become more aware of your own
            patterns, more intentional about your choices, more articulate about who
            you are and what you want.
          </p>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.85, color: T.textSecondary,
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
            fontFamily: T.serif, fontWeight: 300, fontStyle: "italic",
            fontSize: "clamp(1.2rem, 2.5vw, 1.55rem)",
            lineHeight: 1.5, color: T.textSecondary, marginBottom: "2.5rem",
          }}>
            And this is just the Alpha. There's so much more to come.
          </p>
        </Fade>
        <Fade delay={0.1}>
          <div style={{ display: "flex", gap: "1.5rem", justifyContent: "center", flexWrap: "wrap" }}>
            <a href="/login?returnUrl=%2F" style={{
              display: "inline-flex", alignItems: "center", gap: "0.6rem",
              fontFamily: T.sans, fontWeight: 400, fontSize: "0.92rem",
              letterSpacing: "0.05em", padding: "12px 30px",
              border: `1px solid ${T.accent}`, background: "transparent",
              color: T.accent, textDecoration: "none", cursor: "pointer",
            }}>
              <span>Join the Alpha</span>
              <span style={{ fontSize: "1rem" }}>→</span>
            </a>
            <a href="/vision" style={{
              display: "inline-flex", alignItems: "center", gap: "0.6rem",
              fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
              padding: "12px 0", color: T.textMuted, textDecoration: "none",
              borderBottom: `1px solid ${T.border}`,
            }}>
              Read the full vision →
            </a>
          </div>
        </Fade>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   PAGE 5: HOW TO USE LOORE
   ═══════════════════════════════════════════ */

function HowToTip({ icon, title, body }) {
  return (
    <div style={{
      background: T.bgCard, border: `1px solid ${T.border}`,
      borderRadius: 10, padding: "1.4rem 1.5rem",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: "0.7rem",
        marginBottom: "0.7rem",
      }}>
        <span style={{ fontSize: "1rem", color: T.accent, opacity: 0.7 }}>{icon}</span>
        <span style={{
          fontFamily: T.sans, fontWeight: 400, fontSize: "0.88rem",
          color: T.textPrimary,
        }}>{title}</span>
      </div>
      <p style={{
        fontFamily: T.sans, fontWeight: 300, fontSize: "0.88rem",
        lineHeight: 1.7, color: T.textMuted,
      }}>{body}</p>
    </div>
  );
}

function WorkflowCard({ title, description, steps }) {
  return (
    <div style={{
      background: T.bgCard, border: `1px solid ${T.border}`,
      borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
    }}>
      <h3 style={{
        fontFamily: T.serif, fontWeight: 400, fontSize: "1.15rem",
        color: T.textPrimary, marginBottom: "0.6rem",
      }}>{title}</h3>
      <p style={{
        fontFamily: T.sans, fontWeight: 300, fontSize: "0.9rem",
        lineHeight: 1.75, color: T.textSecondary, marginBottom: "1.2rem",
      }}>{description}</p>
      <div style={{
        display: "flex", flexWrap: "wrap", gap: "0.5rem",
      }}>
        {steps.map((step, i) => (
          <React.Fragment key={i}>
            <span style={{
              fontFamily: T.sans, fontSize: "0.72rem", fontWeight: 400,
              letterSpacing: "0.05em",
              color: T.accent, opacity: 0.7,
              background: T.accentSubtle,
              padding: "4px 10px", borderRadius: 4,
            }}>{step}</span>
            {i < steps.length - 1 && (
              <span style={{
                color: T.textMuted, opacity: 0.4,
                fontSize: "0.75rem", lineHeight: "24px",
              }}>→</span>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

function HowToPage() {
  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Header */}
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        textAlign: "center", padding: "5rem 0 3rem",
      }}>
        <Fade>
          <div style={{
            fontFamily: T.sans, fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: T.textMuted, marginBottom: "2rem",
          }}>How to use Loore</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.8rem, 4.5vw, 3rem)", lineHeight: 1.2,
            color: T.textPrimary, maxWidth: 600, marginBottom: "1.2rem",
          }}>
            Practical tips &amp; <em style={{ fontStyle: "italic", color: T.accent }}>workflows</em>
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "1rem",
            lineHeight: 1.8, color: T.textSecondary, maxWidth: 480,
          }}>
            Loore is flexible enough to fit how you think. Here's what the
            basics look like — and some workflows that the creator uses every day.
          </p>
        </Fade>
      </div>

      {/* The Basics */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 4rem" }}>
        <Fade>
          <div style={{
            fontFamily: T.sans, fontSize: "0.7rem", letterSpacing: "0.18em",
            textTransform: "uppercase", color: T.accent, opacity: 0.7,
            marginBottom: "1.5rem", paddingBottom: "0.8rem",
            borderBottom: `1px solid ${T.border}`,
          }}>The Basics</div>
        </Fade>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
          <Fade delay={0.04}>
            <HowToTip
              icon="◇"
              title="Conversations branch"
              body="Every message can become a fork. Follow one thread of thought, then go back and explore another. Your thinking doesn't have to be linear."
            />
          </Fade>
          <Fade delay={0.08}>
            <HowToTip
              icon="◈"
              title="Voice notes become text"
              body="Record a voice note and it's transcribed automatically. Speak your thoughts on a walk, then read and reflect on them later — searchable and editable."
            />
          </Fade>
          <Fade delay={0.12}>
            <HowToTip
              icon="✦"
              title="Your profile grows with you"
              body="Loore builds a living understanding of who you are — your values, patterns, intentions. The more you share, the more personal and useful its reflections become."
            />
          </Fade>
          <Fade delay={0.16}>
            <HowToTip
              icon="⬡"
              title="Multiple AI models, one conversation"
              body="Use different language models within the same thread. Compare perspectives. Choose the voice that resonates. Branch a response to try a different model."
            />
          </Fade>
          <Fade delay={0.2}>
            <HowToTip
              icon="◆"
              title="Import what you've already written"
              body="Bring in your Obsidian journals, markdown files, or exported tweets. Add them as one long thread or each file as its own entry. Set privacy and AI interaction for the whole batch."
            />
          </Fade>
          <Fade delay={0.24}>
            <HowToTip
              icon="▣"
              title="Link and quote across threads"
              body="Reference your user profile in any conversation for instant context. Quote from one thread into another. Split off tangents into their own conversations without losing the original."
            />
          </Fade>
        </div>
      </div>

      {/* Workflows */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 4rem" }}>
        <Fade>
          <div style={{
            fontFamily: T.sans, fontSize: "0.7rem", letterSpacing: "0.18em",
            textTransform: "uppercase", color: T.accent, opacity: 0.7,
            marginBottom: "0.8rem", paddingBottom: "0.8rem",
            borderBottom: `1px solid ${T.border}`,
          }}>Workflows</div>
        </Fade>
        <Fade>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
            lineHeight: 1.75, color: T.textMuted, marginBottom: "1.5rem",
          }}>
            These are real workflows being used daily in Loore right now.
            They emerge naturally from combining the building blocks above.
          </p>
        </Fade>

        <Fade delay={0.06}>
          <WorkflowCard
            title="Daily prioritization"
            description="Keep a running to-do list in Loore. Each morning, share a brief state update — how you slept, what's on your mind, what the day looks like. Link your user profile and today's update into your to-do thread. Loore recommends what to prioritize based on who you are and where you are today."
            steps={["State update", "User profile", "To-do thread", "AI prioritization"]}
          />
        </Fade>
        <Fade delay={0.1}>
          <WorkflowCard
            title="Walk-and-reflect"
            description="Record a voice note while walking — whatever's on your mind. The transcription appears in your feed instantly. Link your user profile and let Loore reflect: what patterns is it seeing? What was vague that now has a name? The walk becomes a reflection session."
            steps={["Voice note", "Transcription", "Profile context", "AI reflection"]}
          />
        </Fade>
        <Fade delay={0.14}>
          <WorkflowCard
            title="Chat with your archive"
            description="Use the user_export keyword to bring your full archive into a conversation. Ask Loore to assess your personal type (e.g. Enneagram, MBTI, or any typology of your choosing) with reasoning, find recurring themes across months of entries, or surface connections between experiences you hadn't noticed."
            steps={["Your archive", "A question", "Deep analysis"]}
          />
        </Fade>
        <Fade delay={0.18}>
          <WorkflowCard
            title="Import and discover"
            description="Import a folder of Obsidian journal entries or your exported tweets. Let Loore build a user profile from them. Suddenly months or years of scattered writing become a coherent picture — patterns, values, and blind spots you didn't know you had."
            steps={["Import files", "Profile generation", "Self-discovery"]}
          />
        </Fade>
        <Fade delay={0.22}>
          <WorkflowCard
            title="Feedback loop"
            description="When Loore's advice misses the mark, tell it why. Your correction gets absorbed — next time, it factors in what it learned. The more you push back, the sharper the reflection becomes. It's not about being right; it's about learning you."
            steps={["AI suggestion", "Your correction", "Adjusted future advice"]}
          />
        </Fade>
      </div>

      {/* Closing */}
      <div style={{
        maxWidth: 480, margin: "0 auto", padding: "2rem 0 5rem",
        textAlign: "center",
      }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: T.accent, opacity: 0.3, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <p style={{
            fontFamily: T.serif, fontWeight: 300,
            fontSize: "clamp(1.2rem, 2.5vw, 1.5rem)",
            lineHeight: 1.5, color: T.textSecondary, marginBottom: "1rem",
          }}>
            These workflows emerged naturally.
          </p>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: T.sans, fontWeight: 300, fontSize: "0.92rem",
            lineHeight: 1.8, color: T.textMuted, maxWidth: 400, margin: "0 auto",
          }}>
            Yours will too. Start with what feels natural and let the
            tools surprise you. If you discover a workflow that works,
            we'd love to hear about it.
          </p>
        </Fade>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════
   TAB NAVIGATION (preview scaffold)
   ═══════════════════════════════════════════ */

const tabs = [
  { id: "vision", label: "Vision", Component: VisionPage },
  { id: "whyloore", label: "Why Loore", Component: WhyLoorePage },
  { id: "howto", label: "How To", Component: HowToPage },
  { id: "thankyou", label: "Alpha Thank You", Component: AlphaThankYou },
  { id: "welcome", label: "Welcome", Component: WelcomePage },
];

export default function LoorePages() {
  const [active, setActive] = useState("vision");
  const ActiveComponent = tabs.find(t => t.id === active).Component;

  return (
    <div style={{
      background: T.bgDeep, color: T.textPrimary,
      fontFamily: T.sans, minHeight: "100vh", position: "relative",
    }}>
      {/* Google Fonts */}
      <link
        href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Outfit:wght@300;400;500&display=swap"
        rel="stylesheet"
      />

      {/* Background atmosphere */}
      <div style={{
        position: "fixed", inset: 0, pointerEvents: "none", zIndex: 0,
        background: "radial-gradient(ellipse 80% 60% at 50% 0%, #1a150f 0%, transparent 70%)",
      }} />

      {/* Tab bar */}
      <div style={{
        position: "sticky", top: 0, zIndex: 100,
        display: "flex", gap: 0,
        background: "#0a0908", borderBottom: `1px solid ${T.border}`,
      }}>
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActive(tab.id)}
            style={{
              padding: "14px 28px",
              fontFamily: T.sans, fontSize: "0.85rem", fontWeight: 400,
              letterSpacing: "0.05em",
              color: active === tab.id ? T.accent : T.textMuted,
              cursor: "pointer", border: "none", background: "none",
              borderBottom: `2px solid ${active === tab.id ? T.accent : "transparent"}`,
              transition: "all 0.3s ease",
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Page content */}
      <div style={{ position: "relative", zIndex: 1 }}>
        <ActiveComponent />
      </div>

      {/* Footer */}
      <footer style={{
        textAlign: "center", padding: "2rem",
        fontFamily: T.sans, fontSize: "0.8rem", color: T.textMuted,
        borderTop: `1px solid #1e1d1a`, position: "relative", zIndex: 1,
      }}>
        © {new Date().getFullYear()} Loore
      </footer>
    </div>
  );
}
