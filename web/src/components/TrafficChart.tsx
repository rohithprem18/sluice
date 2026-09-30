import { useMemo, useRef, useState } from "react";
import type { MinutePoint } from "../types";

const W = 720;
const H = 220;
const PAD = { top: 12, right: 8, bottom: 24, left: 36 };

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  const n = v / pow;
  return (n <= 2 ? 2 : n <= 5 ? 5 : 10) * pow;
}

/** Requests per minute, split into upstream calls and cache hits (stacked, one axis). */
export function TrafficChart({ series }: { series: MinutePoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);
  const max = useMemo(() => niceMax(Math.max(0, ...series.map((p) => p.requests))), [series]);
  const total = series.reduce((a, p) => a + p.requests, 0);

  if (!total) {
    return (
      <div className="empty-chart">
        <div>
          <div>No traffic in the last hour</div>
          <div className="caption" style={{ marginTop: 6 }}>
            Send a prompt from the <a href="#/playground" style={{ textDecoration: "underline" }}>Playground</a> to see it here.
          </div>
        </div>
      </div>
    );
  }

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const slot = iw / series.length;
  const bw = Math.max(2, slot - 2); // 2px surface gap between bars
  const y = (v: number) => PAD.top + ih - (v / max) * ih;
  const ticks = [0, max / 2, max];

  const onMove = (e: React.MouseEvent) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const x = ((e.clientX - box.left) / box.width) * W - PAD.left;
    const i = Math.floor(x / slot);
    setHover(i >= 0 && i < series.length ? i : null);
  };

  const hp = hover != null ? series[hover] : null;
  const hx = hover != null ? ((PAD.left + hover * slot + slot / 2) / W) * 100 : 0;

  return (
    <div className="chart-wrap">
      <svg ref={ref} className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
           aria-label={`Requests per minute over the last hour, ${total} in total`}
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
            <text className="axis" x={PAD.left - 8} y={y(t) + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        {series.map((p, i) => {
          const x = PAD.left + i * slot + (slot - bw) / 2;
          const upstream = p.requests - p.cache_hits;
          const yu = y(upstream);
          const yt = y(p.requests);
          return (
            <g key={p.minute} opacity={hover == null || hover === i ? 1 : 0.45}>
              {upstream > 0 && <rect x={x} y={yu} width={bw} height={y(0) - yu} rx={Math.min(2, bw / 2)} fill="var(--series-upstream)" />}
              {p.cache_hits > 0 && (
                <rect x={x} y={yt} width={bw} height={Math.max(0, yu - yt - (upstream > 0 ? 2 : 0))} rx={Math.min(2, bw / 2)}
                      fill="var(--series-cache)" />
              )}
            </g>
          );
        })}
        <text className="axis" x={PAD.left} y={H - 6}>60 min ago</text>
        <text className="axis" x={W - PAD.right} y={H - 6} textAnchor="end">now</text>
      </svg>
      {hp && (
        <div className="tooltip" style={{ left: `${hx}%`, top: `${(y(hp.requests) / H) * 100}%` }}>
          <div className="muted">{new Date(hp.minute * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</div>
          <div>{hp.requests} requests</div>
          <div>{hp.cache_hits} from cache</div>
          {hp.avg_latency_ms != null && <div className="muted">avg {Math.round(hp.avg_latency_ms)} ms</div>}
        </div>
      )}
    </div>
  );
}
