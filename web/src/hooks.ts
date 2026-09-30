import { useEffect, useState } from "react";
import { api } from "./api";
import type { Health, Stats } from "./types";

export const ROUTES = ["overview", "playground", "requests", "providers"] as const;
export type Route = (typeof ROUTES)[number];

function currentRoute(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "").split("?")[0];
  return (ROUTES as readonly string[]).includes(hash) ? (hash as Route) : "overview";
}

/** Hash routing: bookmarkable views without a server-side router. */
export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(currentRoute);
  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

/** Polls /api/stats; pauses while the tab is hidden. */
export function useStats(intervalMs = 5000) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      if (document.hidden) return;
      try {
        const s = await api.stats(controller.signal);
        if (!cancelled) {
          setStats(s);
          setError(false);
        }
      } catch (e) {
        if (!cancelled && !(e instanceof DOMException && e.name === "AbortError")) setError(true);
      }
    };
    load();
    const timer = window.setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [intervalMs, tick]);

  return { stats, error, refresh: () => setTick((t) => t + 1) };
}

export function useHealth() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api
      .health(controller.signal)
      .then(setHealth)
      .catch((e) => {
        if (!(e instanceof DOMException && e.name === "AbortError")) setError(true);
      });
    return () => controller.abort();
  }, []);
  return { health, error };
}
