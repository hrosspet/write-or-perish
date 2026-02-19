import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';

function useOnScreen(ref, threshold = 0.1) {
  const [isVisible, setIsVisible] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) setIsVisible(true); },
      { threshold }
    );
    observer.observe(el);
    return () => observer.unobserve(el);
  }, [ref, threshold]);
  return isVisible;
}

function getGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

const cards = [
  {
    key: "reflect",
    path: "/reflect",
    title: "Reflect",
    description: "Speak what's present. Let it come back clearer.",
    icon: (
      <img
        src="/loore-logo-transparent.svg"
        alt=""
        style={{ height: "28px", width: "auto", opacity: 0.7 }}
      />
    ),
  },
  {
    key: "orient",
    path: "/orient",
    title: "Orient",
    description: "Ground your day. See what matters.",
    icon: (
      <svg width="28" height="28" viewBox="0 0 42 42" fill="none">
        <circle cx="21" cy="21" r="16" stroke="#c4956a" strokeWidth="1.2" opacity="0.3"/>
        <circle cx="21" cy="21" r="8" stroke="#c4956a" strokeWidth="1" opacity="0.2"/>
        <line x1="21" y1="2" x2="21" y2="10" stroke="#c4956a" strokeWidth="1.5" strokeLinecap="round" opacity="0.6"/>
        <line x1="21" y1="32" x2="21" y2="40" stroke="#c4956a" strokeWidth="1.5" strokeLinecap="round" opacity="0.3"/>
        <line x1="2" y1="21" x2="10" y2="21" stroke="#c4956a" strokeWidth="1.5" strokeLinecap="round" opacity="0.3"/>
        <line x1="32" y1="21" x2="40" y2="21" stroke="#c4956a" strokeWidth="1.5" strokeLinecap="round" opacity="0.3"/>
        <circle cx="21" cy="21" r="2.5" fill="#c4956a" opacity="0.7"/>
      </svg>
    ),
  },
  {
    key: "converse",
    path: "/converse",
    title: "Converse",
    description: "Ask anything. Think out loud.",
    icon: (
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5">
        <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
        <path d="M8 10h8M8 13h5" strokeLinecap="round" />
      </svg>
    ),
  },
];

function WorkflowCard({ card, delay }) {
  const ref = useRef(null);
  const isVisible = useOnScreen(ref);
  const navigate = useNavigate();
  const [hovered, setHovered] = useState(false);

  return (
    <div
      ref={ref}
      onClick={() => navigate(card.path)}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        flex: "1 1 200px",
        maxWidth: "280px",
        background: "var(--bg-card)",
        border: `1px solid ${hovered ? 'var(--border-hover)' : 'var(--border)'}`,
        borderRadius: "12px",
        padding: "28px 24px",
        cursor: "pointer",
        position: "relative",
        overflow: "hidden",
        transform: isVisible
          ? `translateY(${hovered ? '-3px' : '0'})`
          : 'translateY(20px)',
        opacity: isVisible ? 1 : 0,
        transition: `all 0.5s ease ${delay}ms, transform 0.2s ease, border-color 0.2s ease`,
        boxShadow: hovered ? '0 12px 48px rgba(0,0,0,0.35), 0 0 40px var(--accent-glow)' : 'none',
      }}
    >
      {/* Accent top line */}
      <div style={{
        position: "absolute",
        top: 0,
        left: 0,
        right: 0,
        height: "1px",
        background: "linear-gradient(90deg, transparent, var(--accent), transparent)",
        opacity: hovered ? 0.5 : 0,
        transition: "opacity 0.4s ease",
      }} />

      <div style={{ marginBottom: "16px" }}>
        {card.icon}
      </div>
      <h3 style={{
        fontFamily: "var(--serif)",
        fontSize: "1.2rem",
        fontWeight: 400,
        color: "var(--text-primary)",
        margin: "0 0 8px 0",
      }}>
        {card.title}
      </h3>
      <p style={{
        fontFamily: "var(--sans)",
        fontSize: "0.85rem",
        fontWeight: 300,
        color: "var(--text-muted)",
        margin: 0,
        lineHeight: 1.5,
      }}>
        {card.description}
      </p>
    </div>
  );
}

export default function HomePage() {
  const greetingRef = useRef(null);
  const questionRef = useRef(null);
  const greetingVisible = useOnScreen(greetingRef);
  const questionVisible = useOnScreen(questionRef);

  return (
    <div style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      minHeight: "calc(100vh - 120px)",
      padding: "40px 24px",
      background: "radial-gradient(ellipse at 50% 40%, rgba(196,149,106,0.06) 0%, transparent 70%)",
    }}>
      <p
        ref={greetingRef}
        style={{
          fontFamily: "var(--serif)",
          fontSize: "clamp(1.1rem, 2.5vw, 1.35rem)",
          fontWeight: 300,
          color: "var(--text-muted)",
          margin: "0 0 12px 0",
          opacity: greetingVisible ? 1 : 0,
          transform: greetingVisible ? 'translateY(0)' : 'translateY(10px)',
          transition: "all 0.5s ease",
        }}
      >
        {getGreeting()}
      </p>

      <h1
        ref={questionRef}
        style={{
          fontFamily: "var(--serif)",
          fontSize: "clamp(1.8rem, 4.5vw, 2.8rem)",
          fontWeight: 300,
          color: "var(--text-primary)",
          margin: "0 0 48px 0",
          opacity: questionVisible ? 1 : 0,
          transform: questionVisible ? 'translateY(0)' : 'translateY(10px)',
          transition: "all 0.5s ease 200ms",
        }}
      >
        What's on your mind?
      </h1>

      <div style={{
        display: "flex",
        gap: "20px",
        flexWrap: "wrap",
        justifyContent: "center",
        maxWidth: "920px",
        width: "100%",
      }}>
        {cards.map((card, i) => (
          <WorkflowCard key={card.key} card={card} delay={400 + i * 120} />
        ))}
      </div>
    </div>
  );
}
