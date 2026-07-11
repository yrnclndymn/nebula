// Interactive graph view (issue #50).
//
// Library: react-force-graph-2d (MIT). Chosen over Cytoscape.js and sigma.js:
//   - React fit: it *is* a React component (props + ref), so it drops straight
//     into this SPA with no imperative-wrapper glue (Cytoscape's React binding is
//     a thin wrapper; sigma needs a separate graphology data layer).
//   - Bundle: canvas-based via `force-graph`; we import the 2d entrypoint so no
//     three.js/WebGL is pulled in. Comparable to Cytoscape, lighter than a
//     sigma+graphology stack for our needs.
//   - Fit for the data: canvas + d3-force comfortably handles the working set.
//     We never render all ~700 nodes — the view starts from one company and
//     lazily fetches each node's neighbourhood on click (GET /companies/{n}/graph).
//   - Licence: MIT, same permissive terms as the alternatives.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import type { ForceGraphMethods, LinkObject, NodeObject } from "react-force-graph-2d";
import { fetchCompanyGraph } from "./api";
import type { CompanyGraph } from "./types";

type GNode = {
  id: string;
  kind: string;
  name: string;
  companyKind: string | null;
  website: string | null;
  researched: boolean;
  expanded?: boolean;
};
type GLink = { source: string; target: string; type: string };

// Node fill by label kind; researched companies stand out from partner/client stubs.
const NODE_COLOR: Record<string, string> = {
  CompanyResearched: "#4f46e5",
  CompanyStub: "#c7cbe6",
  Person: "#059669",
  Topic: "#d97706",
  CompanyType: "#64748b",
};

const EDGE_COLOR: Record<string, string> = {
  PARTNERS_WITH: "#059669",
  HAS_CLIENT: "#4f46e5",
  LEADS: "#d97706",
  TAGGED_AS: "#b4bccb",
  CLASSIFIED_AS: "#94a3b8",
};

const LEGEND: { color: string; label: string }[] = [
  { color: NODE_COLOR.CompanyResearched, label: "Company (researched)" },
  { color: NODE_COLOR.CompanyStub, label: "Company (stub)" },
  { color: NODE_COLOR.Person, label: "Person" },
  { color: NODE_COLOR.Topic, label: "Topic" },
  { color: NODE_COLOR.CompanyType, label: "Type" },
];

function nodeColor(n: GNode): string {
  if (n.kind === "Company") return n.researched ? NODE_COLOR.CompanyResearched : NODE_COLOR.CompanyStub;
  return NODE_COLOR[n.kind] ?? "#94a3b8";
}

function nodeRadius(n: GNode): number {
  if (n.kind === "Company") return n.researched ? 6 : 4.5;
  if (n.kind === "Person") return 5;
  return 4.5;
}

// Links arrive with string endpoints; the force sim swaps them for node refs.
function endId(v: string | number | NodeObject<GNode> | undefined): string {
  if (v && typeof v === "object") return String((v as GNode).id);
  return String(v);
}

export function GraphView({
  seed,
  onClose,
  onOpenCompany,
}: {
  seed: string | null;
  onClose: () => void;
  onOpenCompany: (name: string) => void;
}) {
  const [data, setData] = useState<{ nodes: GNode[]; links: GLink[] }>({ nodes: [], links: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<NodeObject<GNode>, LinkObject<GNode, GLink>> | undefined>(
    undefined,
  );
  const expanded = useRef<Set<string>>(new Set());
  const fitted = useRef(false);

  // Merge a fetched neighbourhood, reusing existing node objects so the running
  // simulation keeps their positions instead of resetting.
  const merge = useCallback((g: CompanyGraph, expandedId?: string) => {
    setData((prev) => {
      const nodes = new Map(prev.nodes.map((n) => [n.id, n]));
      for (const n of g.nodes) {
        if (!nodes.has(n.id)) nodes.set(n.id, { ...n });
      }
      if (expandedId) {
        const ex = nodes.get(expandedId);
        if (ex) ex.expanded = true;
      }
      const links = new Map(prev.links.map((l) => [`${endId(l.source)}|${endId(l.target)}|${l.type}`, l]));
      for (const e of g.edges) {
        const key = `${e.source}|${e.target}|${e.type}`;
        if (!links.has(key)) links.set(key, { source: e.source, target: e.target, type: e.type });
      }
      return { nodes: [...nodes.values()], links: [...links.values()] };
    });
  }, []);

  // Seed (or re-seed) the graph when the entry-point company changes.
  useEffect(() => {
    if (!seed) return;
    let cancelled = false;
    expanded.current = new Set();
    fitted.current = false;
    setData({ nodes: [], links: [] });
    setError(null);
    setLoading(true);
    fetchCompanyGraph(seed)
      .then((g) => {
        if (cancelled) return;
        expanded.current.add(g.center);
        merge(g, g.center);
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [seed, merge]);

  // Track the container's real pixel size so the canvas fills the panel.
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const handleNodeClick = useCallback(
    (node: NodeObject<GNode>) => {
      const n = node as GNode;
      if (n.kind !== "Company") return;
      onOpenCompany(n.name); // acceptance: clicking a company node opens its drawer
      if (expanded.current.has(n.id)) return; // already expanded — no refetch
      expanded.current.add(n.id);
      fetchCompanyGraph(n.name)
        .then((g) => merge(g, n.id))
        .catch(() => {
          /* a stub with no researched record — nothing more to expand */
        });
    },
    [merge, onOpenCompany],
  );

  const drawNode = useCallback(
    (node: NodeObject<GNode>, ctx: CanvasRenderingContext2D, scale: number) => {
      const n = node as GNode;
      const r = nodeRadius(n);
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = nodeColor(n);
      ctx.fill();
      if (n.kind === "Company" && !n.researched) {
        ctx.lineWidth = 1 / scale;
        ctx.strokeStyle = "#9aa0d8";
        ctx.stroke();
      }
      // Labels appear once zoomed in enough to stay legible.
      if (scale > 1.2) {
        const label = n.name.length > 26 ? `${n.name.slice(0, 25)}…` : n.name;
        const fontSize = 11 / scale;
        ctx.font = `${fontSize}px system-ui, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.lineWidth = 3 / scale;
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.strokeText(label, x, y + r + 1 / scale);
        ctx.fillStyle = "#1a1d23";
        ctx.fillText(label, x, y + r + 1 / scale);
      }
    },
    [],
  );

  const drawNodeHit = useCallback(
    (node: NodeObject<GNode>, color: string, ctx: CanvasRenderingContext2D) => {
      const n = node as GNode;
      ctx.beginPath();
      ctx.arc(node.x ?? 0, node.y ?? 0, nodeRadius(n) + 2, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
    },
    [],
  );

  const linkColor = useCallback((l: LinkObject<GNode, GLink>) => EDGE_COLOR[l.type] ?? "#cbd5e1", []);

  const onEngineStop = useCallback(() => {
    if (fitted.current || data.nodes.length === 0) return;
    fitted.current = true;
    fgRef.current?.zoomToFit(400, 50);
  }, [data.nodes.length]);

  const nodeCount = data.nodes.length;
  const seedName = useMemo(() => seed ?? "", [seed]);

  return (
    <div className="graph-overlay">
      <div className="graph-topbar">
        <div className="graph-title">
          Graph <span className="muted">— {seedName || "select a company"}</span>
        </div>
        <div className="graph-legend">
          {LEGEND.map((it) => (
            <span key={it.label} className="graph-legend-item">
              <span className="graph-dot" style={{ background: it.color }} />
              {it.label}
            </span>
          ))}
        </div>
        <div className="graph-actions">
          <span className="count">{nodeCount ? `${nodeCount} nodes` : loading ? "loading…" : ""}</span>
          <button className="chat-toggle" onClick={() => fgRef.current?.zoomToFit(400, 50)}>
            Fit
          </button>
          <button className="chat-toggle" onClick={onClose}>
            ✕ Close
          </button>
        </div>
      </div>
      <div className="graph-canvas" ref={wrapRef}>
        {error && <div className="graph-msg error">{error}</div>}
        {!seed && !error && (
          <div className="graph-msg">
            Open a company and choose “View in graph”, or use the Graph button in the top bar.
          </div>
        )}
        {seed && (
          <ForceGraph2D<GNode, GLink>
            ref={fgRef}
            graphData={data}
            width={size.w}
            height={size.h}
            nodeId="id"
            nodeRelSize={4}
            nodeLabel={(n) => `${(n as GNode).name} · ${(n as GNode).kind}`}
            nodeCanvasObject={drawNode}
            nodePointerAreaPaint={drawNodeHit}
            linkColor={linkColor}
            linkWidth={1}
            linkLabel={(l) => (l as GLink).type}
            linkDirectionalArrowLength={3}
            linkDirectionalArrowRelPos={1}
            onNodeClick={handleNodeClick}
            cooldownTicks={80}
            onEngineStop={onEngineStop}
          />
        )}
        <div className="graph-hint">Click a company to expand its connections and open details · scroll to zoom · drag to pan</div>
      </div>
    </div>
  );
}
