import { useState, useRef } from "react";

const API = ""; // same-origin; Vite proxies /api -> Flask (see vite.config.js)
const BID_RE = /^GEM\/\d{4}\/[A-Z]\/\d+$/i;

// ≥70 green · 40–69 amber · <40 red
function scoreStyle(pct) {
  if (pct >= 70) return "text-gem-green bg-green-50 border-green-300";
  if (pct >= 40) return "text-amber-700 bg-amber-50 border-amber-300";
  return "text-gem-red bg-red-50 border-red-300";
}

let _id = 0;

export default function BidLookupDashboard() {
  const [query, setQuery]     = useState("");
  const [rows, setRows]       = useState([]);
  const [loading, setLoading] = useState(false);
  const [note, setNote]       = useState("");
  const [highlight, setHighlight] = useState(null);
  const highlightTimer = useRef(null);

  const runLookup = async () => {
    const bidNo = query.trim().toUpperCase();
    if (!BID_RE.test(bidNo)) {
      setNote("Please enter a complete bid number, for example GEM/2026/B/1234567. Partial text filters the list below.");
      return;
    }
    setNote(""); setLoading(true);
    try {
      const res  = await fetch(`${API}/api/lookup`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bidNo }),
      });
      const data = await res.json();
      if (!res.ok) { setNote(data.error || "Lookup failed."); setLoading(false); return; }
      const row = { id: ++_id, ...data };
      setRows((prev) => [row, ...prev.filter((r) => r.bidNo !== data.bidNo)]);
      setHighlight(row.id);
      clearTimeout(highlightTimer.current);
      highlightTimer.current = setTimeout(() => setHighlight(null), 2500);
    } catch (err) { setNote(String(err)); }
    setLoading(false);
  };

  const onKey = (e) => { if (e.key === "Enter") runLookup(); };
  const filtered = rows.filter((r) => r.bidNo.includes(query.trim().toUpperCase()));

  return (
    <div className="gem-wrap max-w-5xl py-8 space-y-6">
      <div>
        <h1 className="gem-title">Bid Lookup</h1>
        <p className="gem-sub">
          Enter a bid number to retrieve and classify it in real time. Custom Bids are matched against the
          category reference list. Category Bids are identified as existing catalogue categories.
        </p>
      </div>

      {/* Search */}
      <div className="gem-card gem-card-pad space-y-3">
        <label className="gem-label">Bid number</label>
        <div className="flex gap-3">
          <input type="text" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey}
                 placeholder="GEM/2026/B/1234567" className="gem-input font-mono flex-1" />
          <button onClick={runLookup} disabled={loading} className="gem-btn gem-btn-primary px-6">
            {loading ? "Searching" : "Search"}
          </button>
        </div>
        <p className="gem-help">A complete bid number runs a live lookup. Partial text filters the results below.</p>
        {note && <p className="text-xs text-amber-700">{note}</p>}
        <div className="flex flex-wrap gap-4 text-xs text-gem-muted pt-1">
          <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-gem-green" /> 70% and above (close)</span>
          <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-amber-500" /> 40 to 69% (review)</span>
          <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-gem-red" /> Below 40% (weak)</span>
        </div>
      </div>

      {/* Results */}
      {filtered.length > 0 ? (
        <div className="gem-card overflow-hidden">
          <div className="overflow-x-auto">
            <table className="gem-table">
              <thead>
                <tr>
                  <th>Bid No</th><th>Type</th><th>Item Category</th>
                  <th>Category Match</th><th>Score</th><th>Layer</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => {
                  const m = r.match;
                  const hl = r.id === highlight;
                  return (
                    <tr key={r.id} className={hl ? "bg-blue-50" : ""}>
                      <td className="font-mono text-xs text-gem-blue whitespace-nowrap">{r.bidNo}</td>
                      <td className="whitespace-nowrap">
                        {!r.found ? <span className="text-gem-muted text-xs">not active</span>
                         : r.classification === "category"
                           ? <span className="gem-badge bg-slate-100 text-gem-muted border-gem-border">Category</span>
                           : <span className="gem-badge bg-blue-50 text-gem-blue border-blue-200">Custom</span>}
                      </td>
                      <td className="text-gem-text max-w-xs">
                        {r.classification === "custom" ? (r.itemCategory || "—") : "—"}
                      </td>
                      {!r.found ? (
                        <td className="text-gem-muted text-xs" colSpan={3}>{r.message}</td>
                      ) : r.classification === "category" ? (
                        <td className="text-gem-muted text-xs" colSpan={3}>Already an existing category bid</td>
                      ) : (
                        <>
                          <td className="text-gem-text max-w-xs">{m?.category || "—"}</td>
                          <td className="whitespace-nowrap">
                            {m ? <span className={`gem-badge ${scoreStyle(m.score)}`}>{m.score}%</span> : "—"}
                          </td>
                          <td className="whitespace-nowrap">
                            {m ? <span className="gem-badge bg-slate-100 text-gem-muted border-gem-border font-normal">{m.layer}</span> : "—"}
                          </td>
                        </>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        rows.length > 0 && <p className="text-center text-gem-muted text-sm">No results match “{query}”.</p>
      )}
    </div>
  );
}
