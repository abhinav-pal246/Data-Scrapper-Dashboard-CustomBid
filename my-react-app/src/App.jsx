import { useState } from "react";
import "./styles.css";
import ExtractorDashboard from "./components/ExtractorDashboard.jsx";
import ComparisonDashboard from "./components/ComparisonDashboard.jsx";

const TABS = [
  { id: "custom",   label: "Custom Bid Extractor" },
  { id: "category", label: "Category Bid Extractor" },
  { id: "compare",  label: "Comparison" },
];

function App() {
  const [tab, setTab] = useState("custom");

  return (
    <div className="App min-h-screen bg-gray-950">
      <nav className="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center gap-2 sticky top-0 z-10">
        <span className="text-white font-bold mr-4">GeM Dashboard</span>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              tab === t.id
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:text-white hover:bg-gray-800"
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {tab === "custom" && (
        <ExtractorDashboard
          module="custom"
          title="GeM Custom Bid Extractor"
          subtitle="Extract GeMARPTS data from active Product Custom Bid/RAs"
          accent="blue"
        />
      )}
      {tab === "category" && (
        <ExtractorDashboard
          module="category"
          title="GeM Category Bid Extractor"
          subtitle="Extract Item Category from active standard Category Bids only"
          accent="teal"
        />
      )}
      {tab === "compare" && <ComparisonDashboard />}
    </div>
  );
}

export default App;
