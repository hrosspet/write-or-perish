import JSZip from "jszip";
import { stripDocxMedia, describeAnalyzeError } from "./docxUpload";

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

test("stripDocxMedia returns the original when the file cannot be read", async () => {
  const bad = new File(["not a zip"], "a.docx");
  expect(await stripDocxMedia(bad)).toBe(bad);
});

test("stripDocxMedia returns the original for zips and docx without media", async () => {
  const zipFile = await makeDocx(entries, "export.zip");
  expect(await stripDocxMedia(zipFile)).toBe(zipFile);
  const noMedia = await makeDocx({ "word/document.xml": "<doc/>" });
  expect(await stripDocxMedia(noMedia)).toBe(noMedia);
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
});
