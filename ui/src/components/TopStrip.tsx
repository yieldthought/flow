import { diagnosticText, formatCompactDuration } from "../format";
import type { OverviewSnapshot } from "../types";

export function TopStrip({ snapshot }: { snapshot: OverviewSnapshot }) {
  const { runtime, flow } = snapshot;
  const banner = runtime.diagnostics[0];

  return (
    <header className="top-strip">
      <div className="top-strip__group">
        <div className="eyebrow">flow</div>
        <div className="top-strip__title">{flow.name}</div>
      </div>
      <div className="top-strip__group">
        <StatusPill tone={runtime.active ? "ok" : "error"} label={runtime.active ? "runtime active" : "runtime shut down"} />
        <InfoPill label={`uptime ${formatCompactDuration(runtime.uptime_seconds)}`} />
        <InfoPill label={`working ${flow.counts.working}`} tone="info" />
        <InfoPill label={`waiting ${flow.counts.waiting}`} />
        <InfoPill label={`paused ${flow.counts.paused}`} tone="warn" />
        <InfoPill label={`needs help ${flow.counts.needs_help}`} tone="error" />
      </div>
      {banner ? (
        <div className={`diagnostic-banner diagnostic-banner--${banner.level}`}>
          <span className="diagnostic-banner__label">{banner.level}</span>
          <span>{diagnosticText(banner)}</span>
        </div>
      ) : null}
    </header>
  );
}

function StatusPill({ label, tone }: { label: string; tone: "ok" | "error" }) {
  return <span className={`summary-pill summary-pill--${tone}`}>{label}</span>;
}

function InfoPill({ label, tone = "muted" }: { label: string; tone?: "muted" | "info" | "warn" | "error" }) {
  return <span className={`summary-pill summary-pill--${tone}`}>{label}</span>;
}
