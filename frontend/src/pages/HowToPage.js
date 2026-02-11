import React from "react";
import Fade from "../utils/Fade";

function HowToTip({ icon, title, body }) {
  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border)",
      borderRadius: 10, padding: "1.4rem 1.5rem",
    }}>
      <div style={{
        display: "flex", alignItems: "center", gap: "0.7rem",
        marginBottom: "0.7rem",
      }}>
        <span style={{ fontSize: "1rem", color: "var(--accent)", opacity: 0.7 }}>{icon}</span>
        <span style={{
          fontFamily: "var(--sans)", fontWeight: 400, fontSize: "0.88rem",
          color: "var(--text-primary)",
        }}>{title}</span>
      </div>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.88rem",
        lineHeight: 1.7, color: "var(--text-muted)",
      }}>{body}</p>
    </div>
  );
}

function WorkflowCard({ title, description, steps }) {
  return (
    <div style={{
      background: "var(--bg-card)", border: "1px solid var(--border)",
      borderRadius: 12, padding: "1.8rem 2rem", marginBottom: "1rem",
    }}>
      <h3 style={{
        fontFamily: "var(--serif)", fontWeight: 400, fontSize: "1.15rem",
        color: "var(--text-primary)", marginBottom: "0.6rem",
      }}>{title}</h3>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.9rem",
        lineHeight: 1.75, color: "var(--text-secondary)", marginBottom: "1.2rem",
      }}>{description}</p>
      <div style={{
        display: "flex", flexWrap: "wrap", gap: "0.5rem",
      }}>
        {steps.map((step, i) => (
          <React.Fragment key={i}>
            <span style={{
              fontFamily: "var(--sans)", fontSize: "0.72rem", fontWeight: 400,
              letterSpacing: "0.05em",
              color: "var(--accent)", opacity: 0.7,
              background: "var(--accent-subtle)",
              padding: "4px 10px", borderRadius: 4,
            }}>{step}</span>
            {i < steps.length - 1 && (
              <span style={{
                color: "var(--text-muted)", opacity: 0.4,
                fontSize: "0.75rem", lineHeight: "24px",
              }}>&rarr;</span>
            )}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

export default function HowToPage() {
  return (
    <div style={{ padding: "0 2rem" }}>
      {/* Header */}
      <div style={{
        display: "flex", flexDirection: "column", alignItems: "center",
        textAlign: "center", padding: "5rem 0 3rem",
      }}>
        <Fade>
          <div style={{
            fontFamily: "var(--sans)", fontSize: "0.72rem", letterSpacing: "0.2em",
            textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "2rem",
          }}>How to use Loore</div>
        </Fade>
        <Fade delay={0.1}>
          <h1 style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.8rem, 4.5vw, 3rem)", lineHeight: 1.2,
            color: "var(--text-primary)", maxWidth: 600, marginBottom: "1.2rem",
          }}>
            Practical tips &amp; <em style={{ fontStyle: "italic", color: "var(--accent)" }}>workflows</em>
          </h1>
        </Fade>
        <Fade delay={0.18}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "1rem",
            lineHeight: 1.8, color: "var(--text-secondary)", maxWidth: 480,
          }}>
            Loore is flexible enough to fit how you think. Here's what the
            basics look like — and some workflows that are being used on Loore every day.
          </p>
        </Fade>
      </div>

      {/* The Basics */}
      <div style={{ maxWidth: 580, margin: "0 auto", padding: "0 0 4rem" }}>
        <Fade>
          <div style={{
            fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.18em",
            textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
            marginBottom: "1.5rem", paddingBottom: "0.8rem",
            borderBottom: "1px solid var(--border)",
          }}>The Basics</div>
        </Fade>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.8rem" }}>
          <Fade delay={0.04}>
            <HowToTip
              icon={"\u25C7"}
              title="Conversations branch"
              body="Every message can become a fork. Follow one thread of thought, then go back and explore another. Your thinking doesn't have to be linear."
            />
          </Fade>
          <Fade delay={0.08}>
            <HowToTip
              icon={"\u25C8"}
              title="Voice notes become text"
              body="Record a voice note and it's transcribed automatically. Speak your thoughts on a walk, then read and reflect on them later — searchable and editable."
            />
          </Fade>
          <Fade delay={0.12}>
            <HowToTip
              icon={"\u2726"}
              title="Your profile grows with you"
              body="Loore builds a living understanding of who you are — your values, patterns, intentions. The more you share, the more personal and useful its reflections become."
            />
          </Fade>
          <Fade delay={0.16}>
            <HowToTip
              icon={"\u2B21"}
              title="Multiple AI models, one conversation"
              body="Use different language models within the same thread. Compare perspectives. Choose the voice that resonates. Branch a response to try a different model."
            />
          </Fade>
          <Fade delay={0.2}>
            <HowToTip
              icon={"\u25C6"}
              title="Import what you've already written"
              body="Bring in your Obsidian journals, markdown files, or exported tweets. Add them as one long thread or each file as its own entry. Set privacy and AI interaction for the whole batch."
            />
          </Fade>
          <Fade delay={0.24}>
            <HowToTip
              icon={"\u25A3"}
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
            fontFamily: "var(--sans)", fontSize: "0.7rem", letterSpacing: "0.18em",
            textTransform: "uppercase", color: "var(--accent)", opacity: 0.7,
            marginBottom: "0.8rem", paddingBottom: "0.8rem",
            borderBottom: "1px solid var(--border)",
          }}>Workflows</div>
        </Fade>
        <Fade>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
            lineHeight: 1.75, color: "var(--text-muted)", marginBottom: "1.5rem",
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
          <div style={{ width: 40, height: 1, background: "var(--accent)", opacity: 0.3, margin: "0 auto 2.5rem" }} />
        </Fade>
        <Fade delay={0.08}>
          <p style={{
            fontFamily: "var(--serif)", fontWeight: 300,
            fontSize: "clamp(1.2rem, 2.5vw, 1.5rem)",
            lineHeight: 1.5, color: "var(--text-secondary)", marginBottom: "1rem",
          }}>
            These workflows emerged naturally.
          </p>
        </Fade>
        <Fade delay={0.14}>
          <p style={{
            fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
            lineHeight: 1.8, color: "var(--text-muted)", maxWidth: 400, margin: "0 auto",
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
