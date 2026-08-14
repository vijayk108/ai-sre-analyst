"use client";
import { useState } from "react";
import { ChevronDown, ChevronRight, Brain, Wrench, BookOpen, Clock, FileSearch, Sliders } from "lucide-react";

type EvidenceRow = {
  source: "alert" | "k8s_event" | "deployment" | "log" | "metric";
  ts?: string;
  observation: string;
  weight: number;
};

type TimelineRow = {
  ts: string;
  source: "alert" | "k8s_event" | "deployment" | "log" | "metric";
  summary: string;
  severity: "info" | "warning" | "error" | "critical";
  correlationScore?: number;
};

type Incident = {
  id: string;
  ago: string;
  ns: string;
  alert: string;
  tier: "1_rule" | "3_llm";
  confidence: number;
  probableCause: string;
  recommendedAction: string;
  evidence: EvidenceRow[];
  blastRadius: string[];
  steps: string[];
  runbooks: string[];
  timeline: TimelineRow[];
  severity: "warn" | "crit";
  routingRecs: RoutingRec[];
  isLive?: boolean;
};

// --- Live API types (Firestore incident document shape) -------------------

type ApiEvidence = {
  source: EvidenceRow["source"];
  ts?: string;
  observation: string;
  weight: number;
};

type ApiTimelineEvent = {
  ts: string;
  source: TimelineRow["source"];
  summary: string;
  severity: TimelineRow["severity"];
  correlation_score?: number;
};

type ApiRunbookRef = {
  title: string;
  source: string;
  score?: number;
};

type ApiRoutingRecommendation = {
  parameter: string;
  current_value?: string | null;
  recommended_value: string;
  unit?: string | null;
  reason: string;
};

type RoutingRec = {
  parameter: string;
  currentValue?: string;
  recommendedValue: string;
  unit?: string;
  reason: string;
};

type ApiVerdict = {
  tier: "1_rule" | "3_llm";
  model: string;
  probable_cause: string;
  confidence: number;
  recommended_action: string;
  remediation_steps: string[];
  evidence: ApiEvidence[];
  blast_radius: string[];
  timeline?: ApiTimelineEvent[];
  runbook_refs?: ApiRunbookRef[];
  routing_recommendations?: ApiRoutingRecommendation[];
  generated_at: string;
};

export type ApiIncident = {
  incident_id: string;
  namespace: string;
  severity: string;
  status: string;
  opened_at: string;
  current_confidence: number;
  verdicts: ApiVerdict[];
  feedback: unknown[];
};

function agoFromIso(isoStr: string): string {
  const mins = Math.floor((Date.now() - new Date(isoStr).getTime()) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  return `${h} hour${h !== 1 ? "s" : ""} ago`;
}

function extractAlertName(evidence: ApiEvidence[]): string {
  const row = evidence.find(e => e.source === "alert");
  if (row) {
    const m = row.observation.match(/Alert (\S+) fired/);
    if (m) return m[1];
  }
  return "Open Incident";
}

function mapApiToIncident(api: ApiIncident): Incident {
  const v = api.verdicts[api.verdicts.length - 1];
  const timeline: TimelineRow[] = (v?.timeline ?? []).map(t => ({
    ts: t.ts,
    source: t.source,
    summary: t.summary,
    severity: t.severity,
    correlationScore: t.correlation_score,
  }));
  const runbooks: string[] = (v?.runbook_refs ?? []).map(r =>
    r.source ? `${r.title} :: ${r.source}` : r.title
  );
  const routingRecs: RoutingRec[] = (v?.routing_recommendations ?? []).map(r => ({
    parameter: r.parameter,
    currentValue: r.current_value ?? undefined,
    recommendedValue: r.recommended_value,
    unit: r.unit ?? undefined,
    reason: r.reason,
  }));
  return {
    id: api.incident_id,
    ago: agoFromIso(api.opened_at),
    ns: api.namespace,
    alert: v ? extractAlertName(v.evidence) : "Open Incident",
    tier: v?.tier ?? "3_llm",
    confidence: api.current_confidence,
    severity: api.severity === "critical" ? "crit" : "warn",
    probableCause: v?.probable_cause ?? "—",
    recommendedAction: v?.recommended_action ?? "—",
    evidence: v?.evidence ?? [],
    blastRadius: v?.blast_radius ?? [],
    steps: v?.remediation_steps ?? [],
    runbooks,
    timeline,
    routingRecs,
    isLive: true,
  };
}

const INCIDENTS: Incident[] = [
  {
    id: "inc-2026-04-28-1843",
    ago: "2 min ago",
    ns: "inference-agents",
    alert: "InferenceCostSpike",
    tier: "3_llm",
    confidence: 0.93,
    severity: "crit",
    probableCause:
      "Output token rate is 8.7× the 2h baseline while RPS is unchanged — this is per-request output growth, not traffic growth. The agents-runtime release at 18:39 UTC removed the orchestration-layer recursion guard. Per-call max_tokens caps each LLM call but does not bound the loop, so the planner is calling itself before terminating. Cost trajectory is roughly $7/min above baseline.",
    recommendedAction:
      "Stop the bleeding first: lower max_tokens from 8192 → 1024 to cap each call. Then helm rollback agents-runtime to v0.41 (the version before the prompt change). Add a recursion-depth guard in the orchestration layer before re-deploying.",
    evidence: [
      { source: "deployment", ts: "18:39:12Z", observation: "agents-runtime v0.42 rolled in inference-agents namespace (3 minutes before alert)", weight: 1.0 },
      { source: "metric",     ts: "18:42:05Z", observation: "inference_tokens_out_total rate jumped from 12k/min to 104k/min (8.7×)", weight: 0.95 },
      { source: "metric",     ts: "18:42:10Z", observation: "inference_requests_total rate unchanged at ~38 RPS — output growth, not traffic", weight: 0.9 },
      { source: "log",        ts: "18:41:54Z", observation: "agents-orchestrator: 'planner.invoke' depth=12 (pre-rollout limit was 5)", weight: 0.85 },
    ],
    blastRadius: ["inference-agents/inference", "billing rollup", "Vertex AI quota burn rate"],
    steps: [
      "Lower max_tokens from 8192 → 1024 on the agents deployment to cap the bleeding",
      "helm rollback agents-runtime to v0.41 — the version before the prompt change",
      "Add a recursion-depth guard at the orchestration layer (per-call max_tokens won't catch loops)",
      "Open postmortem ticket and link the audit log entry for finance review",
    ],
    runbooks: [
      "inference-cost-runaway :: Most common root causes",
      "inference-cost-runaway :: Recommended remediation",
    ],
    timeline: [
      { ts: "18:39:12Z", source: "deployment", summary: "ReplicaSet agents-runtime-7c4b created (image: agents:v0.42)", severity: "warning", correlationScore: 0.92 },
      { ts: "18:39:48Z", source: "deployment", summary: "Deployment agents-runtime: NewReplicaSetAvailable",                severity: "info",    correlationScore: 0.61 },
      { ts: "18:41:54Z", source: "log",        summary: "agents-orchestrator: planner.invoke depth=12 exceeded prior baseline", severity: "warning", correlationScore: 0.74 },
      { ts: "18:42:05Z", source: "metric",     summary: "inference_tokens_out_total rate: 12k/min → 104k/min",              severity: "error",   correlationScore: 0.81 },
      { ts: "18:42:10Z", source: "metric",     summary: "inference_cost_usd_total slope: +$0.04/min → +$7.12/min",          severity: "critical",correlationScore: 0.83 },
      { ts: "18:43:00Z", source: "alert",      summary: "InferenceCostSpike fired",                                          severity: "critical" },
      { ts: "18:43:18Z", source: "k8s_event",  summary: "ScalingReplicaSet on agents-runtime (HPA reacting to CPU from longer responses)", severity: "info" },
    ],
    routingRecs: [
      {
        parameter: "max_tokens",
        currentValue: "8192",
        recommendedValue: "1024",
        unit: "tokens",
        reason: "Output token rate is 8.7× baseline. 8× reduction lands cost back near baseline while preserving response capacity for non-loop paths.",
      },
    ],
  },
  {
    id: "inc-2026-04-28-1840",
    ago: "5 min ago",
    ns: "inference-chat",
    alert: "InferenceTTFTHigh",
    tier: "3_llm",
    confidence: 0.81,
    severity: "warn",
    probableCause:
      "p95 TTFT climbed from ~180ms baseline to 1.84s. KV cache usage ratio is at 0.94, well into the saturation zone. Inflight gauge climbed without a corresponding RPS increase, indicating requests are queueing for batched decode. No deploy in the last hour — this is offered-load pressure, not a code regression.",
    recommendedAction:
      "Reduce max_concurrent_requests in the vLLM config from 64 → 32 via Helm values, then scale chat HPA max from 6 → 10 to absorb the offered load with smaller batches.",
    evidence: [
      { source: "metric",    ts: "18:38:00Z", observation: "kv_cache_usage_ratio sustained above 0.90 for 4 minutes", weight: 0.95 },
      { source: "metric",    ts: "18:39:30Z", observation: "inference_inflight climbed from 14 → 47 with steady RPS", weight: 0.85 },
      { source: "metric",    ts: "18:40:02Z", observation: "inference_time_to_first_token_seconds p95: 0.18s → 1.84s", weight: 0.9 },
      { source: "k8s_event", ts: "18:39:55Z", observation: "No FailedScheduling, no rollouts in last 60 minutes — points away from infra change", weight: 0.6 },
    ],
    blastRadius: ["inference-chat/inference", "downstream chat-frontend"],
    steps: [
      "Lower max_concurrent_requests in vLLM config from 64 → 32 via Helm values",
      "Scale chat HPA max from 6 → 10 to absorb load with smaller batches",
      "Watch kv_cache_usage_ratio for 5 min after rollout — should drop below 0.7",
      "If pressure persists, switch a portion of traffic to the smaller quantized variant",
    ],
    runbooks: ["inference-ttft-high :: Most common root causes"],
    timeline: [
      { ts: "18:36:00Z", source: "metric",    summary: "kv_cache_usage_ratio crossed 0.85",                            severity: "warning", correlationScore: 0.71 },
      { ts: "18:38:00Z", source: "metric",    summary: "kv_cache_usage_ratio crossed 0.90 (sustained)",                severity: "error",   correlationScore: 0.84 },
      { ts: "18:39:30Z", source: "metric",    summary: "inference_inflight: 14 → 47 (RPS unchanged)",                  severity: "warning", correlationScore: 0.76 },
      { ts: "18:40:02Z", source: "metric",    summary: "TTFT p95: 0.18s → 1.84s",                                       severity: "error",   correlationScore: 0.85 },
      { ts: "18:40:30Z", source: "alert",     summary: "InferenceTTFTHigh fired",                                       severity: "warning" },
    ],
    routingRecs: [
      {
        parameter: "max_concurrent_requests",
        currentValue: "64",
        recommendedValue: "32",
        unit: "requests",
        reason: "KV cache at 0.94 saturation with rising inflight under steady RPS. Halving concurrency yields smaller batches and lower cache pressure.",
      },
    ],
  },
  {
    id: "inc-2026-04-28-1828",
    ago: "17 min ago",
    ns: "inference-canary",
    alert: "GPUMemoryPressure",
    tier: "1_rule",
    confidence: 0.95,
    severity: "warn",
    probableCause:
      "GPU memory pressure ratio at 0.92 and climbing ~0.6%/min. CUDA OOM is imminent. A canary deployment landed 18 minutes before the alert, so a memory-leak regression in v0.42 of the model-serving framework is the most likely cause.",
    recommendedAction:
      "Capture nvidia-smi output before restarting (evidence is gone after kubelet kills the pod). Lower max_batch_size via Helm and roll the deployment.",
    evidence: [
      { source: "metric",     ts: "18:26:00Z", observation: "gpu_memory_pressure_ratio crossed 0.85 sustained",              weight: 0.95 },
      { source: "deployment", ts: "18:10:21Z", observation: "ReplicaSet inference-canary-9f2a created (image: v0.42)",      weight: 0.85 },
      { source: "k8s_event",  ts: "18:27:18Z", observation: "Unhealthy on inference-canary-9f2a-x4: readiness probe failed", weight: 0.7 },
    ],
    blastRadius: ["inference-canary/inference"],
    steps: [
      "Capture nvidia-smi output BEFORE restarting — leak evidence is gone after a restart",
      "Lower max_batch_size in the serving config via Helm values",
      "kubectl rollout restart deployment/inference -n inference-canary",
      "File ticket for the model-serving team to investigate KV-cache leak in v0.42",
    ],
    runbooks: ["gpu-oom-pressure :: Recommended remediation"],
    timeline: [
      { ts: "18:10:21Z", source: "deployment", summary: "ReplicaSet inference-canary-9f2a created (image: v0.42)",      severity: "info",    correlationScore: 0.74 },
      { ts: "18:26:00Z", source: "metric",     summary: "gpu_memory_pressure_ratio crossed 0.85 sustained",             severity: "warning", correlationScore: 0.86 },
      { ts: "18:27:18Z", source: "k8s_event",  summary: "Unhealthy: readiness probe failed (HTTP 503)",                  severity: "warning", correlationScore: 0.7 },
      { ts: "18:28:00Z", source: "alert",      summary: "GPUMemoryPressure fired",                                       severity: "warning" },
    ],
    routingRecs: [],
  },
];

export default function IncidentList({ apiIncidents }: { apiIncidents?: ApiIncident[] }) {
  const incidents =
    apiIncidents !== undefined
      ? apiIncidents.map(mapApiToIncident)
      : INCIDENTS;

  if (apiIncidents !== undefined && incidents.length === 0) {
    return (
      <div className="card p-8 text-center text-zinc-500 font-mono text-sm">
        No open incidents — all workloads healthy.
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {incidents.map(inc => <IncidentCard key={inc.id} inc={inc} />)}
    </div>
  );
}

function IncidentCard({ inc }: { inc: Incident }) {
  const [open, setOpen] = useState(inc === INCIDENTS[0]);
  const [tab, setTab] = useState<"evidence" | "timeline" | "remediation">("evidence");
  const sev = inc.severity === "crit" ? "pill-crit" : "pill-warn";
  const conf = Math.round(inc.confidence * 100);
  const confColour =
    conf >= 80 ? "text-signal-ok"  :
    conf >= 60 ? "text-signal-warn":
                 "text-signal-crit";

  return (
    <article className={`card overflow-hidden transition-all ${open ? "border-amber-glow/30" : ""}`}>
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-start gap-3 p-5 text-left hover:bg-ink-800/40 transition-colors"
      >
        <span className="pt-1 text-zinc-500">
          {open ? <ChevronDown className="w-4 h-4"/> : <ChevronRight className="w-4 h-4"/>}
        </span>
        <div className="flex-1">
          <div className="flex items-center gap-3 flex-wrap">
            <span className={sev}>{inc.severity === "crit" ? "critical" : "warning"}</span>
            <span className="font-mono text-sm text-zinc-200">{inc.alert}</span>
            <span className="text-zinc-500 text-xs">·  {inc.ns}</span>
            <span className="text-zinc-600 text-xs ml-auto font-mono">{inc.ago}</span>
          </div>
          <p className="mt-2 text-sm text-zinc-300 line-clamp-2">{inc.probableCause}</p>
          <div className="mt-3 flex items-center gap-4 text-xs">
            <span className="flex items-center gap-1.5 font-mono text-zinc-500">
              <Brain className="w-3 h-3" /> {inc.tier === "1_rule" ? "tier-1 rules" : "tier-3 gemini"}
            </span>
            <span className={`font-mono ${confColour}`}>confidence {conf}%</span>
            <span className="font-mono text-zinc-500">{inc.evidence.length} evidence · {inc.timeline.length} timeline</span>
          </div>
        </div>
      </button>

      {open && (
        <div className="px-5 pb-5 -mt-2 border-t border-ink-700/60 pt-4">
          {/* Recommended action — pinned at top */}
          <div className="mb-4 p-3 bg-amber-glow/5 border border-amber-glow/20 rounded-lg">
            <div className="flex items-center gap-1.5 mb-1.5 text-amber-glow font-mono text-[10px] uppercase tracking-[0.18em]">
              <Wrench className="w-3 h-3"/> Recommended action
            </div>
            <p className="text-sm text-zinc-200 leading-relaxed">{inc.recommendedAction}</p>
          </div>

          {inc.routingRecs.length > 0 && <RoutingBlock recs={inc.routingRecs} />}

          {/* Tab switcher */}
          <div className="flex gap-1 mb-3 font-mono text-[11px]">
            <Tab active={tab==="evidence"}    onClick={()=>setTab("evidence")}    icon={<FileSearch className="w-3 h-3"/>} text={`Evidence (${inc.evidence.length})`}/>
            <Tab active={tab==="timeline"}    onClick={()=>setTab("timeline")}    icon={<Clock className="w-3 h-3"/>}      text={`Timeline (${inc.timeline.length})`}/>
            <Tab active={tab==="remediation"} onClick={()=>setTab("remediation")} icon={<BookOpen className="w-3 h-3"/>}   text="Steps & runbooks"/>
          </div>

          {tab === "evidence"    && <EvidencePanel inc={inc} />}
          {tab === "timeline"    && <TimelinePanel inc={inc} />}
          {tab === "remediation" && <RemediationPanel inc={inc} />}

          <FeedbackBar inc={inc} />
        </div>
      )}
    </article>
  );
}

function FeedbackBar({ inc }: { inc: Incident }) {
  const [state, setState] = useState<"idle" | "submitting" | "ok" | "err">("idle");
  const [showCorrect, setShowCorrect] = useState(false);
  const [text, setText] = useState("");

  async function submit(was_correct: boolean, correction?: string) {
    setState("submitting");
    try {
      const res = await fetch("/api/feedback", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          incident_id: inc.id,
          was_correct,
          correction: correction ?? null,
          sre_user: "dashboard-user",
        }),
      });
      setState(res.ok ? "ok" : "err");
      if (res.ok && !was_correct) {
        setShowCorrect(false);
        setText("");
      }
    } catch {
      setState("err");
    }
  }

  const disabled = !inc.isLive || state === "submitting";

  return (
    <div className="pt-4 mt-4 border-t border-ink-700/60 space-y-3">
      <div className="flex gap-2 items-center">
        <button
          onClick={() => submit(true)}
          disabled={disabled}
          className="text-xs font-mono px-3 py-1.5 rounded-md bg-signal-ok/10 text-signal-ok border border-signal-ok/30 hover:bg-signal-ok/20 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          ✓ Mark correct
        </button>
        <button
          onClick={() => setShowCorrect(o => !o)}
          disabled={disabled}
          className="text-xs font-mono px-3 py-1.5 rounded-md bg-ink-800 text-zinc-300 border border-ink-700 hover:bg-ink-700 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          ✏ Correct it
        </button>
        {!inc.isLive && (
          <span className="text-[10px] font-mono text-zinc-600 ml-1">demo incident — feedback disabled</span>
        )}
        {state === "ok" && (
          <span className="text-[11px] font-mono text-signal-ok ml-1">✓ recorded</span>
        )}
        {state === "err" && (
          <span className="text-[11px] font-mono text-signal-crit ml-1">submit failed</span>
        )}
      </div>
      {showCorrect && (
        <div className="space-y-2">
          <textarea
            value={text}
            onChange={e => setText(e.target.value)}
            rows={3}
            placeholder="What was the actual root cause / correct remediation? Goes to the review queue, not directly into the KB."
            className="w-full text-[13px] font-mono bg-ink-900 border border-ink-700 rounded-md px-3 py-2 text-zinc-200 focus:outline-none focus:border-amber-glow/40"
          />
          <div className="flex gap-2">
            <button
              onClick={() => submit(false, text.trim())}
              disabled={disabled || text.trim().length < 4}
              className="text-xs font-mono px-3 py-1.5 rounded-md bg-amber-glow/10 text-amber-glow border border-amber-glow/30 hover:bg-amber-glow/20 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Submit for review
            </button>
            <button
              onClick={() => { setShowCorrect(false); setText(""); }}
              className="text-xs font-mono px-3 py-1.5 rounded-md bg-ink-800 text-zinc-400 border border-ink-700 hover:bg-ink-700"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function RoutingBlock({ recs }: { recs: RoutingRec[] }) {
  return (
    <div className="mb-4 p-3 bg-signal-ok/5 border border-signal-ok/20 rounded-lg">
      <div className="flex items-center gap-1.5 mb-2 text-signal-ok font-mono text-[10px] uppercase tracking-[0.18em]">
        <Sliders className="w-3 h-3"/> Routing change · cost-aware
      </div>
      <ul className="space-y-2">
        {recs.map((r, i) => (
          <li key={i} className="text-sm">
            <div className="flex items-center gap-2 flex-wrap font-mono">
              <span className="text-zinc-200">{r.parameter}</span>
              {r.currentValue !== undefined && (
                <>
                  <span className="text-zinc-500 text-xs">{r.currentValue}</span>
                  <span className="text-zinc-600 text-xs">→</span>
                </>
              )}
              <span className="text-signal-ok text-sm">{r.recommendedValue}</span>
              {r.unit && <span className="text-zinc-500 text-xs">{r.unit}</span>}
            </div>
            <p className="mt-1 text-xs text-zinc-400 leading-relaxed">{r.reason}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Tab({ active, onClick, icon, text }: { active: boolean; onClick: () => void; icon: React.ReactNode; text: string }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md transition-colors ${
        active
          ? "bg-amber-glow/10 text-amber-glow border border-amber-glow/30"
          : "text-zinc-400 hover:text-zinc-200 hover:bg-ink-800 border border-transparent"
      }`}
    >
      {icon} {text}
    </button>
  );
}

function EvidencePanel({ inc }: { inc: Incident }) {
  return (
    <ul className="space-y-2">
      {inc.evidence.map((e, i) => (
        <li key={i} className="flex gap-3 items-start text-sm">
          <SourceTag source={e.source} />
          <span className="font-mono text-zinc-600 text-xs mt-0.5 w-20 shrink-0">{e.ts}</span>
          <span className="flex-1 text-zinc-300 font-mono text-[13px] leading-relaxed">{e.observation}</span>
          <span className="font-mono text-zinc-500 text-[10px] mt-1">w={e.weight.toFixed(2)}</span>
        </li>
      ))}
    </ul>
  );
}

function TimelinePanel({ inc }: { inc: Incident }) {
  return (
    <ol className="space-y-1">
      {inc.timeline.map((t, i) => {
        const isAlert = t.source === "alert";
        const sevColour =
          t.severity === "critical" ? "border-signal-crit text-signal-crit" :
          t.severity === "error"    ? "border-signal-warn text-signal-warn" :
          t.severity === "warning"  ? "border-amber-glow/40 text-amber-glow" :
                                      "border-ink-700 text-zinc-500";
        return (
          <li key={i} className={`flex gap-3 items-start py-2 ${isAlert ? "bg-signal-warn/5 -mx-2 px-2 rounded" : ""}`}>
            <span className={`font-mono text-[10px] mt-1 w-2 h-2 rounded-full border ${sevColour} ${isAlert ? "ring-2 ring-signal-warn/30" : ""}`} />
            <span className="font-mono text-zinc-600 text-xs mt-0.5 w-20 shrink-0">{t.ts}</span>
            <SourceTag source={t.source} />
            <span className={`flex-1 font-mono text-[13px] leading-relaxed ${isAlert ? "text-signal-warn font-medium" : "text-zinc-300"}`}>{t.summary}</span>
            {t.correlationScore !== undefined && !isAlert && (
              <span className="font-mono text-[10px] text-zinc-500 mt-1">cause? {(t.correlationScore*100).toFixed(0)}%</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function RemediationPanel({ inc }: { inc: Incident }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
      <div>
        <SubTitle text="Remediation steps"/>
        <ol className="space-y-2">
          {inc.steps.map((s, i) => (
            <li key={i} className="text-sm text-zinc-300 flex gap-3">
              <span className="font-mono text-amber-glow text-xs mt-0.5">{i+1}.</span>
              <span className="font-mono text-[13px] leading-relaxed">{s}</span>
            </li>
          ))}
        </ol>
      </div>
      <div className="space-y-4">
        <div>
          <SubTitle text="Blast radius"/>
          <ul className="flex flex-wrap gap-1.5">
            {inc.blastRadius.map(b => (
              <li key={b} className="text-xs font-mono text-zinc-300 px-2 py-1 bg-ink-800 rounded-md border border-ink-700">{b}</li>
            ))}
          </ul>
        </div>
        <div>
          <SubTitle text="Runbooks consulted"/>
          <ul className="space-y-1.5">
            {inc.runbooks.map(r => (
              <li key={r} className="text-xs font-mono text-zinc-400">↳ {r}</li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

const sourceColour: Record<EvidenceRow["source"], string> = {
  alert:      "bg-signal-warn/10 text-signal-warn border-signal-warn/30",
  k8s_event:  "bg-signal-ok/10   text-signal-ok   border-signal-ok/30",
  deployment: "bg-amber-glow/10  text-amber-glow  border-amber-glow/30",
  log:        "bg-ink-800        text-zinc-300    border-ink-700",
  metric:     "bg-ink-700        text-zinc-200    border-ink-600",
};

function SourceTag({ source }: { source: EvidenceRow["source"] }) {
  return (
    <span className={`pill ${sourceColour[source]}`} style={{minWidth:"80px",justifyContent:"center"}}>
      {source}
    </span>
  );
}

function SubTitle({ text }: { text: string }) {
  return (
    <div className="mb-2 text-amber-glow font-mono text-[10px] uppercase tracking-[0.18em]">{text}</div>
  );
}
