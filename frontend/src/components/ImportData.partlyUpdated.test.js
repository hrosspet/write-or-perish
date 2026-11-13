import { partlyUpdatedNote } from "./ImportData";

// #638: a re-import that could set its settings only on the last entry of
// an older item says so in the import result.
describe("partlyUpdatedNote", () => {
  it("names one item", () => {
    expect(partlyUpdatedNote(1)).toBe(
      "1 item could be updated only in part: its last entry now has these "
      + "settings, but Loore could not confirm which entries before it "
      + "belong to the same document or message, so those kept their own. "
      + "You can change them on each entry.");
  });

  it("names several items", () => {
    expect(partlyUpdatedNote(3)).toMatch(
      /^3 items could be updated only in part: the last entry of each now/);
  });
});
