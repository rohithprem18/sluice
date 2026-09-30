import { useState } from "react";
import { fmtInt, fmtMs, PROVIDER_LABEL } from "../format";
import { useStats } from "../hooks";
import type { Health } from "../types";

const origin = typeof window !== "undefined" ? window.location.origin : "https://sluice.vercel.app";

const SNIPPETS: Record<string, string> = {
  python: `from openai import OpenAI

client = OpenAI(base_url="${origin}/v1",
                api_key="YOUR_SLUICE_KEY")

reply = client.chat.completions.create(
    model="sluice/auto",   # or sluice/smart, sluice/fast
    messages=[{"role": "user", "content": "Hi"}],
)
print(reply.choices[0].message.content)
print(reply.model_extra["sluice"]["served_by"])`,
  curl: `curl ${origin}/v1/chat/completions \\
  -H "Authorization: Bearer YOUR_SLUICE_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "sluice/fast", "stream": true,
       "messages": [{"role": "user", "content": "Hi"}]}'

# per request: x-sluice-cache | redact | guard: off`,
};

export function Providers({ health }: { health: Health | null }) {
  const { stats } = useStats(5000);
  const [tab, setTab] = useState<"python" | "curl">("python");
  const deps = stats?.deployments ?? [];

  return (
    <div className="view">
      <header className="view-head">
        <span className="eyebrow">Providers</span>
        <div className="row">
          <h1 className="display">Free tiers, pooled.</h1>
          <span className="caption">
            {health ? `${health.deployments} models across ${health.providers.filter((p) => p.enabled).length} providers` : ""}
          </span>
        </div>
        <p className="lead">
          Each free tier alone is small: Groq allows 8,000 tokens per minute per model. A model that fails three times in a
          row is paused for a minute, then gets one trial request before it rejoins the rotation.
        </p>
      </header>

      <section className="prov">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Model</th><th>Provider</th><th>Tier</th>
                <th className="num">p50</th><th className="num">p95</th><th className="num">Calls</th>
                <th className="num">Failed</th><th>State</th>
              </tr>
            </thead>
            <tbody>
              {deps.map((d) => {
                const tone = d.state === "healthy" ? "good" : d.state === "open" ? "bad" : "warn";
                return (
                  <tr key={d.deployment} style={{ cursor: "default" }}>
                    <td>{d.model.split("/").pop()}</td>
                    <td className="muted">{PROVIDER_LABEL[d.deployment.split("/")[0]] ?? d.provider}</td>
                    <td className="muted">{d.tier}</td>
                    <td className="num">{fmtMs(d.p50_ms)}</td>
                    <td className="num">{fmtMs(d.p95_ms)}</td>
                    <td className="num">{fmtInt(d.requests)}</td>
                    <td className="num">{d.errors ? fmtInt(d.errors) : "—"}</td>
                    <td><span className={`status ${tone}`}><span className="dot" />{d.state === "open" ? "Paused" : d.state}</span></td>
                  </tr>
                );
              })}
              {deps.length === 0 && (
                <tr><td colSpan={8} className="muted">Loading…</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <div className="card-head">
            <span className="card-title">Use it from any OpenAI SDK</span>
            <div className="code-tabs" style={{ marginBottom: 0 }}>
              {(["python", "curl"] as const).map((t) => (
                <button key={t} className={`pill sm ${tab === t ? "on" : ""}`} onClick={() => setTab(t)}>
                  {t === "python" ? "Python" : "curl"}
                </button>
              ))}
            </div>
          </div>
          <pre className="code">{SNIPPETS[tab]}</pre>
          {health && (
            <div className="backends">
              <div><div className="mono-label">State</div><div className="v">{health.backends.store}</div></div>
              <div><div className="mono-label">Cache</div><div className="v">{health.backends.vector}</div></div>
              <div><div className="mono-label">Key limit</div><div className="v">{health.limits.key_rpm} req/min</div></div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
