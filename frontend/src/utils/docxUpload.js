import JSZip from "jszip";

// Analyze requests carry a whole document, so they get a longer timeout than
// the 60 s default of the shared api client. 5 minutes is a heuristic.
export const ANALYZE_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000;

const isDocx = (file) => /\.docx$/i.test(file?.name || "");

// Rebuild a .docx without its word/media/* entries (pictures). The server
// never reads image bytes (it writes a placeholder per image), so sending
// them only makes the upload slow. Every other entry, relationships
// included, is copied unchanged. Returns the original file when it is not
// a .docx, has no media, or JSZip cannot read it (the server then reports
// its own specific error).
export async function stripDocxMedia(file) {
  if (!isDocx(file)) return file;
  try {
    const zip = await JSZip.loadAsync(file);
    const media = Object.keys(zip.files).filter(
      (name) => name.startsWith("word/media/") && !zip.files[name].dir
    );
    if (media.length === 0) return file;
    zip.remove("word/media"); // the folder entry and everything in it
    const blob = await zip.generateAsync({
      type: "blob",
      compression: "DEFLATE",
    });
    return new File([blob], file.name, { type: file.type });
  } catch {
    return file;
  }
}

const SERVER_ERROR_TEXT =
  "Something went wrong while reading this file. You can try again, or save the document again as .docx and upload that copy.";

// User-facing message for a failed /import/analyze request. The backend's
// own `error` text wins whenever the response has one.
export function describeAnalyzeError(err) {
  const backendMsg = err?.response?.data?.error;
  if (backendMsg) return backendMsg;
  const status = err?.response?.status;
  if (!err?.response && (err?.code === "ECONNABORTED" || err?.code === "ETIMEDOUT")) {
    return "The upload took too long and was stopped. Large files on a slow connection can do this. Try again on Wi-Fi, or with a copy of the document without pictures.";
  }
  if (!status) {
    return "The file didn't reach Loore. Check your connection and try again. If the file is in Google Drive or another cloud app, download it to your phone first and pick it from Downloads.";
  }
  if (status === 413) return "This file is too big to upload.";
  if (status === 504) return "Converting this document took too long. Try splitting it into smaller documents.";
  if (status === 502 || status === 503) return "Loore couldn't take the file just now. Please try again in a minute.";
  if (status >= 500) return SERVER_ERROR_TEXT;
  return "Error analyzing import file. Please try again.";
}
