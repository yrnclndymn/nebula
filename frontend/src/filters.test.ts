import { describe, it, expect } from "vitest";
import { headcountInRange } from "./filters";

// Characterisation tests (#162) for the headcount range predicate (#7).
// These pin CURRENT behaviour — they don't assert what it "should" do.
describe("headcountInRange", () => {
  it("keeps every row when both bounds are blank", () => {
    expect(headcountInRange(50, "", "")).toBe(true);
    expect(headcountInRange(null, "", "")).toBe(true);
    expect(headcountInRange(undefined, "", "")).toBe(true);
  });

  it("treats whitespace-only bounds as blank", () => {
    expect(headcountInRange(null, "   ", "  ")).toBe(true);
    expect(headcountInRange(5, " ", " ")).toBe(true);
  });

  it("excludes unknown headcount whenever any bound is set", () => {
    expect(headcountInRange(null, "10", "")).toBe(false);
    expect(headcountInRange(undefined, "", "200")).toBe(false);
    expect(headcountInRange(null, "10", "200")).toBe(false);
  });

  it("applies the lower bound inclusively", () => {
    expect(headcountInRange(10, "10", "")).toBe(true);
    expect(headcountInRange(9, "10", "")).toBe(false);
    expect(headcountInRange(11, "10", "")).toBe(true);
  });

  it("applies the upper bound inclusively", () => {
    expect(headcountInRange(200, "", "200")).toBe(true);
    expect(headcountInRange(201, "", "200")).toBe(false);
    expect(headcountInRange(199, "", "200")).toBe(true);
  });

  it("applies both bounds together", () => {
    expect(headcountInRange(50, "10", "200")).toBe(true);
    expect(headcountInRange(10, "10", "200")).toBe(true);
    expect(headcountInRange(200, "10", "200")).toBe(true);
    expect(headcountInRange(5, "10", "200")).toBe(false);
    expect(headcountInRange(500, "10", "200")).toBe(false);
  });

  it("ignores an unparseable bound (NaN is not treated as a bound)", () => {
    // "abc" -> NaN -> hasLo false, so with a blank hi every row passes.
    expect(headcountInRange(3, "abc", "")).toBe(true);
    expect(headcountInRange(null, "abc", "")).toBe(true);
    // A valid hi still applies even when lo is unparseable.
    expect(headcountInRange(300, "abc", "200")).toBe(false);
    expect(headcountInRange(100, "abc", "200")).toBe(true);
  });
});
