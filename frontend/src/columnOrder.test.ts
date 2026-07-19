import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { loadColumnOrder, saveColumnOrder } from "./columnOrder";

const ORDER_KEY = "nebula.columnOrder";

// Characterisation tests (#162) for the localStorage-backed column order.
// The node test env has no localStorage, so we stub a minimal in-memory one.
function makeStorage() {
  const store = new Map<string, string>();
  return {
    getItem: vi.fn((k: string) => (store.has(k) ? store.get(k)! : null)),
    setItem: vi.fn((k: string, v: string) => {
      store.set(k, v);
    }),
    removeItem: vi.fn((k: string) => {
      store.delete(k);
    }),
    clear: vi.fn(() => store.clear()),
  };
}

describe("columnOrder", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe("with a working localStorage", () => {
    let storage: ReturnType<typeof makeStorage>;
    beforeEach(() => {
      storage = makeStorage();
      vi.stubGlobal("localStorage", storage);
    });

    it("loads [] when nothing is stored", () => {
      expect(loadColumnOrder()).toEqual([]);
    });

    it("round-trips a saved order", () => {
      saveColumnOrder(["name", "headcount", "topic"]);
      expect(storage.setItem).toHaveBeenCalledWith(
        ORDER_KEY,
        JSON.stringify(["name", "headcount", "topic"]),
      );
      expect(loadColumnOrder()).toEqual(["name", "headcount", "topic"]);
    });

    it("returns [] and swallows the error on malformed JSON", () => {
      storage.getItem.mockReturnValueOnce("not json");
      expect(loadColumnOrder()).toEqual([]);
    });
  });

  it("loadColumnOrder returns [] when localStorage throws", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("unavailable");
      },
    });
    expect(loadColumnOrder()).toEqual([]);
  });

  it("saveColumnOrder swallows the error when localStorage throws", () => {
    vi.stubGlobal("localStorage", {
      setItem: () => {
        throw new Error("unavailable");
      },
    });
    expect(() => saveColumnOrder(["name"])).not.toThrow();
  });
});
