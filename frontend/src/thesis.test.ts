import { describe, it, expect } from "vitest";
import { humanizeKind, thesisPair, originLabel, confidenceLabel } from "./thesis";

// Pure-shaping tests (#195) for the Market-thesis panel's label helpers. Abstract
// kinds only (public-repo rule).

describe("humanizeKind", () => {
  it("snake-cases into spaced, capitalised words", () => {
    expect(humanizeKind("cloud_provider")).toBe("Cloud provider");
    expect(humanizeKind("service_provider")).toBe("Service provider");
  });

  it("upper-cases acronym kinds", () => {
    expect(humanizeKind("isv")).toBe("ISV");
    expect(humanizeKind("ISV")).toBe("ISV");
  });

  it("falls back to an em-dash on empty input", () => {
    expect(humanizeKind("")).toBe("—");
    expect(humanizeKind("   ")).toBe("—");
  });
});

describe("thesisPair", () => {
  it("renders the acquirer→target shape", () => {
    expect(thesisPair({ acquirer_kind: "cloud_provider", target_kind: "service_provider" })).toBe(
      "Cloud provider → Service provider",
    );
    expect(thesisPair({ acquirer_kind: "service_provider", target_kind: "isv" })).toBe(
      "Service provider → ISV",
    );
  });
});

describe("originLabel", () => {
  it("maps the restricted vocab", () => {
    expect(originLabel("user")).toBe("Maintainer");
    expect(originLabel("reviewer")).toBe("Reviewer");
  });

  it("capitalises unknowns and em-dashes empties", () => {
    expect(originLabel("import")).toBe("Import");
    expect(originLabel(null)).toBe("—");
    expect(originLabel("")).toBe("—");
  });
});

describe("confidenceLabel", () => {
  it("renders a whole-percent label", () => {
    expect(confidenceLabel(0.75)).toBe("75%");
    expect(confidenceLabel(0.5)).toBe("50%");
    expect(confidenceLabel(0)).toBe("0%");
    expect(confidenceLabel(1)).toBe("100%");
  });

  it("clamps out-of-range and non-finite values", () => {
    expect(confidenceLabel(1.5)).toBe("100%");
    expect(confidenceLabel(-0.2)).toBe("0%");
    expect(confidenceLabel(null)).toBe("0%");
    expect(confidenceLabel(undefined)).toBe("0%");
    expect(confidenceLabel(NaN)).toBe("0%");
  });
});
