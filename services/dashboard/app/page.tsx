import { Activity, AlertTriangle, Brain, Cpu, GitBranch, Layers, Shield, Zap } from "lucide-react";
import IncidentList, { ApiIncident } from "@/components/IncidentList";
import MetricCard from "@/components/MetricCard";
import NamespaceGrid from "@/components/NamespaceGrid";

export const dynamic = "force-dynamic";

async function fetchIncidents(): Promise<ApiIncident[] | undefined> {
  const url = `${process.env.ANALYST_URL ?? "http://ai-analyst:8080"}/v1/incidents`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return undefined;
    return res.json();
  } catch {
    return undefined;
  }
}

export default async function Page() {
  const apiIncidents = await fetchIncidents();
  return (
    <main className="max-w-[1440px] mx-auto px-8 py-8">
      {/* Header */}
      <header className="flex items-end justify-between border-b border-ink-700 pb-6 mb-8">
        <div>
          <div className="flex items-center gap-2 text-xs font-mono text-zinc-500 uppercase tracking-[0.2em] mb-2">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full rounded-full bg-signal-ok opacity-75 live-dot"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-signal-ok"></span>
            </span>
            Live · gke-autopilot · us-central1
          </div>
          <h1 className="font-display text-5xl italic text-zinc-50 leading-none">
            AI SRE Analyst
          </h1>
          <p className="text-zinc-400 mt-2 max-w-xl">
            Autonomous incident triage for LLM serving infrastructure on Kubernetes.
            Powered by Gemini 2.5 Flash + RAG over operational runbooks.
          </p>
        </div>
        <div className="text-right">
          <div className="font-mono text-xs text-zinc-500 uppercase tracking-wider">Mission control</div>
          <div className="font-display text-2xl text-amber-glow italic">v1.0.0</div>
        </div>
      </header>

      {/* Top metric strip */}
      <section className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <MetricCard icon={<Zap className="w-4 h-4"/>} label="Mean time to detect" value="1.4m" delta="-91%" tone="ok" />
        <MetricCard icon={<Activity className="w-4 h-4"/>} label="Mean time to resolve" value="8.2m" delta="-82%" tone="ok" />
        <MetricCard icon={<Brain className="w-4 h-4"/>} label="AI accuracy (rolling 7d)" value="92.4%" delta="+2.1%" tone="ok" />
        <MetricCard icon={<AlertTriangle className="w-4 h-4"/>} label="Alerts deduped" value="68%" delta="" tone="warn" />
      </section>

      {/* Namespace map + incidents */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">
        <div className="lg:col-span-2">
          <SectionTitle icon={<Layers className="w-4 h-4"/>} text="Inference workloads" />
          <NamespaceGrid />
        </div>
        <div>
          <SectionTitle icon={<Shield className="w-4 h-4"/>} text="System health" />
          <SystemHealth />
        </div>
      </section>

      <section className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <SectionTitle icon={<Cpu className="w-4 h-4"/>} text="Recent AI verdicts" />
          <IncidentList apiIncidents={apiIncidents} />
        </div>
        <div>
          <SectionTitle icon={<GitBranch className="w-4 h-4"/>} text="Pipeline" />
          <Pipeline />
        </div>
      </section>

      <footer className="mt-12 pt-6 border-t border-ink-700 text-xs text-zinc-500 flex justify-between font-mono">
        <span>GKE Autopilot · Vertex AI · Qdrant · Prometheus</span>
        <span>built as a portfolio piece — github.com/your-org/ai-sre-analyst</span>
      </footer>
    </main>
  );
}

function SectionTitle({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-2 mb-4 text-zinc-400">
      <span className="text-amber-glow">{icon}</span>
      <h2 className="font-mono text-xs uppercase tracking-[0.2em]">{text}</h2>
      <span className="flex-1 h-px bg-ink-700 ml-2" />
    </div>
  );
}

function SystemHealth() {
  const rows = [
    { name: "Vertex AI · gemini-2.5-flash", status: "healthy", latency: "612 ms p95" },
    { name: "Qdrant vector store",          status: "healthy", latency: "11 ms p95"  },
    { name: "Redis dedup cache",            status: "healthy", latency: "1.4 ms p95" },
    { name: "Alertmanager webhook",         status: "healthy", latency: "3 ms p95"   },
    { name: "GCS audit log",                status: "healthy", latency: "—"          },
  ];
  return (
    <div className="card p-4 space-y-3">
      {rows.map(r => (
        <div key={r.name} className="flex items-center justify-between text-sm">
          <span className="text-zinc-300">{r.name}</span>
          <span className="flex items-center gap-3">
            <span className="font-mono text-[11px] text-zinc-500">{r.latency}</span>
            <span className="pill-ok">●  ok</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function Pipeline() {
  const steps = [
    { label: "Alert ingest",   note: "Alertmanager webhook" },
    { label: "Dedup",          note: "Redis fingerprint" },
    { label: "Correlate",      note: "90s cross-NS window" },
    { label: "Tier-1 rules",   note: "Known signatures" },
    { label: "RAG retrieve",   note: "Qdrant top-k=4" },
    { label: "Gemini analyze", note: "Structured JSON" },
    { label: "Fan-out",        note: "Slack · dash · audit" },
  ];
  return (
    <div className="card p-5">
      <ol className="space-y-3">
        {steps.map((s, i) => (
          <li key={s.label} className="flex gap-3 items-start">
            <div className="flex flex-col items-center pt-0.5">
              <span className="w-6 h-6 rounded-full border border-amber-glow/40 bg-amber-glow/10 text-amber-glow text-[11px] font-mono flex items-center justify-center">
                {i + 1}
              </span>
              {i < steps.length - 1 && <span className="w-px h-6 bg-ink-700 mt-1" />}
            </div>
            <div>
              <div className="text-sm text-zinc-200">{s.label}</div>
              <div className="text-xs text-zinc-500 font-mono">{s.note}</div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
