import { useState } from "react";
import { fmtAgo, fmtMs, piiSummary, splitDeployment } from "../format";
import { useStats } from "../hooks";
import type { RequestEvent } from "../types";

function outcome(e: RequestEvent): { label: string; cls: string } {
  if (e.status === 403) return { label: "Blocked", cls: "bad" };
  if (e.status >= 400) return { label: `Error ${e.status}`, cls: "bad" };
  if (e.cache) return { label: `Cache · ${e.cache}`, cls: "cache" };
  return { label: "Upstream", cls: "" };
}

export function Requests() {
  const { stats, error } = useStats(4000);
  const [selected, setSelected] = useState<string | null>(null);
  const rows = stats?.recent ?? [];
  const sel = rows.find((r) => r.id === selected) ?? rows[0] ?? null;

  return (
    <div className="view">
      <header className="view-head">
        <span className="eyebrow">Requests</span>
        <div className="row">
          <h1 className="display">Every call, accounted for.</h1>
          <span className="caption">Latest 50 requests · refreshes every 4 s</span>
        </div>
      </header>
      {error && <div className="error-banner">Can't reach the Sluice API.</div>}

      <section className="split">
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>When</th><th>Client</th><th>Served by</th><th>Outcome</th><th className="num">Latency</th><th>PII</th></tr>
            </thead>
            <tbody>
              {rows.length === 0 && (
                <tr><td colSpan={6} className="muted" style={{ cursor: "default" }}>
                  No requests yet. Send one from the <a href="#/playground" style={{ textDecoration: "underline" }}>Playground</a>.
                </td></tr>
              )}
              {rows.map((r) => {
                const o = outcome(r);
                return (
                  <tr key={r.id} className={sel?.id === r.id ? "selected" : undefined} onClick={() => setSelected(r.id)}>
                    <td className="muted">{fmtAgo(r.ts)}</td>
                    <td>{r.client}</td>
                    <td>{r.served_by ? splitDeployment(r.served_by).model : "—"}</td>
                    <td><span className={`tag ${o.cls}`}>{o.label}</span></td>
                    <td className="num">{fmtMs(r.latency_ms)}</td>
                    <td className="muted">{Object.values(r.pii).reduce((a, b) => a + b, 0) || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <aside className="card" aria-label="Request detail">
          {!sel ? (
            <p className="caption">Select a request to see how it was handled.</p>
          ) : (
            <>
              <div className="card-head">
                <span className="card-title">Request detail</span>
                <span className="mono-label">{sel.id}</span>
              </div>
              <dl className="detail-list">
                <dt>Model asked</dt><dd>{sel.model_requested}{sel.tier ? ` → ${sel.tier} tier` : ""}</dd>
                <dt>Served by</dt><dd>{sel.served_by ?? (sel.cache ? "cache" : "—")}</dd>
                <dt>Status</dt><dd>{sel.status}{sel.error ? ` · ${sel.error}` : ""}</dd>
                <dt>Total time</dt><dd>{fmtMs(sel.latency_ms)}{sel.upstream_ms ? ` (provider ${fmtMs(sel.upstream_ms)})` : ""}</dd>
                <dt>Cache</dt><dd>{sel.cache ? `${sel.cache}${sel.similarity ? `, similarity ${sel.similarity.toFixed(3)}` : ""}` : "miss"}</dd>
                <dt>PII redacted</dt><dd>{piiSummary(sel.pii)}</dd>
                <dt>Injection score</dt><dd>{sel.guard_score == null ? "—" : sel.guard_score.toFixed(4)}</dd>
                <dt>Tokens</dt><dd>{sel.tokens_in} in · {sel.tokens_out} out</dd>
                <dt>Mode</dt><dd>{sel.stream ? "streaming" : "single response"}</dd>
              </dl>
              {sel.attempts.length > 0 && (
                <>
                  <div className="mono-label" style={{ marginTop: 18 }}>Attempts</div>
                  <ul className="attempts">
                    {sel.attempts.map((a, i) => (
                      <li key={i}>
                        <span>{i + 1}. {splitDeployment(a.deployment).model}</span>
                        <span className={a.ok ? "muted" : ""} style={a.ok ? undefined : { color: "var(--bad)" }}>
                          {a.ok ? fmtMs(a.ms) : `failed ${a.status}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </>
          )}
        </aside>
      </section>
    </div>
  );
}
