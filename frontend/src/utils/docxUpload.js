import JSZip from "jszip";

// Analyze requests carry a whole document, so they get a longer timeout than
// the 60 s default of the shared api client. 5 minutes is a heuristic.
export const ANALYZE_UPLOAD_TIMEOUT_MS = 5 * 60 * 1000;

const isDocx = (file) => /\.docx$/i.test(file?.name || "");

export const FILE_READ_MESSAGE =
  "Loore couldn't read this file from your phone. If it's in Google Drive or another cloud app, open it there, download it, and pick it from Downloads.";

// Thrown when the browser cannot read the picked file (for example a file
// backed by a cloud app on Android). No request is made in that case.
export class FileReadError extends Error {
  constructor(cause) {
    super(FILE_READ_MESSAGE);
    this.name = "FileReadError";
    this.cause = cause;
  }
}

// Blob.arrayBuffer() where it exists; FileReader for older browsers.
const readBytes = (file) =>
  typeof file.arrayBuffer === "function"
    ? file.arrayBuffer()
    : new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsArrayBuffer(file);
      });

const copyOf = (file, bytes) =>
  new File([bytes], file.name, { type: file.type });

// A File whose bytes are in memory. On Android Chrome a File picked from a
// cloud app (a Drive content URI) is read again at upload time and that
// second read can fail, so uploads send this copy instead of the original.
export async function inMemoryCopy(file) {
  return copyOf(file, await readBytes(file));
}

// Rebuild a .docx without its word/media/* entries (pictures). The server
// never reads image bytes (it writes a placeholder per image), so sending
// them only makes the upload slow. Every other entry, relationships
// included, is copied unchanged. When the file is not a .docx, has no
// media, or JSZip cannot read it (the server then reports its own specific
// error), the result is an in-memory copy of the original. The file is
// read once. Throws FileReadError when the browser cannot read it.
export async function stripDocxMedia(file) {
  let bytes;
  try {
    bytes = await readBytes(file);
  } catch (err) {
    throw new FileReadError(err);
  }
  if (!isDocx(file)) return copyOf(file, bytes);
  try {
    const zip = await JSZip.loadAsync(bytes);
    const media = Object.keys(zip.files).filter(
      (name) => name.startsWith("word/media/") && !zip.files[name].dir
    );
    if (media.length === 0) return copyOf(file, bytes);
    zip.remove("word/media"); // the folder entry and everything in it
    const blob = await zip.generateAsync({
      type: "blob",
      compression: "DEFLATE",
    });
    return new File([blob], file.name, { type: file.type });
  } catch {
    return copyOf(file, bytes);
  }
}

const isPdf = (file) => /\.pdf$/i.test(file?.name || "");

// Same text as the backend's MSG_OLD_DOC (backend/utils/pdf_import.py),
// which also reports a .doc inside a zip.
export const OLD_DOC_MESSAGE =
  "Old Word (.doc) files can't be imported. Open it in Word or Google Docs, save it as .docx, and upload that.";

// A message for a file Loore never imports, shown before uploading it.
export function localFileError(file) {
  if (/\.doc$/i.test(file?.name || "")) return OLD_DOC_MESSAGE;
  return null;
}

const SERVER_ERROR_TEXT =
  "Something went wrong while reading this file. You can try again, or save the document again as .docx and upload that copy.";
const PDF_SERVER_ERROR_TEXT =
  "Something went wrong while reading this PDF. You can try again, or save it again as a PDF (Print, then Save as PDF) and upload that copy.";

// User-facing message for a failed /import/analyze request. The backend's
// own `error` text wins whenever the response has one. `file` is the
// uploaded file; a PDF gets its own wording where the advice differs.
export function describeAnalyzeError(err, file) {
  const backendMsg = err?.response?.data?.error;
  if (backendMsg) return backendMsg;
  const pdf = isPdf(file);
  const status = err?.response?.status;
  if (!err?.response && (err?.code === "ECONNABORTED" || err?.code === "ETIMEDOUT")) {
    return pdf
      ? "The upload took too long and was stopped. Large files on a slow connection can do this. Try again on Wi-Fi, or with a smaller PDF."
      : "The upload took too long and was stopped. Large files on a slow connection can do this. Try again on Wi-Fi, or with a copy of the document without pictures.";
  }
  if (!status) {
    return "The file didn't reach Loore. Check your connection and try again. If the file is in Google Drive or another cloud app, download it to your phone first and pick it from Downloads.";
  }
  if (status === 413) return "This file is too big to upload.";
  if (status === 504) {
    return pdf
      ? "Reading this PDF took too long. Split it into smaller PDFs and upload them one by one."
      : "Converting this document took too long. Try splitting it into smaller documents.";
  }
  if (status === 502 || status === 503) return "Loore couldn't take the file just now. Please try again in a minute.";
  if (status >= 500) return pdf ? PDF_SERVER_ERROR_TEXT : SERVER_ERROR_TEXT;
  return "Error analyzing import file. Please try again.";
}
