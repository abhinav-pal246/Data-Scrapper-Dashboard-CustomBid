import { useState, useEffect, useRef } from "react";

const API = "http://127.0.0.1:5000";

// A confident match cleared a threshold (its reason names the layer that hit).
const isConfident = (row) => /Layer [123]/.test(row["Why It Could Belong"] || "");

// Colour-code the verdict that is embedded in the reason string.
function verdictStyle(row) {
  const reason = row["Why It Could Belong"] || "";
  const matched = row["Matched Category"] && row["Matched Category"] !== "—";
  if (!matched) return "bg-gray-800 text-gray-400 border-gray-700";
  if (reason.includes("Layer 1")) return "bg-emerald-950 text-emerald-300 border-emerald-700";
  if (reason.includes("Layer 2")) return "bg-blue-950 text-blue-300 border-blue-700";
  if (reason.includes("Layer 3")) return "bg-purple-950 text-purple-300 border-purple-700";
  if (reason.includes("below confidence")) return "bg-amber-950 text-amber-300 border-amber-700";
  return "bg-gray-800 text-gray-300 border-gray-700";
}

export default function ComparisonDashboard() {
  const [customCount, setCustomCount]     = useState("");
  const [categoryCount, setCategoryCount] = useState("");
  const [compareAll, setCompareAll]       = useState(false);

  const [status, setStatus] = useState("idle"); // idle | running | done | error
  const [job, setJob]       = useState(null);   // progress snapshot
  const [result, setResult] = useState(null);   // { total, inline, rows }
  const [error, setError]   = useState("");
  const pollRef             = useRef(null);

  // ── Poll progress while running ───────────────────────────────────────────
  useEffect(() => {
    if (status !== "running") return;

    pollRef.current = setInterval(async () => {
      try {
        const res  = await fetch(`${API}/api/compare/status`);
        const data = await res.json();
        setJob(data);

        if (data.done) {
          clearInterval(pollRef.current);
          if (data.status === "error") {
            setError(data.error || "Comparison failed.");
            setStatus("error");
            return;
          }
          const r = await fetch(`${API}/api/compare/result`);
          setResult(await r.json());
          setStatus("done");
        }
      } catch (err) {
        console.error("Compare poll error:", err);
      }
    }, 800);

    return () => clearInterval(pollRef.current);
  }, [status]);

  const handleRun = async () => {
    if (!compareAll) {
      const c = Number(customCount);
      const g = Number(categoryCount);
      if (!customCount || isNaN(c) || c <= 0 || !categoryCount || isNaN(g) || g <= 0) {
        alert("Enter a valid number of custom bids and category bids, or turn on Compare All.");
        return;
      }
    }

    setStatus("running");
    setError("");
    setResult(null);
    setJob({ phase: "Starting", total: 0, processed: 0, matched: 0, encoded: 0, encode_total: 0 });

    try {
      const res = await fetch(`${API}/api/compare`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          customCount:   compareAll ? null : Number(customCount),
          categoryCount: compareAll ? null : Number(categoryCount),
          compareAll,
        }),
      });
      if (!res.ok) {
        const data = await res.json();
        setError(data.error || "Could not start comparison.");
        setStatus("error");
      }
    } catch (err) {
      setError(String(err));
      setStatus("error");
    }
  };

  const handleCancel = async () => {
    clearInterval(pollRef.current);
    try {
      await fetch(`${API}/api/compare/cancel`, { method: "POST" });
    } catch (err) {
      console.error("Cancel error:", err);
    }
    reset();
  };

  const handleDownload = () => window.open(`${API}/api/compare/download`, "_blank");

  const reset = () => {
    setStatus("idle");
    setJob(null);
    setResult(null);
    setError("");
  };

  const confidentCount = result?.rows ? result.rows.filter(isConfident).length : null;
  const weakCount = result?.rows
    ? result.rows.filter((r) => !isConfident(r) && r["Matched Category"] !== "—").length
    : null;

  // Progress percentages for each phase.
  const encodePct  = job?.encode_total > 0 ? Math.round((job.encoded / job.encode_total) * 100) : 0;
  const itemPct    = job?.total > 0 ? Math.round((job.processed / job.total) * 100) : 0;
  const collectPct = job?.collect_total > 0 ? Math.round((job.collected / job.collect_total) * 100) : 0;
  const isEncoding   = job?.phase?.startsWith("Encoding");
  const isCollecting = job?.phase?.startsWith("Collecting");

  return (
    <div className="min-h-screen bg-gray-950 text-white flex items-start justify-center p-6">
      <div className="w-full max-w-6xl space-y-6">

        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold text-white">Custom Bid → Category Comparison</h1>
          <p className="text-gray-400 text-sm mt-1">
            Checks which Product Custom Bids could actually have been placed as standard
            Category Bids. Fully local, explainable, three-layer matching — no LLM, no API.
          </p>
        </div>

        {/* Controls */}
        <div className="bg-gray-900 rounded-2xl shadow-2xl p-6 space-y-5">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="space-y-2">
              <label className="text-sm text-gray-300"># of Custom Bids to compare</label>
              <input
                type="number"
                value={customCount}
                onChange={(e) => setCustomCount(e.target.value)}
                disabled={compareAll || status === "running"}
                placeholder="e.g. 50"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3
                           text-white placeholder-gray-500 focus:outline-none
                           focus:border-blue-500 disabled:opacity-40"
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-gray-300"># of Category Bids to compare against</label>
              <input
                type="number"
                value={categoryCount}
                onChange={(e) => setCategoryCount(e.target.value)}
                disabled={compareAll || status === "running"}
                placeholder="e.g. 500"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3
                           text-white placeholder-gray-500 focus:outline-none
                           focus:border-blue-500 disabled:opacity-40"
              />
            </div>
          </div>

          <label className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={compareAll}
              onChange={(e) => setCompareAll(e.target.checked)}
              disabled={status === "running"}
              className="w-4 h-4 accent-blue-500"
            />
            <span className="text-sm text-gray-300">
              Compare <span className="font-semibold text-white">ALL</span> custom bids against{" "}
              <span className="font-semibold text-white">ALL</span> category bids (ignores the numbers above)
            </span>
          </label>

          <button
            onClick={handleRun}
            disabled={status === "running"}
            className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50
                       text-white font-semibold py-3 rounded-lg transition-colors"
          >
            {status === "running" ? "Matching…" : "Run Comparison"}
          </button>

          {/* Layer legend */}
          <div className="flex flex-wrap gap-3 text-xs text-gray-400 pt-1">
            <span className="flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" /> Layer 1 · Strong (fuzzy &gt; 90)
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-full bg-blue-500" /> Layer 2 · Likely (TF-IDF &gt; 0.65)
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-full bg-purple-500" /> Layer 3 · Possible (embed &gt; 0.75)
            </span>
            <span className="flex items-center gap-1.5">
              <span className="w-2.5 h-2.5 rounded-full bg-amber-500" /> Weak · nearest, below threshold — review
            </span>
          </div>
        </div>

        {/* Running — progress */}
        {status === "running" && job && (
          <div className="bg-gray-900 rounded-2xl p-6 space-y-4">
            <div className="flex items-center gap-3">
              <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
              <p className="text-sm font-semibold text-white">{job.phase || "Working…"}</p>
            </div>

            {/* Collecting working custom bids (unbounded Compare All shows a live count) */}
            {isCollecting && !(job.collect_total > 0) && (
              <p className="text-xs text-gray-400">
                <span className="text-cyan-400 font-semibold">{(job.collected || 0).toLocaleString()}</span> working custom bids collected
                {job.failed > 0 && ` · ${job.failed.toLocaleString()} skipped (expired)`}…
              </p>
            )}

            {/* Custom-bid collection progress (auto-scraped on demand) */}
            {isCollecting && job.collect_total > 0 && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                  <div className="bg-cyan-500 h-3 rounded-full transition-all duration-300"
                       style={{ width: `${collectPct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>
                    {job.collected.toLocaleString()} / {job.collect_total.toLocaleString()} working custom bids collected
                    {job.failed > 0 && ` · ${job.failed.toLocaleString()} skipped (expired)`}
                  </span>
                  <span className="text-cyan-400">{collectPct}%</span>
                </div>
              </div>
            )}

            {/* Item matching progress */}
            {!isCollecting && job.total > 0 && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                  <div className="bg-blue-500 h-3 rounded-full transition-all duration-300"
                       style={{ width: `${itemPct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>{job.processed.toLocaleString()} / {job.total.toLocaleString()} custom bids</span>
                  <span className="text-blue-400">{itemPct}%</span>
                </div>
              </div>
            )}

            {/* Embedding encode progress (the heavy phase at scale) */}
            {isEncoding && job.encode_total > 0 && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-2 overflow-hidden">
                  <div className="bg-purple-500 h-2 rounded-full transition-all duration-300"
                       style={{ width: `${encodePct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>{job.phase} · {job.encoded.toLocaleString()} / {job.encode_total.toLocaleString()}</span>
                  <span className="text-purple-400">{encodePct}%</span>
                </div>
              </div>
            )}

            <p className="text-center text-gray-500 text-xs">
              {isCollecting
                ? "Collecting working custom bids live from GeM (rate-limited ~1.5s each); expired bids are skipped and it keeps going until it has the number you asked for. Already-collected bids are cached and reused instantly."
                : "Category embeddings are cached — the first big run is the slow one; later runs reuse them."}
            </p>

            <div className="flex justify-center">
              <button
                onClick={handleCancel}
                className="bg-gray-700 hover:bg-gray-600 text-white px-5 py-2 rounded-lg text-sm transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Error */}
        {status === "error" && (
          <div className="bg-gray-900 rounded-2xl p-6 text-center space-y-3">
            <div className="text-red-400 text-3xl">✗</div>
            <p className="text-red-400 font-semibold">Comparison failed</p>
            <p className="text-gray-500 text-xs break-all">{error}</p>
            <button onClick={reset} className="bg-gray-700 hover:bg-gray-600 text-white px-4 py-2 rounded-lg text-sm">
              Try again
            </button>
          </div>
        )}

        {/* Done */}
        {status === "done" && result && (
          <div className="space-y-4">

            {/* Summary bar */}
            <div className="bg-gray-900 rounded-2xl p-5 flex flex-wrap items-center justify-between gap-4">
              <div className="flex flex-wrap gap-6">
                <div>
                  <p className="text-2xl font-bold text-white">{result.total.toLocaleString()}</p>
                  <p className="text-xs text-gray-400">Custom bids compared</p>
                </div>
                {confidentCount !== null && (
                  <div>
                    <p className="text-2xl font-bold text-emerald-400">{confidentCount.toLocaleString()}</p>
                    <p className="text-xs text-gray-400">Confident matches</p>
                  </div>
                )}
                {weakCount !== null && (
                  <div>
                    <p className="text-2xl font-bold text-amber-400">{weakCount.toLocaleString()}</p>
                    <p className="text-xs text-gray-400">Weak — needs review</p>
                  </div>
                )}
              </div>
              <div className="flex gap-3">
                <button
                  onClick={handleDownload}
                  className="bg-green-600 hover:bg-green-500 text-white font-semibold px-5 py-2.5 rounded-lg transition-colors"
                >
                  Download CSV
                </button>
                <button
                  onClick={reset}
                  className="bg-gray-700 hover:bg-gray-600 text-white px-4 py-2.5 rounded-lg text-sm transition-colors"
                >
                  New comparison
                </button>
              </div>
            </div>

            {/* Large result — download only */}
            {!result.inline && (
              <div className="bg-gray-900 rounded-2xl p-8 text-center space-y-3">
                <div className="text-4xl">📄</div>
                <p className="text-white font-semibold">
                  {result.total.toLocaleString()} rows — too large to display here
                </p>
                <p className="text-gray-400 text-sm max-w-md mx-auto">
                  Results above 300 rows aren't rendered in the dashboard to keep it fast.
                  Use <span className="text-green-400 font-medium">Download CSV</span> to get the
                  full comparison file with every Custom Bid No and Category Bid No.
                </p>
              </div>
            )}

            {/* Inline table */}
            {result.inline && result.rows && (
              <div className="bg-gray-900 rounded-2xl overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-800 text-gray-300 text-left">
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Custom Bid No</th>
                        <th className="px-4 py-3 font-semibold">Custom Item</th>
                        <th className="px-4 py-3 font-semibold">Matched Category</th>
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Category Bid No</th>
                        <th className="px-4 py-3 font-semibold">Why It Could Belong</th>
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={i} className="border-t border-gray-800 hover:bg-gray-800/50 align-top">
                          <td className="px-4 py-3 font-mono text-xs text-blue-300 whitespace-nowrap">
                            {row["Custom Bid No"]}
                          </td>
                          <td className="px-4 py-3 text-gray-200 max-w-xs">{row["Custom Item"]}</td>
                          <td className="px-4 py-3 text-gray-200 max-w-xs">{row["Matched Category"]}</td>
                          <td className="px-4 py-3 font-mono text-xs text-emerald-300 whitespace-nowrap">
                            {row["Category Bid No"]}
                          </td>
                          <td className="px-4 py-3 text-gray-400 max-w-md text-xs leading-relaxed">
                            {row["Why It Could Belong"]}
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap">
                            <span className={`inline-block px-2.5 py-1 rounded-md border text-xs font-semibold ${verdictStyle(row)}`}>
                              {row["Score"]}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

      </div>
    </div>
  );
}
