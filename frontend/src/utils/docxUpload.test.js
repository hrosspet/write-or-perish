import JSZip from "jszip";
import {
  stripDocxMedia, inMemoryCopy, FileReadError, describeAnalyzeError, localFileError, OLD_DOC_MESSAGE,
} from "./docxUpload";

async function makeDocx(entries, name = "a.docx") {
  const zip = new JSZip();
  Object.entries(entries).forEach(([n, d]) => zip.file(n, d));
  const blob = await zip.generateAsync({ type: "uint8array" });
  return new File([blob], name, { type: "application/octet-stream" });
}

const entries = {
  "[Content_Types].xml": "<Types/>",
  "word/document.xml": "<doc>hi</doc>",
  "word/_rels/document.xml.rels": "<Relationships>img</Relationships>",
  "word/media/image1.png": "PNGDATA",
  "word/media/image2.jpeg": "JPEGDATA",
};

test("stripDocxMedia removes word/media and keeps other entries unchanged", async () => {
  const out = await stripDocxMedia(await makeDocx(entries));
  expect(out.name).toBe("a.docx");
  const zip = await JSZip.loadAsync(out);
  expect(Object.keys(zip.files).filter((n) => n.startsWith("word/media/"))).toEqual([]);
  expect(await zip.file("word/document.xml").async("string")).toBe("<doc>hi</doc>");
  expect(await zip.file("word/_rels/document.xml.rels").async("string")).toBe(
    "<Relationships>img</Relationships>"
  );
  expect(await zip.file("[Content_Types].xml").async("string")).toBe("<Types/>");
});

const bytesOf = (f) =>
  new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(new Uint8Array(r.result));
    r.onerror = () => rej(r.error);
    r.readAsArrayBuffer(f);
  });

test("stripDocxMedia returns an in-memory copy when the file cannot be read as a zip", async () => {
  const bad = new File(["not a zip"], "a.docx", { type: "x/y" });
  const out = await stripDocxMedia(bad);
  expect(out).not.toBe(bad);
  expect(out.name).toBe("a.docx");
  expect(await bytesOf(out)).toEqual(await bytesOf(bad));
});

test("stripDocxMedia returns in-memory copies for zips and docx without media", async () => {
  const zipFile = await makeDocx(entries, "export.zip");
  const outZip = await stripDocxMedia(zipFile);
  expect(outZip).not.toBe(zipFile);
  expect(await bytesOf(outZip)).toEqual(await bytesOf(zipFile));
  const noMedia = await makeDocx({ "word/document.xml": "<doc/>" });
  const outDocx = await stripDocxMedia(noMedia);
  expect(outDocx).not.toBe(noMedia);
  expect(await bytesOf(outDocx)).toEqual(await bytesOf(noMedia));
});

test("stripDocxMedia throws FileReadError when the file cannot be read", async () => {
  const f = new File(["x"], "a.docx");
  f.arrayBuffer = () => Promise.reject(new DOMException("gone", "NotReadableError"));
  await expect(stripDocxMedia(f)).rejects.toBeInstanceOf(FileReadError);
});

test("inMemoryCopy keeps name, type and bytes in a new File", async () => {
  const f = new File(["%PDF-1.4"], "p.pdf", { type: "application/pdf" });
  const out = await inMemoryCopy(f);
  expect(out).not.toBe(f);
  expect(out).toBeInstanceOf(File);
  expect(out.name).toBe("p.pdf");
  expect(out.type).toBe("application/pdf");
  expect(await bytesOf(out)).toEqual(await bytesOf(f));
});

describe("describeAnalyzeError", () => {
  const withStatus = (status, data) => ({ response: { status, data } });
  test("keeps the backend error text", () => {
    expect(describeAnalyzeError(withStatus(400, { error: "a.docx: bad" }))).toBe("a.docx: bad");
    expect(describeAnalyzeError(withStatus(500, { error: "from server" }))).toBe("from server");
  });
  test("timeout", () => {
    expect(describeAnalyzeError({ code: "ECONNABORTED" })).toMatch(/took too long and was stopped/);
    expect(describeAnalyzeError({ code: "ETIMEDOUT" })).toMatch(/took too long and was stopped/);
  });
  test("no response", () => {
    expect(describeAnalyzeError({ code: "ERR_NETWORK" })).toMatch(/didn't reach Loore/);
  });
  test("status codes", () => {
    expect(describeAnalyzeError(withStatus(413))).toBe("This file is too big to upload.");
    expect(describeAnalyzeError(withStatus(504))).toMatch(/Converting this document took too long/);
    expect(describeAnalyzeError(withStatus(502))).toMatch(/try again in a minute/);
    expect(describeAnalyzeError(withStatus(503, "<html>"))).toMatch(/try again in a minute/);
    expect(describeAnalyzeError(withStatus(500, "<html>"))).toMatch(/Something went wrong while reading/);
    expect(describeAnalyzeError(withStatus(404))).toMatch(/Error analyzing import file/);
  });
  test("a PDF gets PDF advice where it differs, and the backend text still wins", () => {
    const pdf = new File(["%PDF"], "Paper.PDF");
    expect(describeAnalyzeError(withStatus(504), pdf)).toMatch(/Reading this PDF took too long/);
    expect(describeAnalyzeError(withStatus(500, "<html>"), pdf)).toMatch(/reading this PDF/);
    expect(describeAnalyzeError(withStatus(500, "<html>"), pdf)).not.toMatch(/\.docx/);
    expect(describeAnalyzeError({ code: "ECONNABORTED" }, pdf)).toMatch(/with a smaller PDF/);
    expect(describeAnalyzeError(withStatus(400, { error: "This PDF looks scanned" }), pdf))
      .toBe("This PDF looks scanned");
  });
});

test("localFileError: an old .doc is refused before upload", () => {
  expect(localFileError(new File(["x"], "Report.DOC"))).toBe(OLD_DOC_MESSAGE);
  expect(localFileError(new File(["x"], "Report.docx"))).toBeNull();
  expect(localFileError(new File(["x"], "paper.pdf"))).toBeNull();
});
