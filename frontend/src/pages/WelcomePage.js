import React from "react";
import { Link } from "react-router-dom";
import Fade from "../utils/Fade";
import ImportData from "../components/ImportData";
import CtaButton from "../components/CtaButton";
import PrefillConsentCard from "../components/PrefillConsentCard";

const importButtonStyle = {
  display: "inline-flex", alignItems: "center", gap: "0.4rem",
  background: "transparent", border: "none", padding: 0,
  color: "var(--accent)", cursor: "pointer",
  fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.9rem",
  letterSpacing: "0.04em", whiteSpace: "nowrap",
};

const importButtonHoverStyle = {
  textDecoration: "underline",
  textUnderlineOffset: 3,
};

export default function WelcomePage() {
  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Hero welcome */}
      <div style={{
        minHeight: "55vh", display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center",
        textAlign: "center", padding: "4rem 0 2rem",
      }}>
        <Fade>
          <img
            src={process.env.PUBLIC_URL + "/loore-logo-transparent.svg"}
            alt="Loore"
            style={{
              width: "2rem", height: "2rem",
              marginBottom: "1.5rem", opacity: 0.5,
            }}
          />
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(2rem, 5vw, 3.2rem)", lineHeight: 1.2,
            color: "var(--text-primary)", maxWidth: 600, marginBottom: "1.2rem",
          }}>
            Welcome to <em style={{ fontStyle: "italic", color: "var(--accent)" }}>Loore</em>.
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1.05rem",
            lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 500,
          }}>
            You're one of the first people here. This is an alpha —
            things are raw, evolving, alive. Your experience and your
            feedback shape what Loore becomes.
          </p>
        </Fade>
      </div>

      {/* Import, as one line ABOVE the main CTA (#306): "Reflect" navigates
          away, so anything below it goes unseen, and import is how the
          profile stops starting from zero. Kept small so writing something
          stays the main invitation. */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "1rem 0 1.2rem" }}>
        <Fade>
          <div style={{
            display: "flex", alignItems: "center", justifyContent: "space-between",
            flexWrap: "wrap", gap: "0.5rem 1.2rem",
            border: "1px solid var(--border)", borderRadius: 10,
            padding: "0.85rem 1.2rem",
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
            lineHeight: 1.5, color: "var(--text-secondary)",
          }}>
            <span>Already have journals, notes, AI chats or tweets?</span>
            <ImportData
              buttonLabel={<><span>Import them</span><span aria-hidden="true">&rarr;</span></>}
              buttonStyle={importButtonStyle}
              buttonHoverStyle={importButtonHoverStyle}
            />
          </div>
        </Fade>
      </div>

      {/* Second touch for the tweets opt-in — only for X-login users who
          never answered on /alpha-thank-you. Renders null otherwise. Above
          Reflect for the same reason as the import strip: this is the last
          place the app asks. */}
      <div style={{ maxWidth: 580, margin: "0 auto" }}>
        <Fade>
          <PrefillConsentCard delayHint="Already on X?" style={{ maxWidth: "none", padding: "2.2rem 2rem", textAlign: "center", marginBottom: "1.2rem" }} />
        </Fade>
      </div>

      {/* The journaling prompt — the main CTA */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 4rem" }}>
        <Fade>
          <div style={{
            background: "var(--bg-card)", border: "1px solid var(--border)",
            borderRadius: 14, padding: "2.2rem 2rem", textAlign: "center",
            position: "relative", overflow: "hidden",
          }}>
            {/* Subtle glow behind */}
            <div style={{
              position: "absolute", top: "-30%", left: "50%",
              transform: "translateX(-50%)",
              width: 300, height: 200, borderRadius: "50%",
              background: "radial-gradient(circle, var(--accent-glow) 0%, transparent 70%)",
              pointerEvents: "none", opacity: 0.5,
            }} />

            <div style={{
              fontFamily: "var(--sans)", fontSize: "0.68rem", letterSpacing: "0.18em",
              textTransform: "uppercase", color: "var(--accent)", opacity: 0.6,
              marginBottom: "1.2rem", position: "relative",
            }}>Your first entry</div>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300,
              fontSize: "clamp(1.1rem, 2.5vw, 1.3rem)", lineHeight: 1.6,
              color: "var(--text-primary)", maxWidth: 440, margin: "0 auto 1.8rem",
              position: "relative",
            }}>
              What brought you to Loore — and what are you hoping to
              find here?
            </p>
            {/* The homepage's voice / text modes are the first-entry
                experience; the full entry editor is too much at this point. */}
            <CtaButton to="/">Reflect</CtaButton>
            <p style={{
              fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.78rem",
              color: "var(--text-muted)", marginTop: "1rem", position: "relative",
            }}>
              Take the question with you, or start with whatever is on your
              mind. Type or record a voice note, whichever feels natural.
            </p>
          </div>
        </Fade>
      </div>

      {/* How To link */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 3rem", textAlign: "center" }}>
        <Fade delay={0.08}>
          <Link to="/how-to" style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem",
            color: "var(--accent)", textDecoration: "none",
            borderBottom: "1px solid var(--accent-glow)",
            paddingBottom: 2,
          }}>
            See practical tips &amp; workflows &rarr;
          </Link>
        </Fade>
      </div>

      {/* Gentle closing */}
      <div style={{
        maxWidth: 500, margin: "0 auto", padding: "2rem 0 5rem",
        textAlign: "center",
      }}>
        <Fade>
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.3, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.2rem, 2.5vw, 1.5rem)",
            lineHeight: 1.5, color: "var(--text-secondary)", marginBottom: "0.8rem",
          }}>
            There's no wrong way to do this.
          </p>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.95rem",
            lineHeight: 1.8, color: "var(--text-muted)", maxWidth: 420, margin: "0 auto 1.5rem",
          }}>
            Write about today. Talk about a dream. Process something that's been
            sitting in you. Loore will meet you wherever you are.
          </p>
        </Fade>
        <Fade delay={0.2}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.85rem",
            color: "var(--text-muted)", opacity: 0.7,
          }}>
            If something's broken or feels wrong, tell us.
            <br />This is ours to shape together.
          </p>
        </Fade>
      </div>
    </div>
  );
}
