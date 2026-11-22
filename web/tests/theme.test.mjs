// Regression tests for the light theme: the palette must exist, be complete,
// keep WCAG AA contrast, and the toggle/system preference wiring must be in place.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (p) => readFileSync(join(web, p), "utf8");

const CORE_VARS = [
  "--canvas", "--canvas-soft", "--canvas-card", "--canvas-mid", "--hairline",
  "--pill-line", "--pill-line-hover", "--ink", "--body", "--mute",
  "--sunset", "--sunset-soft", "--dusk", "--twilight", "--breeze",
  "--good", "--warn", "--bad", "--series-upstream", "--series-cache",
];

function parsePalette(css, selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}");
  const m = css.match(re);
  assert.ok(m, `palette block ${selector} missing`);
  const vars = {};
  const body = m[1].replace(/\/\*[\s\S]*?\*\//g, "");
  for (const line of body.split(";")) {
    const idx = line.indexOf(":");
    if (idx > 0) vars[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return vars;
}

function luminance(hex) {
  const c = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(c.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(fg, bg) {
  const [a, b] = [luminance(fg), luminance(bg)];
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test("styles.css defines a complete light palette under :root[data-theme=light]", () => {
  const css = read("src/styles.css");
  const light = parsePalette(css, ':root[data-theme="light"]');
  for (const v of CORE_VARS) assert.ok(light[v], `light palette missing ${v}`);
  assert.match(light["color-scheme"] ?? css.match(/:root\[data-theme="light"\][^}]*color-scheme:\s*light/s)?.[0] ?? "", /light/);
});

test("both themes keep WCAG AA contrast for text on the canvas", () => {
  const css = read("src/styles.css");
  const dark = parsePalette(css, ":root");
  const light = parsePalette(css, ':root[data-theme="light"]');
  for (const [name, palette] of [["dark", dark], ["light", light]]) {
    for (const [fg, bg] of [["--ink", "--canvas"], ["--body", "--canvas"], ["--mute", "--canvas"]]) {
      const ratio = contrast(palette[fg], palette[bg]);
      assert.ok(ratio >= 4.5, `${name}: ${fg} on ${bg} is ${ratio.toFixed(2)}:1, needs >= 4.5`);
    }
  }
});

test("index.html sets data-theme before first paint from localStorage or prefers-color-scheme", () => {
  const html = read("index.html");
  assert.match(html, /localStorage\.getItem\("sluice\.theme"\)/);
  assert.match(html, /prefers-color-scheme:\s*light/);
  assert.match(html, /document\.documentElement\.dataset\.theme/);
});

test("useTheme hook stores the choice and follows the system when none is stored", () => {
  const hooks = read("src/hooks.ts");
  assert.match(hooks, /export function useTheme\(\)/);
  assert.match(hooks, /localStorage\.setItem\(THEME_KEY/);
  assert.match(hooks, /prefers-color-scheme: light/);
  assert.match(hooks, /document\.documentElement\.dataset\.theme = theme/);
});

test("Navbar has an accessible theme toggle and the logo uses theme variables", () => {
  const navbar = read("src/components/Navbar.tsx");
  assert.match(navbar, /aria-label=\{theme === "light" \? "Switch to dark theme" : "Switch to light theme"\}/);
  assert.match(navbar, /aria-pressed/);
  assert.match(navbar, /onToggleTheme/);
  assert.match(navbar, /stroke="var\(--ink\)"/);
  assert.ok(!navbar.includes('stroke="#fff"'), "logo must not hardcode white");
});

test("package.json exposes the test script", () => {
  const pkg = JSON.parse(read("package.json"));
  assert.equal(pkg.scripts.test, "node --test tests/");
});
