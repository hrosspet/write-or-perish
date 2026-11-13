import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import api from "../api";

// How often the screen asks whether the starting point is ready.
export const STARTING_POINT_POLL_MS = 3000;

/**
 * "Preparing your starting point…" (#698): shown after a Yes on "Start
 * from your tweets" when Loore had material prepared for the person's X
 * account (their public tweets and the profile drafted from them), while
 * it is loaded into their Loore. When it is ready the person is let in
 * and taken to their profile. Shown again after a reload while the load
 * runs (user.starting_point.status === "preparing").
 */
export default function StartingPointProgress({ style }) {
  const { setUser } = useUser();
  const navigate = useNavigate();
  const [status, setStatus] = useState("preparing");
  const done = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (done.current) return;
      try {
        const res = await api.get("/dashboard/starting-point");
        if (cancelled) return;
        const next = res.data.starting_point?.status;
        if (next === "ready" && res.data.approved) {
          done.current = true;
          const me = await api.get("/dashboard", { params: { profile: 0 } });
          if (cancelled) return;
          setUser(me.data.user);
          navigate("/profile");
        } else if (next === "failed") {
          done.current = true;
          setStatus("failed");
        }
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

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        background: "var(--bg-card)", border: "1px solid var(--border)",
        borderRadius: 12, padding: "2rem 2.2rem", maxWidth: 460,
        textAlign: "left", ...style,
      }}
    >
      <p style={{
        fontFamily: "var(--serif)", fontWeight: 300,
        fontSize: "1.35rem", lineHeight: 1.35,
        color: "var(--text-primary)", marginBottom: "0.8rem",
      }}>
        {status === "failed"
          ? "Your starting point isn't ready yet."
          : "Preparing your starting point…"}
      </p>
      <p style={{
        fontFamily: "var(--sans)", fontWeight: 300, fontSize: "0.92rem",
        lineHeight: 1.7, color: "var(--text-secondary)", margin: 0,
      }}>
        {status === "failed"
          ? "Something went wrong while bringing it in. We've been alerted and will set it up for you."
          : "Your public tweets and the profile drafted from them are on their way into your Loore. This can take a few minutes; your profile opens here when it's ready."}
      </p>
    </div>
  );
}
