import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";

// How often the screen asks whether the starting point is ready.
export const STARTING_POINT_POLL_MS = 3000;

/**
 * "Preparing your starting point…" (#698). After a Yes on "Start from
 * your tweets" the server looks, in the background, for material Loore
 * prepared for the person's X account (their public tweets and the
 * profile drafted from them):
 *  - "checking": it is still looking; the card shows its usual line
 *    (`noted`) and keeps asking;
 *  - "preparing": it found some and is loading it;
 *  - "ready": loaded; the person is let in and taken to their profile;
 *  - "failed": the load stopped; the card says so;
 *  - nothing: there was none; the usual line stays.
 * Shown again after a reload while it runs (user.starting_point).
 */
export default function StartingPointProgress({ initial, noted, style }) {
  const { setUser } = useUser();
  const navigate = useNavigate();
  const [status, setStatus] = useState(initial || "checking");
  const done = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (done.current) return;
      try {
        const res = await api.get("/dashboard/starting-point");
        if (cancelled) return;
        const next = res.data.starting_point?.status || "none";
        if (next === "ready" && res.data.approved) {
          done.current = true;
          const me = await api.get("/dashboard", { params: { profile: 0 } });
          if (cancelled) return;
          setUser(me.data.user);
          navigate("/profile");
          return;
        }
        if (next === "failed" || next === "none" || next === "ready") {
          done.current = true;
        }
        setStatus(next);
      } catch (err) {
        // A missed poll is retried on the next tick.
        console.warn("Starting point check failed:", err);
      }
    };
    const timer = setInterval(tick, STARTING_POINT_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [navigate, setUser]);

  const card = {
    background: "var(--bg-card)", border: "1px solid var(--border)",
    borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
    textAlign: "left", ...style,
  };
  const body = {
    fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
    lineHeight: 1.7, color: "var(--text-secondary)", margin: 0,
  };

  if (status === "checking" || status === "none") {
    if (!noted) return null;
    return (
      <div role="status" aria-live="polite" style={card}>
        <p style={body}>{noted}</p>
      </div>
    );
  }

  const title = {
    failed: "Your starting point isn't ready yet.",
    ready: "Your starting point is ready.",
  }[status] || "Preparing your starting point…";
  const text = {
    failed: "Something went wrong while bringing it in. We've been alerted and will set it up for you.",
    ready: "Your public tweets and the profile drafted from them are in your Loore.",
  }[status] || "Your public tweets and the profile drafted from them are on their way into your Loore. This can take a few minutes; your profile opens here when it's ready.";

  return (
    <div role="status" aria-live="polite" style={card}>
      <p style={{
        fontFamily: "var(--serif)", fontWeight: 300,
        fontSize: "1.35rem", lineHeight: 1.35,
        color: "var(--text-primary)", marginBottom: "0.8rem",
      }}>
        {title}
      </p>
      <p style={body}>{text}</p>
    </div>
  );
}
