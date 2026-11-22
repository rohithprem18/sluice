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

export type Theme = "light" | "dark";

const THEME_KEY = "sluice.theme";

function initialTheme(): Theme {
  // index.html sets data-theme before first paint; this is the React-side fallback
  const attr = document.documentElement.dataset.theme;
  if (attr === "light" || attr === "dark") return attr;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function systemTheme(): Theme {
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** The console theme: the stored choice wins, otherwise the system preference. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    meta?.setAttribute("content", theme === "light" ? "#f6f7f9" : "#0a0a0a");
  }, [theme]);

  // follow the OS while the user has not picked a theme explicitly
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => {
      let stored: string | null = null;
      try {
        stored = localStorage.getItem(THEME_KEY);
      } catch {
        /* storage unavailable */
      }
      if (stored !== "light" && stored !== "dark") setTheme(systemTheme());
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const toggle = () =>
    setTheme((t) => {
      const next: Theme = t === "light" ? "dark" : "light";
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch {
        /* storage unavailable */
      }
      return next;
    });
  return [theme, toggle];
}
