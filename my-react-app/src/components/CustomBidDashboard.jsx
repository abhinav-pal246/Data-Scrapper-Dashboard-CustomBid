import { useState, useEffect, useRef } from "react";

export default function CustomBidDashboard() {
  const [count, setCount]       = useState("");
  const [allBids, setAllBids]   = useState(false);
  const [status, setStatus]     = useState("idle");
  const [job, setJob]           = useState({
    phase: "", collected: 0, total: 0, written: 0, failed: 0
  });
  const [timeLeft, setTimeLeft] = useState(null);
  const startTimeRef            = useRef(null);
  const intervalRef             = useRef(null);

  // ── Poll Flask every second ───────────────────────────────────────────────
  useEffect(() => {
    if (status !== "running") return;

    intervalRef.current = setInterval(async () => {
      try {
        const res  = await fetch("http://127.0.0.1:5000/api/status");
        const data = await res.json();
        setJob(data);

        // Time remaining only makes sense in extracting phase
        if (data.phase === "extracting" && data.written > 0) {
          const elapsed   = (Date.now() - startTimeRef.current) / 1000;
          const rate      = data.written / elapsed;
          const remaining = (data.total - data.written) / rate;
          setTimeLeft(Math.round(remaining));
        }

        if (data.done) {
          clearInterval(intervalRef.current);
          setStatus(data.status === "error" ? "error" : "done");
        }
      } catch (err) {
        console.error("Poll error:", err);
      }
    }, 1000);

    return () => clearInterval(intervalRef.current);
  }, [status]);

  // ── Start ─────────────────────────────────────────────────────────────────
  const handleStart = async () => {
    if (!allBids && (!count || isNaN(count) || Number(count) <= 0)) {
      alert("Please enter a valid number of bids.");
      return;
    }

    startTimeRef.current = Date.now();
    setStatus("running");
    setJob({ phase: "collecting", collected: 0, total: 0, written: 0, failed: 0 });
    setTimeLeft(null);

    await fetch("http://127.0.0.1:5000/api/start", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ count: allBids ? "all" : count }),
    });
  };

  const handleDownload = () => {
    window.open("http://127.0.0.1:5000/api/download", "_blank");
  };

  const pct = job.total > 0
    ? Math.round((job.written / job.total) * 100)
    : 0;

  const formatTime = (secs) => {
    if (!secs || secs < 0) return "calculating...";
    if (secs < 60) return `${secs}s remaining`;
    return `${Math.floor(secs / 60)}m ${secs % 60}s remaining`;
  };

  return (
    <div className="min-h-screen bg-gray-950 text-white flex items-center justify-center p-6">
      <div className="w-full max-w-xl bg-gray-900 rounded-2xl shadow-2xl p-8 space-y-6">

        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold text-white">GeM Custom Bid Extractor</h1>
          <p className="text-gray-400 text-sm mt-1">
            Extract GeMARPTS data from Product Custom Bid/RAs
          </p>
        </div>

        {/* ── IDLE ── */}
        {status === "idle" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm text-gray-300">
                Number of bids to retrieve
              </label>
              <input
                type="number"
                value={count}
                onChange={(e) => setCount(e.target.value)}
                disabled={allBids}
                placeholder="e.g. 20"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg
                           px-4 py-3 text-white placeholder-gray-500
                           focus:outline-none focus:border-blue-500 disabled:opacity-40"
              />
            </div>

            <label className="flex items-center gap-3 cursor-pointer">
              <input
                type="checkbox"
                checked={allBids}
                onChange={(e) => setAllBids(e.target.checked)}
                className="w-4 h-4 accent-blue-500"
              />
              <span className="text-sm text-gray-300">Retrieve ALL bids</span>
            </label>

            <button
              onClick={handleStart}
              className="w-full bg-blue-600 hover:bg-blue-500 text-white
                         font-semibold py-3 rounded-lg transition-colors"
            >
              Start Extraction
            </button>
          </div>
        )}

        {/* ── RUNNING ── */}
        {status === "running" && (
          <div className="space-y-5">

            {/* Phase 1: Collecting IDs */}
            <div className={`rounded-lg p-4 border transition-all ${
              job.phase === "collecting"
                ? "border-blue-500 bg-blue-950"
                : "border-gray-700 bg-gray-800 opacity-50"
            }`}>
              <div className="flex items-center gap-3">
                {job.phase === "collecting" ? (
                  <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent
                                  rounded-full animate-spin" />
                ) : (
                  <div className="text-green-400 font-bold">✓</div>
                )}
                <div>
                  <p className="text-sm font-semibold text-white">
                    Phase 1 — Collecting active bid IDs
                  </p>
                  <p className="text-xs text-gray-400 mt-0.5">
                    {job.phase === "collecting"
                      ? `${job.collected} IDs found so far...`
                      : `${job.total} IDs collected`}
                  </p>
                </div>
              </div>
            </div>

            {/* Phase 2: Extracting data */}
            <div className={`rounded-lg p-4 border transition-all ${
              job.phase === "extracting"
                ? "border-purple-500 bg-purple-950"
                : "border-gray-700 bg-gray-800 opacity-40"
            }`}>
              <div className="flex items-center gap-3 mb-3">
                {job.phase === "extracting" ? (
                  <div className="w-4 h-4 border-2 border-purple-400 border-t-transparent
                                  rounded-full animate-spin" />
                ) : (
                  <div className="w-4 h-4 rounded-full border-2 border-gray-600" />
                )}
                <div>
                  <p className="text-sm font-semibold text-white">
                    Phase 2 — Extracting GeMARPTS data
                  </p>
                  <p className="text-xs text-gray-400 mt-0.5">
                    {job.phase === "extracting"
                      ? `${job.written} of ${job.total} bids done`
                      : "Waiting for phase 1..."}
                  </p>
                </div>
              </div>

              {job.phase === "extracting" && (
                <>
                  <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                    <div
                      className="bg-purple-500 h-3 rounded-full transition-all duration-500"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <div className="flex justify-between text-xs mt-2">
                    <span className="text-gray-400">
                      {job.failed > 0 && `${job.failed} failed`}
                    </span>
                    <span className="text-purple-400">{pct}% · {formatTime(timeLeft)}</span>
                  </div>
                </>
              )}
            </div>

            <p className="text-center text-gray-500 text-xs animate-pulse">
              Do not close this window
            </p>
          </div>
        )}

        {/* ── DONE ── */}
        {status === "done" && (
          <div className="space-y-4 text-center">
            <div className="text-green-400 text-5xl">✓</div>
            <p className="text-white font-semibold text-lg">Extraction complete</p>
            <p className="text-gray-400 text-sm">
              {job.written} bids extracted
              {job.failed > 0 && `, ${job.failed} failed (expired bids)`}
            </p>
            <button
              onClick={handleDownload}
              className="w-full bg-green-600 hover:bg-green-500 text-white
                         font-semibold py-3 rounded-lg transition-colors"
            >
              Download CSV
            </button>
            <button
              onClick={() => { setStatus("idle"); setJob({ phase: "", collected: 0, total: 0, written: 0, failed: 0 }); }}
              className="w-full bg-gray-700 hover:bg-gray-600 text-white
                         py-2 rounded-lg text-sm transition-colors"
            >
              Start new extraction
            </button>
          </div>
        )}

        {/* ── ERROR ── */}
        {status === "error" && (
          <div className="space-y-4 text-center">
            <div className="text-red-400 text-5xl">✗</div>
            <p className="text-red-400 font-semibold">Something went wrong</p>
            <p className="text-gray-500 text-xs">{job.error}</p>
            <button
              onClick={() => setStatus("idle")}
              className="w-full bg-gray-700 hover:bg-gray-600 text-white
                         py-2 rounded-lg text-sm transition-colors"
            >
              Try again
            </button>
          </div>
        )}

      </div>
    </div>
  );
}