import { useEffect } from "react";
import { Navbar } from "./components/Navbar";
import { useHealth, useRoute, useTheme } from "./hooks";
import { Overview } from "./views/Overview";
import { Playground } from "./views/Playground";
import { Providers } from "./views/Providers";
import { Requests } from "./views/Requests";

const TITLES = { overview: "Overview", playground: "Playground", requests: "Requests", providers: "Providers" };

export default function App() {
  const route = useRoute();
  const { health, error } = useHealth();
  const [theme, toggleTheme] = useTheme();

  useEffect(() => {
    document.title = `${TITLES[route]} · Sluice`;
  }, [route]);

  return (
    <>
      <Navbar route={route} health={health} healthError={error} theme={theme} onToggleTheme={toggleTheme} />
      <main key={route}>
        {route === "overview" && <Overview />}
        {route === "playground" && <Playground health={health} />}
        {route === "requests" && <Requests />}
        {route === "providers" && <Providers health={health} />}
      </main>
    </>
  );
}
