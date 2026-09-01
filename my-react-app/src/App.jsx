import { useState, useEffect } from "react";
import "./styles.css";
import ExtractorDashboard from "./components/ExtractorDashboard.jsx";
import ComparisonDashboard from "./components/ComparisonDashboard.jsx";
import BidLookupDashboard from "./components/BidLookupDashboard.jsx";
import RecommendationDashboard from "./components/RecommendationDashboard.jsx";
import gemStar from "./assets/gem-star.png";

const API = ""; // same-origin; Vite proxies /api -> Flask (see vite.config.js)

const TABS = [
  { id: "custom",    label: "Custom Bid Extractor" },
  { id: "category",  label: "Category Bid Extractor" },
  { id: "compare",   label: "Comparison" },
  { id: "lookup",    label: "Bid Lookup" },
  { id: "recommend", label: "Category Recommendation" },
];

// Official GeM star mark (cropped from the GeM logo, transparent background).
function GemLogo() {
  return <img src={gemStar} alt="GeM" className="h-11 w-11 shrink-0" />;
}

function BackendStatus() {
  const [ok, setOk] = useState(null);
  useEffect(() => {
    let alive = true;
    const ping = () =>
      fetch(`${API}/api/extract/custom/status`)
        .then((r) => alive && setOk(r.ok))
        .catch(() => alive && setOk(false));
    ping();
    const t = setInterval(ping, 15000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  const label = ok === null ? "Checking service status" : ok ? "Analytics service connected" : "Analytics service offline";
  const dot   = ok === null ? "bg-slate-400" : ok ? "bg-green-400" : "bg-red-400";
  return (
    <span className="flex items-center gap-2 text-xs text-white/80">
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function App() {
  const [tab, setTab] = useState("custom");

  return (
    <div className="gem-page flex flex-col min-h-screen">
      {/* Top utility strip */}
      <div className="bg-gem-header text-white/70 text-xs">
        <div className="gem-wrap max-w-6xl flex items-center justify-between h-8">
          <span>Government e Marketplace · Internal Analytics Portal</span>
          <BackendStatus />
        </div>
      </div>

      {/* Masthead */}
      <header className="bg-gem-header">
        <div className="gem-wrap max-w-6xl flex items-center gap-3 py-3">
          <GemLogo />
          <div className="leading-tight">
            <div className="text-white font-bold text-xl tracking-tight">
              GeM <span className="font-normal text-white/90">Analytics</span>
            </div>
            <div className="text-white/60 text-[11px] -mt-0.5">Government e Marketplace · Analytics Portal</div>
          </div>
        </div>

        {/* Navigation band */}
        <nav className="bg-gem-nav">
          <div className="gem-wrap max-w-6xl flex flex-wrap">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`gem-tab ${tab === t.id ? "gem-tab-active" : ""}`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </nav>
      </header>

      {/* Tricolour accent divider */}
      <div className="gem-divider" />

      {/* Active view. Every section stays mounted and inactive ones are hidden,
          so each section keeps its own state and results when switching tabs
          (and any running job continues updating in the background). */}
      <main className="flex-1">
        <div className={tab === "custom" ? "" : "hidden"}>
          <ExtractorDashboard
            module="custom"
            title="Custom Bid Extractor"
            subtitle="Extract GeMARPTS data from active Product Custom Bids and Reverse Auctions."
            accent="blue"
          />
        </div>
        <div className={tab === "category" ? "" : "hidden"}>
          <ExtractorDashboard
            module="category"
            title="Category Bid Extractor"
            subtitle="Extract item category details from active standard Category Bids."
            accent="green"
          />
        </div>
        <div className={tab === "compare" ? "" : "hidden"}>
          <ComparisonDashboard />
        </div>
        <div className={tab === "lookup" ? "" : "hidden"}>
          <BidLookupDashboard />
        </div>
        <div className={tab === "recommend" ? "" : "hidden"}>
          <RecommendationDashboard />
        </div>
      </main>

      {/* A full site footer can be added here in future — it will sit ABOVE
          the bottom bar below. */}

      {/* Bottom bar — always pinned to the very bottom of the page */}
      <footer className="border-t border-gem-border bg-white">
        <div className="gem-wrap max-w-6xl py-3 text-xs text-gem-muted flex flex-wrap items-center justify-between gap-2">
          <span>GeM Analytics · Internal analytics portal. Data sourced from bidplus.gem.gov.in.</span>
          <span>Fully local and explainable matching</span>
        </div>
      </footer>
    </div>
  );
}

export default App;
