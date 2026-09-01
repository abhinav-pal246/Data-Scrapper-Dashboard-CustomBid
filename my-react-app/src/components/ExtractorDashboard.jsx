import { useState, useEffect, useRef } from "react";

const API = ""; // same-origin; Vite proxies /api -> Flask (see vite.config.js)

// Reusable extractor UI for both Custom Bid and Category Bid modules.
// Start = fresh · Pause = stop & keep (resume later) · Cancel = discard & reset.
export default function ExtractorDashboard({ module, title, subtitle, accent = "blue" }) {
  const base = `${API}/api/extract/${module}`;

  const [count, setCount]     = useState("");
  const [allBids, setAllBids] = useState(false);
  const [status, setStatus]   = useState("idle"); // idle|running|paused|retrying|hold|done|error
  const [job, setJob]         = useState({ phase: "", collected: 0, total: 0, written: 0, failed: 0, paused: false });
  const [timeLeft, setTimeLeft] = useState(null);
  const startRef = useRef(null);
  const pollRef  = useRef(null);

  const ACTIVE = ["running", "paused", "retrying", "hold"];

  useEffect(() => {
    if (!ACTIVE.includes(status)) return;
    pollRef.current = setInterval(async () => {
      try {
        const data = await (await fetch(`${base}/status`)).json();
        setJob(data);
        if (data.phase === "extracting" && data.written > 0 && data.status === "running") {
          const elapsed = (Date.now() - startRef.current) / 1000;
          const rate    = data.written / elapsed;
          if (rate > 0 && data.total > 0) setTimeLeft(Math.round((data.total - data.written) / rate));
        }
        if (data.done) {
          clearInterval(pollRef.current);
          setStatus(data.status === "error" ? "error" : "done");
        } else if (data.status === "idle") {
          clearInterval(pollRef.current);
          setStatus("idle");
        } else {
          setStatus(data.status);
        }
      } catch (err) { console.error("poll error:", err); }
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
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: allBids ? "all" : count }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alert(d.error || "Could not start."); setStatus("idle");
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
  const active = ACTIVE.includes(status);
  const startBtn = accent === "green" ? "gem-btn-success" : "gem-btn-primary";
  const barColor = status === "paused" ? "bg-amber-500" : accent === "green" ? "bg-gem-green" : "bg-gem-blue";

  return (
    <div className="gem-wrap max-w-3xl py-8">
      <div className="gem-card gem-card-accent gem-card-pad space-y-6">

        <div>
          <h1 className="gem-title">{title}</h1>
          <p className="gem-sub">{subtitle}</p>
        </div>

        {/* IDLE */}
        {status === "idle" && (
          <div className="space-y-4">
            <div>
              <label className="gem-label">Number of bids to retrieve</label>
              <input type="number" value={count} onChange={(e) => setCount(e.target.value)}
                     disabled={allBids} placeholder="For example, 20" className="gem-input" />
            </div>
            <label className="flex items-center gap-3 cursor-pointer">
              <input type="checkbox" checked={allBids} onChange={(e) => setAllBids(e.target.checked)}
                     className="w-4 h-4 accent-gem-blue" />
              <span className="text-sm text-gem-text">Retrieve all active bids</span>
            </label>
            <button onClick={handleStart} className={`gem-btn ${startBtn} w-full py-3`}>
              Start Extraction
            </button>
          </div>
        )}

        {/* RUNNING / PAUSED / RETRYING / HOLD */}
        {active && (
          <div className="space-y-5">
            {status === "retrying" && (
              <div className="rounded-md p-3 border border-amber-300 bg-amber-50 flex items-center gap-3">
                <div className="w-4 h-4 border-2 border-amber-500 border-t-transparent rounded-full animate-spin" />
                <p className="text-xs text-amber-800">
                  A network or server issue occurred. Retrying automatically (attempt {job.attempt} of {job.max_attempts}).
                  Your progress is preserved.
                </p>
              </div>
            )}
            {status === "hold" && (
              <div className="rounded-md p-4 border border-red-300 bg-red-50 space-y-1">
                <p className="text-sm font-semibold text-gem-red">On hold. Unable to reach GeM.</p>
                <p className="text-xs text-red-700/90">
                  Automatic retries were unsuccessful after {job.max_attempts} attempts.
                  Your progress is saved. Retry once the connection is restored, or export the data collected so far.
                </p>
              </div>
            )}

            {/* Phase 1 */}
            <div className={`rounded-md p-4 border ${
              status === "paused" ? "border-amber-300 bg-amber-50"
              : job.phase === "collecting" ? "border-gem-blue/40 bg-blue-50"
              : "border-gem-border bg-slate-50"}`}>
              <div className="flex items-center gap-3">
                {status !== "paused" && job.phase === "collecting"
                  ? <div className="w-4 h-4 border-2 border-gem-blue border-t-transparent rounded-full animate-spin" />
                  : <div className="text-gem-green font-bold">✓</div>}
                <div>
                  <p className="text-sm font-semibold text-gem-text">Step 1. Collecting active bid identifiers</p>
                  <p className="text-xs text-gem-muted mt-0.5">{job.collected} identifiers scanned</p>
                </div>
              </div>
            </div>

            {/* Phase 2 */}
            <div className={`rounded-md p-4 border ${
              status === "paused" ? "border-amber-300 bg-amber-50" : "border-gem-border bg-white"}`}>
              <div className="flex items-center gap-3 mb-3">
                {status === "paused"
                  ? <div className="text-amber-500 text-lg leading-none">⏸</div>
                  : <div className={`w-4 h-4 border-2 border-t-transparent rounded-full animate-spin ${accent === "green" ? "border-gem-green" : "border-gem-blue"}`} />}
                <div>
                  <p className="text-sm font-semibold text-gem-text">Step 2. Extracting active bids</p>
                  <p className="text-xs text-gem-muted mt-0.5">
                    {job.written} extracted{job.total ? ` of ${job.total}` : ""}{status === "paused" && " · paused"}
                  </p>
                </div>
              </div>
              <div className="w-full bg-slate-200 rounded-full h-2.5 overflow-hidden">
                <div className={`h-2.5 rounded-full transition-all duration-500 ${barColor}`} style={{ width: `${pct}%` }} />
              </div>
              <div className="flex justify-between text-xs mt-2">
                <span className="text-gem-muted">{job.failed > 0 && `${job.failed} skipped`}</span>
                <span className={status === "paused" ? "text-amber-600" : "text-gem-blue"}>
                  {pct}%{status !== "paused" && ` · ${fmt(timeLeft)}`}
                </span>
              </div>
            </div>

            {/* Controls */}
            <div className="flex gap-3">
              {status === "running" && (
                <button onClick={handlePause} className="gem-btn gem-btn-warn flex-1">Pause</button>
              )}
              {status === "paused" && (
                <>
                  <button onClick={handleResume} className="gem-btn gem-btn-success flex-1">Resume</button>
                  <button onClick={handleDownload} className="gem-btn gem-btn-ghost flex-1">Export collected data</button>
                </>
              )}
              {status === "hold" && (
                <>
                  <button onClick={handleRetry} className="gem-btn gem-btn-success flex-1">Retry</button>
                  <button onClick={handleDownload} className="gem-btn gem-btn-ghost flex-1">Export collected data</button>
                </>
              )}
              <button onClick={handleCancel} className="gem-btn gem-btn-danger flex-1">Cancel</button>
            </div>

            <p className="text-center gem-help">
              {status === "paused"
                ? "Paused. The data collected so far is saved. Resume to continue from where you stopped."
                : status === "hold"
                ? "On hold. Retrying resumes from the exact point where it stopped. No data is lost."
                : status === "retrying"
                ? "Retrying automatically. If the connection remains unavailable, the process will pause rather than fail."
                : "Pause to stop and keep your progress. Cancel to discard and choose a new range."}
            </p>
          </div>
        )}

        {/* DONE */}
        {status === "done" && (
          <div className="space-y-4 text-center">
            <div className="mx-auto w-12 h-12 rounded-full bg-green-100 text-gem-green flex items-center justify-center text-2xl">✓</div>
            <p className="text-gem-text font-semibold text-lg">Extraction complete</p>
            <p className="text-gem-muted text-sm">
              {job.written} active bids extracted{job.failed > 0 && `. ${job.failed} skipped`}.
            </p>
            <button onClick={handleDownload} className="gem-btn gem-btn-success w-full py-3">Download CSV</button>
            <button onClick={reset} className="gem-btn gem-btn-ghost w-full">Start a new extraction</button>
          </div>
        )}

        {/* ERROR */}
        {status === "error" && (
          <div className="space-y-4 text-center">
            <div className="mx-auto w-12 h-12 rounded-full bg-red-100 text-gem-red flex items-center justify-center text-2xl">!</div>
            <p className="text-gem-red font-semibold">The extraction could not be completed</p>
            <p className="text-gem-muted text-xs break-all">{job.error}</p>
            <button onClick={reset} className="gem-btn gem-btn-ghost w-full">Try again</button>
          </div>
        )}
      </div>
    </div>
  );
}
