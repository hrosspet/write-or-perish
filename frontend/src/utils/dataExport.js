import api from "../api";

// The account's data as one file: the browser's own download of it, which
// the server sends while it builds it (#591). It starts at once, shows in
// the browser's download list with its progress, and is never held in this
// page's memory. A HEAD request first checks that it can start (signed
// in, server reachable); the server builds nothing for it. Used by the
// menu's "Export data" and, for an account that is not let in (#438), by
// the subscribe page's "Download your data". Resolves false when the
// download could not start.
export async function downloadAccountData() {
  try {
    await api.head("/export/threads");
  } catch (err) {
    return false;
  }
  const a = document.createElement("a");
  a.href = `${api.defaults.baseURL}/export/threads`;
  a.download = ""; // the server names the file (loore-export-YYYY-MM-DD.txt)
  document.body.appendChild(a);
  a.click();
  a.remove();
  return true;
}

export const EXPORT_FAILED_MESSAGE = "The export couldn't start. Please try again.";
