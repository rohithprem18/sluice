import { TrafficChart } from "../components/TrafficChart";
import { fmtCompact, fmtInt, fmtMs, fmtPct, splitDeployment, PROVIDER_LABEL } from "../format";
import { useStats } from "../hooks";

const FLOW = [
  { t: "Redact", d: "Emails, cards, Aadhaar, keys become placeholders" },
  { t: "Screen", d: "Prompt Guard scores injection attempts" },
  { t: "Route", d: "Pick a tier, rank models by live latency" },
  { t: "Cache", d: "Exact match, then semantic match" },
  { t: "Call", d: "Fall back on 429, 5xx or timeout" },
  { t: "Restore", d: "Originals put back in the answer" },
];

export function Overview() {
  const { stats, error } = useStats(5000);
  const t = stats?.totals;
  const upstreamCalls = t ? t.requests - t.cache_hits : 0;
  const kpis = [
    { label: "Requests", value: fmtInt(t?.requests), note: t ? `${fmtInt(upstreamCalls)} reached a provider` : "" },
    { label: "Cache hit rate", value: fmtPct(t?.cache_hit_rate, 1), note: t ? `${t.exact_hits} exact · ${t.semantic_hits} semantic` : "" },
    { label: "Tokens saved", value: fmtCompact(t?.tokens_saved), note: "served from cache, not generated" },
    { label: "Fallbacks absorbed", value: fmtInt(t?.fallbacks), note: "failed calls the client never saw" },
    { label: "PII redacted", value: fmtInt(t?.pii_redacted), note: t ? `${t.blocked} injection attempts blocked` : "" },
  ];

  return (
    <div className="view">
      <header className="view-head">
        <span className="eyebrow">Overview</span>
        <div className="row">
          <h1 className="display">One endpoint. Every free model.</h1>
        </div>
        <p className="lead">
          Sluice pools free LLM providers behind a single OpenAI-compatible API. It routes each request to the fastest
          healthy model, falls back when one fails, answers repeats from cache, and never lets personal data leave.
        </p>
      </header>

      {error && <div className="error-banner">Can't reach the Sluice API. Metrics will appear when it's back.</div>}

      <section className="kpis" aria-label="Key metrics">
        {kpis.map((k) => (
          <div key={k.label} className="card kpi">
            <span className="mono-label">{k.label}</span>
            <span className="kpi-value">{stats ? k.value : <span className="skeleton" style={{ display: "block", height: 34, width: 90 }} />}</span>
            <span className="caption">{k.note}</span>
          </div>
        ))}
      </section>

      <section className="grid-2">
        <div className="card">
          <div className="card-head">
            <span className="card-title">Traffic, last 60 minutes</span>
            <div className="legend">
              <span><i style={{ background: "var(--series-upstream)" }} />Sent to a provider</span>
              <span><i style={{ background: "var(--series-cache)" }} />Answered from cache</span>
            </div>
          </div>
          {stats ? <TrafficChart series={stats.series} /> : <div className="skeleton" style={{ height: 220 }} />}
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">Models by latency</span>
            <a className="caption" href="#/providers">All providers →</a>
          </div>
          <div className="table-wrap" style={{ maxHeight: 232 }}>
            <table>
              <thead>
                <tr><th>Model</th><th className="num">p50</th><th className="num">p95</th><th><span className="visually-hidden">State</span></th></tr>
              </thead>
              <tbody>
                {(stats?.deployments ?? [])
                  .slice()
                  .sort((a, b) => (a.p50_ms ?? 1e9) - (b.p50_ms ?? 1e9))
                  .map((d) => (
                    <tr key={d.deployment} onClick={() => (window.location.hash = "/providers")}>
                      <td>
                        {splitDeployment(d.deployment).model}
                        <span className="muted"> · {PROVIDER_LABEL[splitDeployment(d.deployment).provider] ?? d.provider}</span>
                      </td>
                      <td className="num">{fmtMs(d.p50_ms)}</td>
                      <td className="num">{fmtMs(d.p95_ms)}</td>
                      <td>
                        <span className={`status ${d.state === "healthy" ? "good" : d.state === "open" ? "bad" : "warn"}`}
                              title={d.state === "open" ? "Paused" : d.state}>
                          <span className="dot" />
                          <span className="visually-hidden">{d.state === "open" ? "Paused" : d.state}</span>
                        </span>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="card" aria-label="Request pipeline">
        <div className="card-head">
          <span className="card-title">What happens to every request</span>
          <a className="caption" href="#/playground">Watch it live in the Playground →</a>
        </div>
        <div className="flow">
          {FLOW.map((f, i) => (
            <div key={f.t} className="flow-step">
              <div className="n">{String(i + 1).padStart(2, "0")}</div>
              <div className="t">{f.t}</div>
              <div className="d">{f.d}</div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
