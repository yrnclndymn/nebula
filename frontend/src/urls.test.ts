import { describe, it, expect } from "vitest";
import { isHttpUrl } from "./urls";

// Characterisation tests (#162) for isHttpUrl — the http(s)-only link guard
// against hostile javascript:/data: values from untrusted crawled content (#121).
describe("isHttpUrl", () => {
  it("is false for falsy input", () => {
    expect(isHttpUrl(null)).toBe(false);
    expect(isHttpUrl(undefined)).toBe(false);
    expect(isHttpUrl("")).toBe(false);
  });

  it("is true for http and https URLs", () => {
    expect(isHttpUrl("http://example.com")).toBe(true);
    expect(isHttpUrl("https://example.com/path?q=1")).toBe(true);
    expect(isHttpUrl("HTTPS://EXAMPLE.COM")).toBe(true);
  });

  it("is false for non-http schemes", () => {
    expect(isHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isHttpUrl("data:text/html,<script>1</script>")).toBe(false);
    expect(isHttpUrl("ftp://example.com")).toBe(false);
    expect(isHttpUrl("mailto:a@b.com")).toBe(false);
  });

  it("is false for a string the URL constructor can't parse", () => {
    expect(isHttpUrl("example.com")).toBe(false);
    expect(isHttpUrl("not a url")).toBe(false);
    expect(isHttpUrl("/relative/path")).toBe(false);
  });
});
