import React, { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useUser } from "../contexts/UserContext";
import { rememberInvite } from "../utils/invite";

// /invite/<code> (#439): keeps the code for the sign-in that follows and
// goes on to the landing page. It shows nothing about who sent the link.
// Someone already signed in was not brought by it: the code is dropped.
export default function InvitePage() {
  const { code } = useParams();
  const { user, loading } = useUser();
  const [done, setDone] = useState(false);

  useEffect(() => {
    if (loading) return;
    if (!user) rememberInvite(code);
    setDone(true);
  }, [code, user, loading]);

  if (!done) return null;
  return <Navigate to={user ? "/" : "/landing"} replace />;
}
