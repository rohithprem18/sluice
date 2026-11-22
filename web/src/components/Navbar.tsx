import type { Route } from "../hooks";
import type { Health } from "../types";

const LINKS: { route: Route; label: string }[] = [
  { route: "overview", label: "Overview" },
  { route: "playground", label: "Playground" },
  { route: "requests", label: "Requests" },
  { route: "providers", label: "Providers" },
];

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect x="0.5" y="0.5" width="31" height="31" rx="8" fill="none" stroke="var(--pill-line)" />
      <path d="M8 11h16M8 16h11M8 21h6" stroke="var(--ink)" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}

function ThemeIcon({ theme }: { theme: "light" | "dark" }) {
  return theme === "light" ? (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

interface NavbarProps {
  route: Route;
  health: Health | null;
  healthError: boolean;
  theme: "light" | "dark";
  onToggleTheme: () => void;
}

export function Navbar({ route, health, healthError, theme, onToggleTheme }: NavbarProps) {
  const live = health?.providers.filter((p) => p.enabled).length ?? 0;
  const state = healthError ? "bad" : health ? "good" : "warn";
  const label = healthError ? "API offline" : health ? `${live} providers live` : "Connecting";
  return (
    <header className="navbar">
      <div className="nav-inner">
        <a className="brand" href="#/overview" aria-label="Sluice overview">
          <Logo />
          <span>Sluice</span>
        </a>
        <nav className="nav-links" aria-label="Sections">
          {LINKS.map((l) => (
            <a key={l.route} href={`#/${l.route}`} className={route === l.route ? "active" : undefined}
               aria-current={route === l.route ? "page" : undefined}>
              {l.label}
            </a>
          ))}
        </nav>
        <div className="nav-right">
          <span className={`status ${state}`} role="status">
            <span className="dot" aria-hidden="true" />
            {label}
          </span>
          <button
            type="button"
            className="pill sm theme-toggle"
            onClick={onToggleTheme}
            aria-label={theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
            aria-pressed={theme === "light"}
            title={theme === "light" ? "Switch to dark theme" : "Switch to light theme"}
          >
            <ThemeIcon theme={theme} />
          </button>
          <a className="pill sm" href="https://github.com/rohithprem18/sluice" target="_blank" rel="noopener noreferrer">
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}
