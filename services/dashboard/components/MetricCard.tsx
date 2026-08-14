interface Props {
  icon: React.ReactNode;
  label: string;
  value: string;
  delta?: string;
  tone: "ok" | "warn" | "crit";
}

const toneClass = {
  ok:   "text-signal-ok",
  warn: "text-signal-warn",
  crit: "text-signal-crit",
};

export default function MetricCard({ icon, label, value, delta, tone }: Props) {
  return (
    <div className="card p-5 relative overflow-hidden group hover:border-ink-600 transition-colors">
      <div className="absolute -right-6 -top-6 w-24 h-24 rounded-full bg-gradient-to-br from-amber-glow/5 to-transparent blur-xl group-hover:from-amber-glow/15 transition-all" />
      <div className="relative">
        <div className="flex items-center gap-2 text-zinc-500 mb-3">
          <span className="text-amber-glow">{icon}</span>
          <span className="font-mono text-[11px] uppercase tracking-[0.18em]">{label}</span>
        </div>
        <div className="flex items-baseline gap-2">
          <span className="font-display text-4xl text-zinc-50 leading-none">{value}</span>
          {delta && (
            <span className={`font-mono text-xs ${toneClass[tone]}`}>{delta}</span>
          )}
        </div>
      </div>
    </div>
  );
}
