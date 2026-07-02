import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { fetchDashboardGraph } from "./api";
import type { DashboardGraph, DashboardGraphEdge, DashboardGraphNode } from "./types";

type LayoutNode = {
  id: string;
  label: string;
  kind: string;
  tags: string[];
  weight: number;
  summary: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
};

type LayoutEdge = {
  id: string;
  sourceId: string;
  targetId: string;
  relation: string;
  weight: number;
};

const KIND_COLORS: Record<string, string> = {
  identity: "#7c5cff",
  memory: "#4ea1ff",
  interaction: "#ffd05c",
  runtime_config: "#ff8e9e",
  hypothesis: "#c89bff",
  entity: "#7cf6a8",
};

function colorForKind(kind: string): string {
  return KIND_COLORS[kind] ?? "#90a0c8";
}

type RefreshState = "idle" | "loading" | "ready" | "error";

const VIEW_WIDTH = 720;
const VIEW_HEIGHT = 480;

export default function GraphView() {
  const [graph, setGraph] = useState<DashboardGraph | null>(null);
  const [state, setState] = useState<RefreshState>("idle");
  const [error, setError] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [, forceRender] = useState({ tick: 0 });
  const layoutRef = useRef<{ nodes: LayoutNode[]; edges: LayoutEdge[] }>({
    nodes: [],
    edges: [],
  });
  const animationRef = useRef<{ frame: number | null; lastRenderAt: number }>({
    frame: null,
    lastRenderAt: 0,
  });
  const simulationRef = useRef<{ tick: number; maxTicks: number }>({
    tick: 0,
    maxTicks: 240,
  });

  const refresh = useCallback(async () => {
    setState((current) => (current === "ready" ? "ready" : "loading"));
    try {
      const payload = await fetchDashboardGraph(200, 400);
      setGraph(payload);
      setState("ready");
      setError(null);
    } catch (caught) {
      setState("error");
      setError(caught instanceof Error ? caught.message : "Failed to load graph");
    }
  }, []);

  // Build a fresh layout whenever a new graph arrives and reset the simulation.
  useEffect(() => {
    if (!graph) {
      return;
    }
    layoutRef.current = buildInitialLayout(graph);
    simulationRef.current = { tick: 0, maxTicks: 240 };
    // Nudge the first render so the SVG picks up the fresh nodes immediately.
    forceRender({ tick: 0 });
  }, [graph]);

  // Drive a brief force-directed simulation in the browser and stop cleanly.
  useEffect(() => {
    if (!graph || layoutRef.current.nodes.length === 0) {
      return;
    }
    let cancelled = false;

    function settleThreshold(nodes: LayoutNode[]): boolean {
      // If average energy is tiny, the layout has converged.
      if (nodes.length === 0) {
        return true;
      }
      let total = 0;
      for (const node of nodes) {
        total += Math.abs(node.vx) + Math.abs(node.vy);
      }
      return total / nodes.length < 0.05;
    }

    function step() {
      if (cancelled) {
        return;
      }
      const sim = simulationRef.current;
      const animation = animationRef.current;
      const layout = layoutRef.current;

      if (layout.nodes.length === 0 || sim.tick >= sim.maxTicks) {
        animation.frame = null;
        return;
      }

      runSimulationStep(layout.nodes, layout.edges);
      sim.tick += 1;

      // Throttle React renders to ~30 Hz so we don't churn the main thread
      // while still feeling smooth to the eye.
      const now =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      if (now - animation.lastRenderAt >= 33) {
        animation.lastRenderAt = now;
        forceRender({ tick: sim.tick });
      }

      if (settleThreshold(layout.nodes) && sim.tick > 30) {
        animation.frame = null;
        // One final render so the settled positions appear.
        forceRender({ tick: sim.tick });
        return;
      }

      animation.frame = window.requestAnimationFrame(step);
    }

    animationRef.current.lastRenderAt = 0;
    animationRef.current.frame = window.requestAnimationFrame(step);

    return () => {
      cancelled = true;
      if (animationRef.current.frame !== null) {
        window.cancelAnimationFrame(animationRef.current.frame);
        animationRef.current.frame = null;
      }
    };
  }, [graph]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const positionById = useMemo(() => {
    const map = new Map<string, LayoutNode>();
    for (const node of layoutRef.current.nodes) {
      map.set(node.id, node);
    }
    return map;
    // The layout ref mutates in place; we explicitly re-derive the lookup
    // whenever a new render is forced or a fresh graph arrives. The
    // exhaustive-deps rule is intentionally disabled here because reading
    // through the ref is exactly what gives the SVG the latest positions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  const hoveredNode = hoveredId ? positionById.get(hoveredId) ?? null : null;

  return (
    <article className="chart-card chart-card-wide">
      <header>
        <p className="eyebrow">Memory graph</p>
        <h3>
          CGI nodes & edges
          {graph
            ? ` · ${graph.total_nodes} nodes / ${graph.total_edges} edges`
            : ""}
        </h3>
      </header>

      <p className="muted">
        {state === "loading" && graph === null
          ? "Loading graph…"
          : graph
            ? `Generated ${graph.generated_at} · hover a node to inspect`
            : "Awaiting first load…"}
      </p>

      {error ? <p className="error-text">Graph failed: {error}</p> : null}

      {!graph || graph.nodes.length === 0 ? (
        <p className="empty">No CGI memory nodes recorded yet.</p>
      ) : (
        <div className="graph-wrap">
          <svg
            className="graph-svg"
            viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
            role="img"
            aria-label={`CGI memory graph: ${graph.total_nodes} nodes and ${graph.total_edges} edges`}
          >
            <defs>
              <radialGradient id="graph-glow" cx="50%" cy="50%" r="50%">
                <stop offset="0%" stopColor="rgba(124, 92, 255, 0.35)" />
                <stop offset="100%" stopColor="rgba(124, 92, 255, 0)" />
              </radialGradient>
            </defs>
            <rect
              x={0}
              y={0}
              width={VIEW_WIDTH}
              height={VIEW_HEIGHT}
              fill="url(#graph-glow)"
            />
            {layoutRef.current.edges.map((edge) => {
              const source = positionById.get(edge.sourceId);
              const target = positionById.get(edge.targetId);
              if (!source || !target) {
                return null;
              }
              const isHighlighted =
                hoveredId === source.id || hoveredId === target.id;
              return (
                <g key={edge.id} className={`graph-edge ${isHighlighted ? "hot" : ""}`}>
                  <line
                    x1={source.x}
                    y1={source.y}
                    x2={target.x}
                    y2={target.y}
                    stroke={isHighlighted ? "#f0f5ff" : "rgba(146, 162, 210, 0.45)"}
                    strokeWidth={Math.max(0.6, edge.weight * 1.6)}
                  />
                  <text
                    x={(source.x + target.x) / 2}
                    y={(source.y + target.y) / 2 - 4}
                    className="graph-edge-label"
                    textAnchor="middle"
                  >
                    {edge.relation}
                  </text>
                </g>
              );
            })}
            {layoutRef.current.nodes.map((node) => {
              const radius = 6 + Math.max(0, Math.min(node.weight, 1)) * 14;
              const isHovered = hoveredId === node.id;
              return (
                <g
                  key={node.id}
                  className={`graph-node ${isHovered ? "hot" : ""}`}
                  transform={`translate(${node.x.toFixed(1)}, ${node.y.toFixed(1)})`}
                  onMouseEnter={() => setHoveredId(node.id)}
                  onMouseLeave={() => setHoveredId(null)}
                >
                  <circle
                    r={radius}
                    fill={colorForKind(node.kind)}
                    opacity={isHovered ? 1 : 0.88}
                  />
                  <text
                    className="graph-node-label"
                    x={radius + 4}
                    y={4}
                    style={{
                      fontWeight: isHovered ? 700 : 500,
                      fill: isHovered ? "#f0f5ff" : "#cbd5ff",
                    }}
                  >
                    {truncate(node.label, 24)}
                  </text>
                </g>
              );
            })}
          </svg>

          {hoveredNode ? (
            <div className="graph-tooltip" role="tooltip">
              <p className="tooltip-label">{hoveredNode.label}</p>
              <p className="muted">
                <strong>Kind:</strong> {hoveredNode.kind}
              </p>
              <p className="muted">
                <strong>Weight:</strong> {hoveredNode.weight.toFixed(2)}
              </p>
              {hoveredNode.tags.length > 0 ? (
                <p className="muted">
                  <strong>Tags:</strong> {hoveredNode.tags.join(", ")}
                </p>
              ) : null}
              {hoveredNode.summary ? (
                <p className="muted summary">{hoveredNode.summary}</p>
              ) : null}
            </div>
          ) : null}

          <ul className="graph-legend">
            {Object.entries(KIND_COLORS).map(([kind, color]) => (
              <li key={kind}>
                <span className="legend-swatch" style={{ backgroundColor: color }} />
                <span className="legend-label">{kind}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </article>
  );
}

function buildInitialLayout(graph: DashboardGraph): {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
} {
  const cx = VIEW_WIDTH / 2;
  const cy = VIEW_HEIGHT / 2;
  const radius = Math.min(VIEW_WIDTH, VIEW_HEIGHT) / 2 - 60;
  const nodes: LayoutNode[] = graph.nodes.map((node, index) => {
    const angle = (index / Math.max(1, graph.nodes.length)) * Math.PI * 2;
    return {
      id: node.id,
      label: node.label,
      kind: node.kind,
      tags: node.tags,
      weight: node.weight,
      summary: node.summary,
      x: cx + Math.cos(angle) * radius * 0.55,
      y: cy + Math.sin(angle) * radius * 0.55,
      vx: 0,
      vy: 0,
    };
  });

  const labelToId = new Map<string, string>();
  for (const node of graph.nodes) {
    labelToId.set(node.label, node.id);
  }

  const edges: LayoutEdge[] = graph.edges
    .map((edge) => ({
      id: edge.id,
      sourceId:
        labelToId.get(edge.source_id) ??
        labelToId.get(edge.source_label) ??
        edge.source_id,
      targetId:
        labelToId.get(edge.target_id) ??
        labelToId.get(edge.target_label) ??
        edge.target_id,
      relation: edge.relation,
      weight: edge.weight,
    }))
    .filter((edge) => edge.sourceId !== edge.targetId);

  return { nodes, edges };
}

function runSimulationStep(nodes: LayoutNode[], edges: LayoutEdge[]): void {
  // Repulsive force between every node pair so things spread out naturally.
  const repulsion = 900;
  // Target edge length for the spring force.
  const restLength = 110;
  // Spring constant for the attractive edge force.
  const spring = 0.04;
  // Gentle pull toward the canvas center so disconnected clusters stay visible.
  const center = 0.012;
  // Velocity damping so the simulation eventually settles.
  const damping = 0.78;

  for (let i = 0; i < nodes.length; i += 1) {
    const a = nodes[i];
    let fx = -a.x + VIEW_WIDTH / 2;
    let fy = -a.y + VIEW_HEIGHT / 2;
    fx = (fx * center) * 80;
    fy = (fy * center) * 80;
    for (let j = 0; j < nodes.length; j += 1) {
      if (i === j) {
        continue;
      }
      const b = nodes[j];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const distSq = dx * dx + dy * dy + 0.01;
      const force = repulsion / distSq;
      const dist = Math.sqrt(distSq);
      fx += (dx / dist) * force;
      fy += (dy / dist) * force;
    }

    for (const edge of edges) {
      const sourceMatches = edge.sourceId === a.id;
      const targetMatches = edge.targetId === a.id;
      if (!sourceMatches && !targetMatches) {
        continue;
      }
      const other = nodes.find(
        (candidate) =>
          candidate.id === (sourceMatches ? edge.targetId : edge.sourceId),
      );
      if (!other) {
        continue;
      }
      const dx = other.x - a.x;
      const dy = other.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy + 0.01);
      const stretch = dist - restLength;
      const force = stretch * spring * (0.5 + edge.weight * 0.5);
      fx += (dx / dist) * force;
      fy += (dy / dist) * force;
    }

    a.vx = (a.vx + fx) * damping;
    a.vy = (a.vy + fy) * damping;
  }

  for (const node of nodes) {
    node.x += node.vx * 0.25;
    node.y += node.vy * 0.25;
    // Keep the simulation inside the viewBox with a comfortable margin.
    const margin = 36;
    if (node.x < margin) {
      node.x = margin;
      node.vx *= -0.4;
    } else if (node.x > VIEW_WIDTH - margin) {
      node.x = VIEW_WIDTH - margin;
      node.vx *= -0.4;
    }
    if (node.y < margin) {
      node.y = margin;
      node.vy *= -0.4;
    } else if (node.y > VIEW_HEIGHT - margin) {
      node.y = VIEW_HEIGHT - margin;
      node.vy *= -0.4;
    }
  }
}

function truncate(value: string, max: number): string {
  if (value.length <= max) {
    return value;
  }
  return `${value.slice(0, max - 1)}…`;
}

// Re-exported so the lint surface stays minimal when the file is consumed.
export type { DashboardGraph, DashboardGraphEdge, DashboardGraphNode };
