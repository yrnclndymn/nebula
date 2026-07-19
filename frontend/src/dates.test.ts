import { describe, it, expect } from "vitest";
import { whenLabel, signalWhen } from "./dates";

// Characterisation tests (#162) for the shared date labels (#152). Formatted
// output is locale/timezone dependent, so where a date parses we compare against
// the same `toLocaleDateString()` the module uses rather than a hard-coded string.
const ISO = "2026-03-14T12:00:00Z";
const localised = new Date(Date.parse(ISO)).toLocaleDateString();

describe("whenLabel", () => {
  it("returns null for falsy input", () => {
    expect(whenLabel(null)).toBe(null);
    expect(whenLabel(undefined)).toBe(null);
    expect(whenLabel("")).toBe(null);
  });

  it("returns the raw string when it can't be parsed", () => {
    expect(whenLabel("not a date")).toBe("not a date");
    expect(whenLabel("Q3 2025")).toBe("Q3 2025");
  });

  it("returns the localised date when parseable", () => {
    expect(whenLabel(ISO)).toBe(localised);
  });
});

describe("signalWhen", () => {
  it("returns null when no fields are present", () => {
    expect(signalWhen({})).toBe(null);
    expect(signalWhen({ publishedAt: null, publishedAtRaw: null, capturedAt: null })).toBe(null);
  });

  it("prefers a parseable publishedAt, localised", () => {
    expect(signalWhen({ publishedAt: ISO, publishedAtRaw: "raw", capturedAt: ISO })).toBe(localised);
  });

  it("falls back to publishedAtRaw when publishedAt is unparseable", () => {
    expect(signalWhen({ publishedAt: "garbage", publishedAtRaw: "last week" })).toBe("last week");
  });

  it("falls back to publishedAtRaw when publishedAt is absent", () => {
    expect(signalWhen({ publishedAtRaw: "sometime" })).toBe("sometime");
  });

  it("falls back to a 'captured' prefixed date when only capturedAt is set", () => {
    expect(signalWhen({ capturedAt: ISO })).toBe(`captured ${localised}`);
  });

  it("returns null when only an unparseable capturedAt is set", () => {
    expect(signalWhen({ capturedAt: "never" })).toBe(null);
  });
});
