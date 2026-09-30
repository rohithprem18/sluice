export interface Totals {
  requests: number;
  errors: number;
  cache_hits: number;
  cache_hit_rate: number;
  semantic_hits: number;
  exact_hits: number;
  fallbacks: number;
  pii_redacted: number;
  blocked: number;
  tokens_in: number;
  tokens_out: number;
  tokens_saved: number;
}

export interface MinutePoint {
  minute: number;
  requests: number;
  errors: number;
  cache_hits: number;
  avg_latency_ms: number | null;
}

export interface DeploymentStat {
  deployment: string;
  provider: string;
  model: string;
  tier: "smart" | "fast";
  requests: number;
  errors: number;
  p50_ms: number | null;
  p95_ms: number | null;
  ewma_ms: number | null;
  state: "healthy" | "degraded" | "open";
}

export interface Attempt {
  deployment: string;
  ok: boolean;
  ms: number;
  status: number;
  error?: string;
}

export interface RequestEvent {
  id: string;
  ts: number;
  client: string;
  model_requested: string;
  tier: string;
  served_by: string | null;
  status: number;
  latency_ms: number;
  upstream_ms: number | null;
  cache: "exact" | "semantic" | null;
  similarity: number | null;
  attempts: Attempt[];
  pii: Record<string, number>;
  guard_score: number | null;
  tokens_in: number;
  tokens_out: number;
  stream: boolean;
  error: string | null;
}

export interface Stats {
  totals: Totals;
  series: MinutePoint[];
  deployments: DeploymentStat[];
  recent: RequestEvent[];
}

export interface Health {
  status: string;
  version: string;
  backends: { store: string; vector: string; embeddings: string };
  providers: { name: string; label: string; enabled: boolean }[];
  deployments: number;
  guard: boolean;
  cache_threshold: number | Record<string, number>;
  limits: { playground_per_hour: number; key_rpm: number };
}

export type TraceStep =
  | { type: "step"; step: "redact"; ms: number; found: Record<string, number>; preview: string | null }
  | { type: "step"; step: "guard"; ms?: number; score: number | null; blocked: boolean; skipped?: boolean }
  | { type: "step"; step: "route"; tier: string; reason: string; order: string[]; skipped_open: string[] }
  | { type: "step"; step: "cache"; ms?: number; result: string; similarity?: number | null; matched?: string | null }
  | { type: "step"; step: "upstream"; deployment: string; ttft_ms: number }
  | { type: "step"; step: "attempt_failed"; deployment: string; status: number; ms: number; simulated?: boolean };

export interface DoneMeta {
  request_id: string;
  served_by: string | null;
  tier: string;
  cache: string | null;
  similarity: number | null;
  attempts: Attempt[];
  pii: Record<string, number>;
  guard_score: number | null;
  latency_ms: number;
}

export type PlaygroundEvent =
  | TraceStep
  | { type: "delta"; text: string; model: string }
  | { type: "done"; meta: DoneMeta; usage: Record<string, number> | null }
  | { type: "error"; status: number; code: string; message: string };
