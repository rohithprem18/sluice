import type { Health, PlaygroundEvent, Stats } from "./types";

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(path, { signal });
  if (!r.ok) throw new Error(`${path} returned ${r.status}`);
  return r.json() as Promise<T>;
}

export interface PlaygroundRequest {
  messages: { role: "user" | "assistant" | "system"; content: string }[];
  model: string;
  cache: boolean;
  redact: boolean;
  guard: boolean;
  simulate_outage?: string[];
  temperature?: number;
  max_tokens?: number;
}

export const api = {
  stats: (signal?: AbortSignal) => getJSON<Stats>("/api/stats", signal),
  health: (signal?: AbortSignal) => getJSON<Health>("/api/health", signal),

  /** Streams the gateway's NDJSON trace: steps, answer tokens, then a summary. */
  async playground(body: PlaygroundRequest, onEvent: (e: PlaygroundEvent) => void, signal?: AbortSignal) {
    const r = await fetch("/api/playground", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!r.ok || !r.body) {
      let message = `Request failed (${r.status})`;
      try {
        message = (await r.json()).error?.message ?? message;
      } catch {
        /* keep default */
      }
      onEvent({ type: "error", status: r.status, code: "http", message });
      return;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) onEvent(JSON.parse(line) as PlaygroundEvent);
      }
    }
  },
};
