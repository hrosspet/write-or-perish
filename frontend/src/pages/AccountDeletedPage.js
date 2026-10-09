import React from "react";
import { Link, useSearchParams } from "react-router-dom";
import { formatDeletionDate } from "../utils/dataDeletion";
import { headingStyle, linkStyle, pageStyle, textStyle } from "./accountPageStyles";

// After a deletion is scheduled (#269): the session has ended, so the
// date comes in the address.
export default function AccountDeletedPage() {
  const [searchParams] = useSearchParams();
  const date = formatDeletionDate(searchParams.get("on"));
  return (
    <div style={pageStyle}>
      <h1 style={headingStyle}>Your account is deleted</h1>
      <p role="status" style={textStyle}>
        You are signed out.{" "}
        {date
          ? <>If you change your mind, you can still restore it by signing in
            until {date}; after that it is deleted forever, with everything
            in it.</>
          : <>If you change your mind, you can still restore it by signing in
            within 30 days; after that it is deleted forever, with
            everything in it.</>}
      </p>
      <Link to="/" style={linkStyle}>Back to Loore &rarr;</Link>
    </div>
  );
}
