import api from "../api";

// Export data: the browser's own download of the file, which the server
// sends while it builds it (#591). It starts at once, shows in the
// browser's download list with its progress, and is never held in this
// page's memory. A HEAD request first checks that it can start (signed
// in, server reachable); the server builds nothing for it.
//
// Used by the craft menu and by the Terms dialog, where a person who
// doesn't accept new Terms can take their writing with them (#440).
// Resolves to false when the export couldn't start.
export async function startExportDownload() {
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
