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
      <rect x="0.5" y="0.5" width="31" height="31" rx="8" fill="none" stroke="rgba(255,255,255,.35)" />
      <path d="M8 11h16M8 16h11M8 21h6" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}

export function Navbar({ route, health, healthError }: { route: Route; health: Health | null; healthError: boolean }) {
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
          <a className="pill sm" href="https://github.com/rohithprem18/sluice" target="_blank" rel="noopener noreferrer">
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}
