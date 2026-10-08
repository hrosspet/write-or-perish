import { breadcrumbsIntegration } from "@sentry/react";

// What the browser's Sentry may receive. No user content reaches Sentry
// (the rule behind #422, which did the same for the backend), and the
// browser SDK's defaults send some:
// - every event carries the page address with its query and fragment
//   (/confirm-email?token=…) and a Referer header with the previous page's;
// - xhr/fetch breadcrumbs keep each API call's address with its query
//   (/api/search?q=<the search words>), navigation breadcrumbs the
//   addresses moved from and to;
// - console breadcrumbs carry each console call's arguments, and some of
//   the app's logs quote content (the opening of a voice reply);
// - click and key-press breadcrumbs describe the element with its title,
//   aria-label and alt values, which can be a chapter title taken from a
//   reply, a link from a note, or the account's email address;
// - stack frames of the inlined webpack runtime are filed under the page
//   address, query included.
// Queries and fragments are removed and paths kept, so the route of an
// error stays readable. Console breadcrumbs are not recorded. An element is
// described by its tag, id and classes only.

export function withoutQuery(url) {
  return url.split("?", 1)[0].split("#", 1)[0];
}

const URL_FIELDS = ["url", "from", "to"];

// The SDK's own limits for an element description: the element and up to
// four ancestors, cut at 80 characters after the first.
const MAX_ELEMENTS = 5;
const MAX_DESCRIPTION_LENGTH = 80;

function describeElement(domEvent) {
  let node = domEvent && domEvent.target ? domEvent.target : domEvent;
  const parts = [];
  let length = 0;
  for (let height = 0; node && node.tagName && height < MAX_ELEMENTS; height++) {
    let part = node.tagName.toLowerCase();
    if (part === "html") break;
    if (node.id) part += `#${node.id}`;
    if (typeof node.className === "string") {
      for (const cls of node.className.split(/\s+/)) {
        if (cls) part += `.${cls}`;
      }
    }
    if (height > 0 && length + parts.length * 3 + part.length >= MAX_DESCRIPTION_LENGTH) {
      break;
    }
    parts.push(part);
    length += part.length;
    node = node.parentNode;
  }
  return parts.length ? parts.reverse().join(" > ") : "<unknown>";
}

export function scrubBreadcrumb(breadcrumb, hint) {
  if (!breadcrumb) return breadcrumb;
  if (breadcrumb.category === "console") return null;
  if (typeof breadcrumb.category === "string" && breadcrumb.category.startsWith("ui.")) {
    breadcrumb.message = describeElement(hint && hint.event);
  }
  const data = breadcrumb.data;
  if (data && typeof data === "object") {
    delete data["http.query"];
    delete data["http.fragment"];
    for (const key of URL_FIELDS) {
      if (typeof data[key] === "string") data[key] = withoutQuery(data[key]);
    }
  }
  return breadcrumb;
}

export function scrubEvent(event) {
  const request = event.request;
  if (request && typeof request === "object") {
    delete request.query_string;
    delete request.data;
    if (typeof request.url === "string") request.url = withoutQuery(request.url);
    const headers = request.headers;
    if (headers && typeof headers === "object") {
      for (const name of Object.keys(headers)) {
        if (name.toLowerCase() === "referer" && typeof headers[name] === "string") {
          headers[name] = withoutQuery(headers[name]);
        }
      }
    }
  }
  if (typeof event.transaction === "string") {
    event.transaction = withoutQuery(event.transaction);
  }
  const exceptions = event.exception && event.exception.values;
  for (const exception of Array.isArray(exceptions) ? exceptions : []) {
    const frames = exception && exception.stacktrace && exception.stacktrace.frames;
    for (const frame of Array.isArray(frames) ? frames : []) {
      for (const key of ["filename", "abs_path"]) {
        if (frame && typeof frame[key] === "string") frame[key] = withoutQuery(frame[key]);
      }
    }
  }
  return event;
}

export function sentryPrivacyOptions() {
  return {
    sendDefaultPii: false,
    beforeSend: scrubEvent,
    beforeBreadcrumb: scrubBreadcrumb,
    // Don't instrument the console at all; scrubBreadcrumb drops any
    // console breadcrumb that still arrives.
    integrations: [breadcrumbsIntegration({ console: false })],
  };
}
