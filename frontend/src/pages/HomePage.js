import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { FaRegCompass } from 'react-icons/fa';

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
        style={{ height: "42px", width: "auto" }}
      />
    ),
  },
  {
    key: "orient",
    path: "/orient",
    title: "Orient",
    description: "Ground your day. See what matters.",
    icon: (
      <FaRegCompass size={42} color="var(--accent)" />
    ),
  },
  {
    key: "converse",
    path: "/converse",
    title: "Converse",
    description: "Ask anything. Think out loud.",
    icon: (
      <svg width="42" height="42" viewBox="0 0 42 42" fill="none">
        <path d="M8 12 C8 9.8 9.8 8 12 8 L30 8 C32.2 8 34 9.8 34 12 L34 24 C34 26.2 32.2 28 30 28 L16 28 L10 33 L10 28 L12 28 C9.8 28 8 26.2 8 24 Z"
              stroke="#c4956a" strokeWidth="1.2" opacity="0.4" fill="none"/>
        <line x1="14" y1="15" x2="28" y2="15" stroke="#c4956a" strokeWidth="1.2" strokeLinecap="round" opacity="0.3"/>
        <line x1="14" y1="20" x2="23" y2="20" stroke="#c4956a" strokeWidth="1.2" strokeLinecap="round" opacity="0.3"/>
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
        background: "var(--bg-card)",
        border: `1px solid ${hovered ? 'var(--border-hover)' : 'var(--border)'}`,
        borderRadius: "12px",
        padding: "2.2rem 1.8rem 2rem",
        cursor: "pointer",
        position: "relative",
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

      <div style={{
        width: "48px", height: "48px", marginBottom: "1.5rem",
        display: "flex", alignItems: "center", justifyContent: "center",
        opacity: hovered ? 1 : 0.7, transition: "opacity 0.3s",
      }}>
        {card.icon}
      </div>
      <h3 style={{
        fontFamily: "var(--serif)",
        fontSize: "1.5rem",
        fontWeight: 400,
        color: "var(--text-primary)",
        margin: "0 0 0.7rem 0",
      }}>
        {card.title}
      </h3>
      <p style={{
        fontFamily: "var(--sans)",
        fontSize: "0.88rem",
        fontWeight: 300,
        color: "var(--text-muted)",
        margin: 0,
        lineHeight: 1.65,
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
        gap: "1.5rem",
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
