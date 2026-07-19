import { describe, expect, it } from "vitest";
import { thesisMatchSummary } from "./acquirerReasons";

// Pure shaping test (#162 harness) for the #194 thesis-match reason string.
describe("thesisMatchSummary", () => {
  it("renders the kinds and a pluralized supporting-deal count", () => {
    expect(
      thesisMatchSummary({
        acquirer_kind: "service_provider",
        target_kind: "service_provider",
        evidence: 6,
      }),
    ).toBe("Service provider → Service provider (n=6 supporting deals)");
  });

  it("uses the singular for a single supporting deal", () => {
    expect(
      thesisMatchSummary({
        acquirer_kind: "cloud_provider",
        target_kind: "service_provider",
        evidence: 1,
      }),
    ).toBe("Cloud provider → Service provider (n=1 supporting deal)");
  });

  it("omits the parenthetical when a freshly-seeded rule has no evidence yet", () => {
    expect(
      thesisMatchSummary({ acquirer_kind: "service_provider", target_kind: "isv", evidence: 0 }),
    ).toBe("Service provider → ISV");
    // Absent evidence field behaves like zero.
    expect(thesisMatchSummary({ acquirer_kind: "service_provider", target_kind: "isv" })).toBe(
      "Service provider → ISV",
    );
  });

  it("falls back to the em-dash placeholder for an unknown/absent kind", () => {
    expect(thesisMatchSummary({ acquirer_kind: null, target_kind: "isv", evidence: 2 })).toBe(
      "— → ISV (n=2 supporting deals)",
    );
  });
});
