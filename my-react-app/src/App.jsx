import { useState } from "react";
import "./styles.css";
import CustomBidDashboard from "./components/CustomBidDashboard.jsx";
import ComparisonDashboard from "./components/ComparisonDashboard.jsx";

function App() {
  const [tab, setTab] = useState("extract"); // extract | compare

  return (
    <div className="App min-h-screen bg-gray-950">
      {/* Top nav */}
      <nav className="bg-gray-900 border-b border-gray-800 px-6 py-3 flex items-center gap-2 sticky top-0 z-10">
        <span className="text-white font-bold mr-4">GeM Dashboard</span>
        <button
          onClick={() => setTab("extract")}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            tab === "extract"
              ? "bg-blue-600 text-white"
              : "text-gray-400 hover:text-white hover:bg-gray-800"
          }`}
        >
          Custom Bid Extractor
        </button>
        <button
          onClick={() => setTab("compare")}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            tab === "compare"
              ? "bg-blue-600 text-white"
              : "text-gray-400 hover:text-white hover:bg-gray-800"
          }`}
        >
          Comparison
        </button>
      </nav>

      {tab === "extract" ? <CustomBidDashboard /> : <ComparisonDashboard />}
    </div>
  );
}

export default App;
