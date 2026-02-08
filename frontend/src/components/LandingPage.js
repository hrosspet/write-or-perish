import React, { useState, useEffect, useRef } from "react";

const backendUrl = process.env.REACT_APP_BACKEND_URL;

function useOnScreen(ref, threshold = 0.15) {
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

function FadeSection({ children, delay = 0, className = "", style = {} }) {
  const ref = useRef(null);
  const visible = useOnScreen(ref, 0.1);
  return (
    <div
      ref={ref}
      className={className}
      style={{
        opacity: visible ? 1 : 0,
        transform: visible ? "translateY(0)" : "translateY(32px)",
        transition: `opacity 0.9s cubic-bezier(0.22,1,0.36,1) ${delay}s, transform 0.9s cubic-bezier(0.22,1,0.36,1) ${delay}s`,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

const styles = {
  global: `
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500&family=Outfit:wght@300;400;500&display=swap');

    .loore-landing * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }

    .loore-landing {
      --bg-deep: #0e0d0b;
      --bg-surface: #151412;
      --bg-card: #1a1917;
      --text-primary: #e8e2d6;
      --text-secondary: #9e9688;
      --text-muted: #6b655b;
      --accent: #c4956a;
      --accent-glow: #c4956a33;
      --accent-subtle: #c4956a18;
      --serif: 'Cormorant Garamond', Georgia, serif;
      --sans: 'Outfit', system-ui, sans-serif;

      background: var(--bg-deep);
      color: var(--text-primary);
      font-family: var(--sans);
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
    }

    .loore-landing::before {
      content: '';
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 80% 60% at 50% 0%, #1a150f 0%, transparent 70%),
        radial-gradient(ellipse 50% 40% at 80% 20%, #1a130d08 0%, transparent 60%);
      pointer-events: none;
      z-index: 0;
    }

    .loore-hero {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      position: relative;
      padding: 2rem;
      text-align: center;
    }

    .loore-hero-grain {
      position: absolute;
      inset: 0;
      opacity: 0.03;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
      background-size: 200px;
      pointer-events: none;
    }

    .loore-logo-mark {
      font-family: var(--serif);
      font-weight: 300;
      font-size: 1.1rem;
      letter-spacing: 0.35em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 3rem;
      opacity: 0;
      animation: loore-fade-down 1s cubic-bezier(0.22,1,0.36,1) 0.2s forwards;
    }

    .loore-hero-title {
      font-family: var(--serif);
      font-weight: 300;
      font-size: clamp(2.4rem, 6vw, 4.5rem);
      line-height: 1.15;
      max-width: 800px;
      color: var(--text-primary);
      margin-bottom: 1rem;
      opacity: 0;
      animation: loore-fade-down 1.2s cubic-bezier(0.22,1,0.36,1) 0.4s forwards;
    }

    .loore-hero-title em {
      font-style: italic;
      color: var(--accent);
    }

    .loore-hero-subtitle {
      font-family: var(--sans);
      font-weight: 300;
      font-size: clamp(1rem, 2vw, 1.15rem);
      color: var(--text-secondary);
      max-width: 520px;
      line-height: 1.7;
      margin-bottom: 2.8rem;
      opacity: 0;
      animation: loore-fade-down 1.2s cubic-bezier(0.22,1,0.36,1) 0.7s forwards;
    }

    .loore-cta {
      display: inline-flex;
      align-items: center;
      gap: 0.6rem;
      font-family: var(--sans);
      font-weight: 400;
      font-size: 0.95rem;
      letter-spacing: 0.06em;
      padding: 14px 36px;
      border: 1px solid var(--accent);
      background: transparent;
      color: var(--accent);
      text-decoration: none;
      cursor: pointer;
      transition: all 0.4s cubic-bezier(0.22,1,0.36,1);
      position: relative;
      overflow: hidden;
      opacity: 0;
      animation: loore-fade-down 1.2s cubic-bezier(0.22,1,0.36,1) 0.9s forwards;
    }

    .loore-cta::before {
      content: '';
      position: absolute;
      inset: 0;
      background: var(--accent);
      opacity: 0;
      transition: opacity 0.4s ease;
    }

    .loore-cta:hover::before {
      opacity: 0.1;
    }

    .loore-cta:hover {
      box-shadow: 0 0 30px var(--accent-glow);
      transform: translateY(-1px);
    }

    .loore-cta-arrow {
      transition: transform 0.3s ease;
      font-size: 1.1rem;
    }

    .loore-cta:hover .loore-cta-arrow {
      transform: translateX(3px);
    }

    .loore-scroll-hint {
      position: absolute;
      bottom: 2.5rem;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 0.5rem;
      opacity: 0;
      animation: loore-fade-down 1s ease 1.5s forwards;
    }

    .loore-scroll-line {
      width: 1px;
      height: 40px;
      background: linear-gradient(to bottom, var(--text-muted), transparent);
      animation: loore-pulse 2s ease-in-out infinite;
    }

    @keyframes loore-fade-down {
      from { opacity: 0; transform: translateY(20px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @keyframes loore-pulse {
      0%, 100% { opacity: 0.3; }
      50% { opacity: 0.8; }
    }

    /* Narrative sections */
    .loore-narrative {
      position: relative;
      z-index: 1;
      padding: 0 2rem;
    }

    .loore-section {
      max-width: 640px;
      margin: 0 auto;
      padding: 6rem 0;
      position: relative;
    }

    .loore-section-divider {
      width: 40px;
      height: 1px;
      background: var(--accent);
      opacity: 0.4;
      margin: 0 auto 3rem;
    }

    .loore-section-lead {
      font-family: var(--serif);
      font-weight: 400;
      font-size: clamp(1.6rem, 3.5vw, 2.2rem);
      line-height: 1.35;
      color: var(--text-primary);
      margin-bottom: 1.5rem;
    }

    .loore-section-lead em {
      font-style: italic;
      color: var(--accent);
    }

    .loore-section-body {
      font-family: var(--sans);
      font-weight: 300;
      font-size: 1.05rem;
      line-height: 1.85;
      color: var(--text-secondary);
    }

    /* Preview mockup */
    .loore-preview {
      max-width: 760px;
      margin: 2rem auto 0;
      padding: 6rem 2rem 8rem;
      text-align: center;
      position: relative;
      z-index: 1;
    }

    .loore-preview-label {
      font-family: var(--sans);
      font-weight: 400;
      font-size: 0.75rem;
      letter-spacing: 0.2em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 2.5rem;
    }

    .loore-mockup {
      background: var(--bg-card);
      border: 1px solid #252320;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 20px 80px rgba(0,0,0,0.4);
    }

    .loore-mockup-bar {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 14px 18px;
      background: #1e1d1a;
      border-bottom: 1px solid #252320;
    }

    .loore-mockup-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #2a2825;
    }

    .loore-mockup-content {
      padding: 2.5rem 2rem;
      text-align: left;
    }

    .loore-mockup-date {
      font-family: var(--sans);
      font-size: 0.75rem;
      color: var(--text-muted);
      letter-spacing: 0.08em;
      margin-bottom: 1.2rem;
    }

    .loore-mockup-entry {
      font-family: var(--serif);
      font-size: 1.25rem;
      line-height: 1.75;
      color: #b5aea2;
      font-style: normal;
      font-weight: 400;
      margin-bottom: 1.8rem;
      padding-bottom: 1.8rem;
      border-bottom: 1px solid #252320;
    }

    .loore-mockup-reflection-label {
      font-family: var(--sans);
      font-size: 0.7rem;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--accent);
      opacity: 0.7;
      margin-bottom: 0.8rem;
    }

    .loore-mockup-reflection {
      font-family: var(--sans);
      font-weight: 300;
      font-size: 0.95rem;
      line-height: 1.75;
      color: var(--text-muted);
    }

    /* Closing */
    .loore-closing {
      text-align: center;
      padding: 4rem 2rem 8rem;
      position: relative;
      z-index: 1;
    }

    .loore-closing-text {
      font-family: var(--serif);
      font-weight: 300;
      font-size: clamp(1.5rem, 3vw, 2rem);
      color: var(--text-secondary);
      max-width: 550px;
      margin: 0 auto 2.5rem;
      line-height: 1.4;
    }

    .loore-closing-text strong {
      color: var(--text-primary);
      font-weight: 400;
    }

    .loore-footer {
      text-align: center;
      padding: 2rem;
      font-family: var(--sans);
      font-size: 0.8rem;
      color: var(--text-muted);
      border-top: 1px solid #1e1d1a;
      position: relative;
      z-index: 1;
    }

    @media (max-width: 640px) {
      .loore-section { padding: 4rem 0; }
      .loore-preview { padding: 3rem 1rem 5rem; }
      .loore-mockup-content { padding: 1.5rem 1.2rem; }
    }
  `,
};

function LandingPage() {
  return (
    <>
      <style>{styles.global}</style>
      <div className="loore-landing">
        {/* Grain overlay */}
        <div className="loore-hero-grain" />

        {/* Hero */}
        <section className="loore-hero">
          <div className="loore-logo-mark">Loore</div>
          <h1 className="loore-hero-title">
            Uncover your <em>lore</em>.
            <br />
            Author yourself.
          </h1>
          <p className="loore-hero-subtitle">
            A tool for seeing the story you're actually living — and shaping it
            with intention.
          </p>
          <a href={`${backendUrl}/auth/login`} className="loore-cta">
            <span>Join the Alpha</span>
            <span className="loore-cta-arrow">→</span>
          </a>
          <div className="loore-scroll-hint">
            <div className="loore-scroll-line" />
          </div>
        </section>

        {/* Narrative sections */}
        <div className="loore-narrative">
          {/* Section 1 */}
          <div className="loore-section">
            <FadeSection>
              <div className="loore-section-divider" />
            </FadeSection>
            <FadeSection delay={0.1}>
              <h2 className="loore-section-lead">
                You are already living a <em>story</em>.
              </h2>
            </FadeSection>
            <FadeSection delay={0.2}>
              <p className="loore-section-body">
                But most of it runs beneath awareness — patterns inherited,
                narratives distorted, intentions half-formed. Loore helps you
                uncover your own lore: the actual shape of your life, not just
                the story you tell yourself.
              </p>
            </FadeSection>
          </div>

          {/* Section 2 */}
          <div className="loore-section">
            <FadeSection>
              <div className="loore-section-divider" />
            </FadeSection>
            <FadeSection delay={0.1}>
              <h2 className="loore-section-lead">
                Surface what's <em>hidden</em>.
              </h2>
            </FadeSection>
            <FadeSection delay={0.2}>
              <p className="loore-section-body">
                Through effortless journaling and AI reflection, you name what's
                vague, see through your blind spots, and begin to author yourself
                more deliberately. This is private, first. Sacred, even.
              </p>
            </FadeSection>
          </div>

          {/* Section 3 */}
          <div className="loore-section">
            <FadeSection>
              <div className="loore-section-divider" />
            </FadeSection>
            <FadeSection delay={0.1}>
              <h2 className="loore-section-lead">
                Your lore becomes an <em>offering</em>.
              </h2>
            </FadeSection>
            <FadeSection delay={0.2}>
              <p className="loore-section-body">
                As you clarify who you are and what you're for, sharing becomes
                natural — not performance, but offering. Your lore becomes part
                of a larger weave. A way of connecting that starts from truth.
              </p>
            </FadeSection>
          </div>
        </div>

        {/* Interface preview */}
        <div className="loore-preview">
          <FadeSection>
            <div className="loore-preview-label">A glimpse inside</div>
          </FadeSection>
          <FadeSection delay={0.15}>
            <div className="loore-mockup">
              <div className="loore-mockup-bar">
                <div className="loore-mockup-dot" />
                <div className="loore-mockup-dot" />
                <div className="loore-mockup-dot" />
              </div>
              <div className="loore-mockup-content">
                <div className="loore-mockup-date">February 8, 2026</div>
                <div className="loore-mockup-entry">
                  I keep reaching for control in situations where what I
                  actually need is to trust the process. The pattern is clear
                  now — every time something matters, I tighten my grip instead
                  of opening my hands...
                </div>
                <div className="loore-mockup-reflection-label">
                  Loore reflects
                </div>
                <div className="loore-mockup-reflection">
                  You've named this pattern three times this month, each time
                  with more precision. Notice the shift: the first entry
                  described frustration with outcomes. Now you're seeing the
                  mechanism itself. That's authorship beginning.
                </div>
              </div>
            </div>
          </FadeSection>
        </div>

        {/* Closing CTA */}
        <div className="loore-closing">
          <FadeSection>
            <p className="loore-closing-text">
              AI is rapidly gaining agency.
              <br />
              <strong>Loore helps you gain yours.</strong>
            </p>
          </FadeSection>
          <FadeSection delay={0.15}>
            <a href={`${backendUrl}/auth/login`} className="loore-cta">
              <span>Begin your lore</span>
              <span className="loore-cta-arrow">→</span>
            </a>
          </FadeSection>
        </div>

        <footer className="loore-footer">
          © {new Date().getFullYear()} Loore
        </footer>
      </div>
    </>
  );
}

export default LandingPage;
