import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useUser } from '../contexts/UserContext';

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
    key: "voice",
    path: "/voice",
    title: "Voice",
    description: "Speak what's present.",
    icon: (
      <img
        src="/loore-logo-transparent.svg"
        alt=""
        style={{ height: "42px", width: "auto" }}
      />
    ),
  },
  {
    key: "text",
    path: "/textmode",
    title: "Text",
    description: "Type what's on your mind.",
    icon: (
      <svg width="42" height="42" viewBox="0 0 42 42" fill="none">
        <path d="M8 12 C8 9.8 9.8 8 12 8 L30 8 C32.2 8 34 9.8 34 12 L34 24 C34 26.2 32.2 28 30 28 L16 28 L10 33 L10 28 L12 28 C9.8 28 8 26.2 8 24 Z"
              stroke="var(--accent)" strokeWidth="1.4" fill="none"/>
        <line x1="14" y1="15" x2="28" y2="15" stroke="var(--accent)" strokeWidth="1.4" strokeLinecap="round"/>
        <line x1="14" y1="20" x2="23" y2="20" stroke="var(--accent)" strokeWidth="1.4" strokeLinecap="round"/>
      </svg>
    ),
  },
];

function WorkflowCard({ card, delay }) {
  const ref = useRef(null);
  const isVisible = useOnScreen(ref);
  const navigate = useNavigate();
  const [hovered, setHovered] = useState(false);
  const [hasAnimated, setHasAnimated] = useState(false);

  useEffect(() => {
    if (isVisible && !hasAnimated) {
      const timer = setTimeout(() => setHasAnimated(true), delay + 600);
      return () => clearTimeout(timer);
    }
  }, [isVisible, hasAnimated, delay]);

  const disabled = card.disabled;

  return (
    <div
      ref={ref}
      onClick={() => !disabled && navigate(card.path)}
      onMouseEnter={() => !disabled && setHovered(true)}
      onMouseLeave={() => !disabled && setHovered(false)}
      style={{
        flex: "1 1 200px",
        background: "var(--bg-card)",
        border: `1px solid ${hovered ? 'var(--border-hover)' : 'var(--border)'}`,
        borderRadius: "12px",
        padding: "2.2rem 1.8rem 2rem",
        cursor: disabled ? "default" : "pointer",
        position: "relative",
        transform: isVisible
          ? `translateY(${hovered ? '-3px' : '0'})`
          : 'translateY(20px)',
        opacity: isVisible ? (disabled ? 0.4 : 1) : 0,
        transition: hasAnimated
          ? 'all 0.4s cubic-bezier(0.22, 1, 0.36, 1)'
          : `opacity 0.5s ease ${delay}ms, transform 0.5s ease ${delay}ms`,
        boxShadow: hovered ? '0 12px 48px rgba(0,0,0,0.35), 0 0 40px var(--accent-glow)' : 'none',
        pointerEvents: disabled ? 'none' : 'auto',
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
        display: "flex",
        alignItems: "center",
        gap: "0.6rem",
      }}>
        {card.title}
        {disabled && <span style={{
          fontFamily: "var(--sans)",
          fontSize: "0.65rem",
          fontWeight: 400,
          color: "var(--text-muted)",
          letterSpacing: "0.05em",
          textTransform: "uppercase",
        }}>Coming soon</span>}
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

const shareCard = {
  key: "share",
  path: "/share",
  title: "Share",
  description: "Give something outward.",
  icon: (
    <svg width="42" height="42" viewBox="0 0 42 42" fill="none">
      {/* A shining gem received on an open hand (side view, after Petr's
          reference: arm from lower-left, thumb arching over the palm,
          fingers extending right). */}
      <path d="M16 9 L26 9 L29.5 13.5 L21 21.5 L12.5 13.5 Z"
            stroke="var(--accent)" strokeWidth="1.3" fill="none" strokeLinejoin="round"/>
      <path d="M12.5 13.5 L29.5 13.5 M18.5 9 L18 13.5 L21 21.5 M23.5 9 L24 13.5 L21 21.5"
            stroke="var(--accent)" strokeWidth="1" fill="none" strokeLinejoin="round" opacity="0.75"/>
      <path d="M21 3.5 V6 M11 5 L13 7 M31 5 L29 7"
            stroke="var(--accent)" strokeWidth="1.1" strokeLinecap="round" opacity="0.7"/>
      <path d="M2.5 39 C5.5 37.5 8.5 36.2 10.5 34.4 C11.3 33.8 11.6 33 12.9 32.5 C15.5 31.4 19 31.2 24 30.9 C29 31.2 33.5 30 37.2 28.4 C38.3 27.9 38.3 26.7 37.2 26.5 C33 25.9 28 26.6 24.5 27.9 C24.2 27 24.9 26.1 24 25.7 C22.8 25.2 19.5 25.3 16.8 26.2 C14 27.1 11.8 28.5 10.3 30 C7.2 31.2 4.5 32.6 2.5 34"
            stroke="var(--accent)" strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M24.5 27.9 C21.5 27.9 18.5 27.9 16.2 28.3"
            stroke="var(--accent)" strokeWidth="1.4" strokeLinecap="round"/>
    </svg>
  ),
};

export default function HomePage() {
  const greetingRef = useRef(null);
  const questionRef = useRef(null);
  const greetingVisible = useOnScreen(greetingRef);
  const questionVisible = useOnScreen(questionRef);
  const { user } = useUser();
  const displayCards = user?.share_v1_enabled ? [...cards, shareCard] : cards;

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
        maxWidth: displayCards.length > 2 ? "880px" : "580px",
        width: "100%",
      }}>
        {displayCards.map((card, i) => (
          <WorkflowCard key={card.key} card={card} delay={400 + i * 120} />
        ))}
      </div>
    </div>
  );
}
