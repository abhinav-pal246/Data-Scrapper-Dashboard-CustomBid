import { useState, useEffect, useRef } from "react";

const API = "http://127.0.0.1:5000";

// Reusable extractor UI for both Custom Bid and Category Bid modules.
// Start = fresh · Pause = stop & keep (resume later) · Cancel = discard & reset.
export default function ExtractorDashboard({ module, title, subtitle, accent = "blue" }) {
  const base = `${API}/api/extract/${module}`;

  const [count, setCount]     = useState("");
  const [allBids, setAllBids] = useState(false);
  const [status, setStatus]   = useState("idle"); // idle|running|paused|done|error
  const [job, setJob]         = useState({ phase: "", collected: 0, total: 0, written: 0, failed: 0, paused: false });
  const [timeLeft, setTimeLeft] = useState(null);
  const startRef = useRef(null);
  const pollRef  = useRef(null);

  const ACTIVE = ["running", "paused", "retrying", "hold"];

  // Poll while the job is active (running / paused / retrying / hold).
  useEffect(() => {
    if (!ACTIVE.includes(status)) return;
    pollRef.current = setInterval(async () => {
      try {
        const data = await (await fetch(`${base}/status`)).json();
        setJob(data);

        if (data.phase === "extracting" && data.written > 0 && data.status === "running") {
          const elapsed = (Date.now() - startRef.current) / 1000;
          const rate    = data.written / elapsed;
          if (rate > 0 && data.total > 0)
            setTimeLeft(Math.round((data.total - data.written) / rate));
        }

        if (data.done) {
          clearInterval(pollRef.current);
          setStatus(data.status === "error" ? "error" : "done");
        } else if (data.status === "idle") {
          clearInterval(pollRef.current);
          setStatus("idle");                 // cancelled → reset
        } else {
          setStatus(data.status);            // running | paused | retrying | hold
        }
      } catch (err) {
        console.error("poll error:", err);   // frontend can't reach Flask — keep trying
      }
    }, 1000);
    return () => clearInterval(pollRef.current);
  }, [status, base]);

  const post = (path) => fetch(`${base}/${path}`, { method: "POST" });

  const handleStart = async () => {
    if (!allBids && (!count || isNaN(count) || Number(count) <= 0)) {
      alert("Please enter a valid number of bids.");
      return;
    }
    startRef.current = Date.now();
    setStatus("running");
    setJob({ phase: "collecting", collected: 0, total: 0, written: 0, failed: 0, paused: false });
    setTimeLeft(null);
    const res = await fetch(`${base}/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: allBids ? "all" : count }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert(d.error || "Could not start.");
      setStatus("idle");
    }
  };

  const handlePause  = () => post("pause");
  const handleResume = () => { startRef.current = Date.now(); post("resume"); setStatus("running"); };
  const handleRetry  = () => { startRef.current = Date.now(); post("retry"); };
  const handleCancel = async () => { await post("cancel"); clearInterval(pollRef.current); reset(); };
  const handleDownload = () => window.open(`${base}/download`, "_blank");

  const reset = () => {
    setStatus("idle");
    setJob({ phase: "", collected: 0, total: 0, written: 0, failed: 0, paused: false });
    setTimeLeft(null);
  };

  const pct = job.total > 0 ? Math.round((job.written / job.total) * 100) : 0;
  const fmt = (s) => (!s || s < 0 ? "calculating…" : s < 60 ? `${s}s left` : `${Math.floor(s / 60)}m ${s % 60}s left`);

  const accentBtn = accent === "teal" ? "bg-teal-600 hover:bg-teal-500" : "bg-blue-600 hover:bg-blue-500";
  const active = ACTIVE.includes(status);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex items-start justify-center p-6">
      <div className="w-full max-w-xl bg-gray-900 rounded-2xl shadow-2xl p-8 space-y-6 mt-6">
        <div>
          <h1 className="text-2xl font-bold text-white">{title}</h1>
          <p className="text-gray-400 text-sm mt-1">{subtitle}</p>
        </div>

        {/* IDLE */}
        {status === "idle" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm text-gray-300">Number of bids to retrieve</label>
              <input
                type="number" value={count} onChange={(e) => setCount(e.target.value)}
                disabled={allBids} placeholder="e.g. 20"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-white
                           placeholder-gray-500 focus:outline-none focus:border-blue-500 disabled:opacity-40"
              />
            </div>
            <label className="flex items-center gap-3 cursor-pointer">
              <input type="checkbox" checked={allBids} onChange={(e) => setAllBids(e.target.checked)}
                     className="w-4 h-4 accent-blue-500" />
              <span className="text-sm text-gray-300">Retrieve ALL active bids</span>
            </label>
            <button onClick={handleStart}
              className={`w-full ${accentBtn} text-white font-semibold py-3 rounded-lg transition-colors`}>
              Start Extraction
            </button>
          </div>
        )}

        {/* RUNNING / PAUSED / RETRYING / HOLD */}
        {active && (
          <div className="space-y-5">
            {/* Network banner */}
            {status === "retrying" && (
              <div className="rounded-lg p-3 border border-yellow-600 bg-yellow-950/60 flex items-center gap-3">
                <div className="w-4 h-4 border-2 border-yellow-400 border-t-transparent rounded-full animate-spin" />
                <p className="text-xs text-yellow-200">
                  Network/server issue — auto-retrying (attempt {job.attempt}/{job.max_attempts})…
                  {job.hold_reason ? ` [${job.hold_reason}]` : ""} Progress is preserved.
                </p>
              </div>
            )}
            {status === "hold" && (
              <div className="rounded-lg p-4 border border-red-600 bg-red-950/60 space-y-1">
                <p className="text-sm font-semibold text-red-300">⏸ On hold — couldn’t reach GeM</p>
                <p className="text-xs text-red-200/80">
                  Auto-retry failed after {job.max_attempts} attempts{job.hold_reason ? ` (${job.hold_reason})` : ""}.
                  Your progress is safe. Retry when your connection/GeM is back, or export what’s collected.
                </p>
              </div>
            )}

            <div className={`rounded-lg p-4 border ${
              status === "paused" ? "border-amber-500 bg-amber-950"
              : job.phase === "collecting" ? "border-blue-500 bg-blue-950"
              : "border-gray-700 bg-gray-800 opacity-60"}`}>
              <div className="flex items-center gap-3">
                {status !== "paused" && job.phase === "collecting"
                  ? <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
                  : <div className="text-green-400 font-bold">✓</div>}
                <div>
                  <p className="text-sm font-semibold text-white">Phase 1 — Collecting active bid IDs</p>
                  <p className="text-xs text-gray-400 mt-0.5">{job.collected} IDs scanned</p>
                </div>
              </div>
            </div>

            <div className={`rounded-lg p-4 border ${
              status === "paused" ? "border-amber-500 bg-amber-950" : "border-purple-500 bg-purple-950"}`}>
              <div className="flex items-center gap-3 mb-3">
                {status === "paused"
                  ? <div className="text-amber-400 text-lg leading-none">⏸</div>
                  : <div className="w-4 h-4 border-2 border-purple-400 border-t-transparent rounded-full animate-spin" />}
                <div>
                  <p className="text-sm font-semibold text-white">Phase 2 — Extracting active bids</p>
                  <p className="text-xs text-gray-400 mt-0.5">
                    {job.written} extracted{job.total ? ` of ${job.total}` : ""}
                    {status === "paused" && " · paused"}
                  </p>
                </div>
              </div>
              <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                <div className={`h-3 rounded-full transition-all duration-500 ${status === "paused" ? "bg-amber-500" : "bg-purple-500"}`}
                     style={{ width: `${pct}%` }} />
              </div>
              <div className="flex justify-between text-xs mt-2">
                <span className="text-gray-400">{job.failed > 0 && `${job.failed} skipped (expired/other)`}</span>
                <span className={status === "paused" ? "text-amber-400" : "text-purple-400"}>
                  {pct}%{status !== "paused" && ` · ${fmt(timeLeft)}`}
                </span>
              </div>
            </div>

            {/* Controls */}
            <div className="flex gap-3">
              {status === "running" && (
                <button onClick={handlePause}
                  className="flex-1 bg-amber-600 hover:bg-amber-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
                  Pause
                </button>
              )}
              {status === "paused" && (
                <>
                  <button onClick={handleResume}
                    className="flex-1 bg-green-600 hover:bg-green-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
                    Resume
                  </button>
                  <button onClick={handleDownload}
                    className="flex-1 bg-gray-700 hover:bg-gray-600 text-white py-2.5 rounded-lg text-sm transition-colors">
                    Export so far
                  </button>
                </>
              )}
              {status === "hold" && (
                <>
                  <button onClick={handleRetry}
                    className="flex-1 bg-green-600 hover:bg-green-500 text-white font-semibold py-2.5 rounded-lg transition-colors">
                    Retry now
                  </button>
                  <button onClick={handleDownload}
                    className="flex-1 bg-gray-700 hover:bg-gray-600 text-white py-2.5 rounded-lg text-sm transition-colors">
                    Extract so far
                  </button>
                </>
              )}
              <button onClick={handleCancel}
                className="flex-1 bg-red-900 hover:bg-red-800 text-red-200 py-2.5 rounded-lg text-sm transition-colors">
                Cancel
              </button>
            </div>

            <p className="text-center text-gray-500 text-xs">
              {status === "paused"
                ? "Paused — data so far is saved. Resume anytime to continue where you left off."
                : status === "hold"
                ? "On hold — retries resume from exactly where they stopped. Nothing is lost."
                : status === "retrying"
                ? "Auto-retrying — the job will hold (not crash) if the connection stays down."
                : "Pause to stop & keep progress · Cancel to discard and pick a new range."}
            </p>
          </div>
        )}

        {/* DONE */}
        {status === "done" && (
          <div className="space-y-4 text-center">
            <div className="text-green-400 text-5xl">✓</div>
            <p className="text-white font-semibold text-lg">Extraction complete</p>
            <p className="text-gray-400 text-sm">
              {job.written} active bids extracted{job.failed > 0 && `, ${job.failed} skipped`}
            </p>
            <button onClick={handleDownload}
              className="w-full bg-green-600 hover:bg-green-500 text-white font-semibold py-3 rounded-lg transition-colors">
              Download CSV
            </button>
            <button onClick={reset}
              className="w-full bg-gray-700 hover:bg-gray-600 text-white py-2 rounded-lg text-sm transition-colors">
              Start new extraction
            </button>
          </div>
        )}

        {/* ERROR */}
        {status === "error" && (
          <div className="space-y-4 text-center">
            <div className="text-red-400 text-5xl">✗</div>
            <p className="text-red-400 font-semibold">Something went wrong</p>
            <p className="text-gray-500 text-xs break-all">{job.error}</p>
            <button onClick={reset}
              className="w-full bg-gray-700 hover:bg-gray-600 text-white py-2 rounded-lg text-sm transition-colors">
              Try again
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
