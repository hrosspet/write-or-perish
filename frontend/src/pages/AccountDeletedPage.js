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
      <h1 style={headingStyle}>Your account is scheduled for deletion</h1>
      <p role="status" style={textStyle}>
        It is hidden now, and you are signed out.{" "}
        {date
          ? <>On {date} it is deleted with everything in it. Until then you
            can restore it by signing in.</>
          : <>30 days after your request it is deleted with everything in
            it. Until then you can restore it by signing in.</>}
      </p>
      <Link to="/" style={linkStyle}>Back to Loore &rarr;</Link>
    </div>
  );
}
