"use client";
import { useEffect, useState } from "react";
import { Area, AreaChart, ResponsiveContainer } from "recharts";

type NS = {
  name: string;
  kind: string;
  model: string;
  status: "ok" | "warn" | "crit";
  rps: number;
  ttftMs: number;        // p95 TTFT (ms) for chat/agents/canary; full p95 for embed
  costPerMin: number;    // USD/min
  spark: { v: number }[];
};

const seedSpark = (vals: number[]) => vals.map(v => ({ v }));

const NAMESPACES: NS[] = [
  {
    name: "inference-chat", kind: "streaming chat", model: "gemma-7b-it",
    status: "warn", rps: 412, ttftMs: 1840, costPerMin: 0.42,
    spark: seedSpark([180,190,200,210,220,260,290,310,340,380,1100,1480,1700,1820,1840]),
  },
  {
    name: "inference-embed", kind: "embeddings", model: "text-embedding-005",
    status: "ok", rps: 920, ttftMs: 28, costPerMin: 0.06,
    spark: seedSpark([26,28,27,28,29,28,27,28,29,28,27,28,28,28,28]),
  },
  {
    name: "inference-agents", kind: "agentic", model: "gemini-2.5-flash",
    status: "crit", rps: 38, ttftMs: 720, costPerMin: 6.84,
    spark: seedSpark([0.4,0.4,0.5,0.4,0.5,0.6,0.5,0.5,1.2,2.4,3.8,4.9,5.7,6.5,6.84]),
  },
  {
    name: "inference-canary", kind: "model rollout", model: "gemma-7b-it-v2",
    status: "warn", rps: 64, ttftMs: 360, costPerMin: 0.11,
    spark: seedSpark([0.32,0.38,0.42,0.46,0.52,0.58,0.66,0.72,0.78,0.83,0.86,0.89,0.91,0.92,0.93]),
  },
];

const statusToken = {
  ok:   { pill: "pill-ok",   colour: "#5ce1a3", label: "stable"        },
  warn: { pill: "pill-warn", colour: "#ffb547", label: "investigating" },
  crit: { pill: "pill-crit", colour: "#ff5470", label: "incident"      },
};

const sparkLabel: Record<NS["name"], string> = {
  "inference-chat":    "TTFT (ms)",
  "inference-embed":   "p95 latency (ms)",
  "inference-agents":  "$ / min",
  "inference-canary":  "GPU pressure",
};

export default function NamespaceGrid() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const i = setInterval(() => setTick(t => t + 1), 4000);
    return () => clearInterval(i);
  }, []);

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {NAMESPACES.map(ns => {
        const s = statusToken[ns.status];
        return (
          <div
            key={ns.name}
            className="card p-5 hover:border-ink-600 transition-all duration-300"
            style={{ boxShadow: ns.status !== "ok" ? `0 0 0 1px ${s.colour}33` : undefined }}
          >
            <div className="flex items-start justify-between mb-3">
              <div>
                <div className="font-mono text-sm text-zinc-200">{ns.name}</div>
                <div className="text-xs text-zinc-500 mt-0.5">
                  {ns.kind} · <span className="text-zinc-400">{ns.model}</span>
                </div>
              </div>
              <span className={s.pill}>● {s.label}</span>
            </div>

            <div className="h-12 -mx-2 mb-1">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={ns.spark}>
                  <defs>
                    <linearGradient id={`g-${ns.name}`} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%"   stopColor={s.colour} stopOpacity={0.6}/>
                      <stop offset="100%" stopColor={s.colour} stopOpacity={0}  />
                    </linearGradient>
                  </defs>
                  <Area
                    type="monotone"
                    dataKey="v"
                    stroke={s.colour}
                    strokeWidth={1.5}
                    fill={`url(#g-${ns.name})`}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="font-mono text-[10px] text-zinc-600 -mt-1 mb-2 ml-1">
              {sparkLabel[ns.name]}
            </div>

            <div className="grid grid-cols-3 gap-3 text-xs">
              <Stat label="rps" value={ns.rps.toString()} />
              <Stat
                label={ns.kind === "embeddings" ? "p95" : "ttft"}
                value={`${ns.ttftMs}ms`}
                highlight={ns.status !== "ok"}
              />
              <Stat
                label="$ / min"
                value={`$${ns.costPerMin.toFixed(2)}`}
                highlight={ns.costPerMin > 1}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div>
      <div className="font-mono text-[10px] text-zinc-500 uppercase tracking-wider">{label}</div>
      <div className={`font-mono ${highlight ? "text-signal-warn" : "text-zinc-200"}`}>{value}</div>
    </div>
  );
}
