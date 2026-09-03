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

  // ── Delivery-state filter (empty selection = All States, i.e. no filter) ──
  const [stateOptions, setStateOptions]     = useState([]);
  const [selectedStates, setSelectedStates] = useState([]);
  const [stateQuery, setStateQuery]         = useState("");
  const [pickerOpen, setPickerOpen]         = useState(false);
  const pickerRef = useRef(null);

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

  // Load the canonical state list once (shared by both extractor modules).
  useEffect(() => {
    fetch(`${API}/api/states`)
      .then((r) => r.json())
      .then((d) => setStateOptions(d.states || []))
      .catch(() => {});
  }, []);

  // Close the state picker on an outside click.
  useEffect(() => {
    if (!pickerOpen) return;
    const onDoc = (e) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target)) setPickerOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [pickerOpen]);

  const toggleState = (s) =>
    setSelectedStates((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  const filteredStateOptions = stateOptions.filter((s) =>
    s.toLowerCase().includes(stateQuery.trim().toLowerCase()));

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
      body: JSON.stringify({ count: allBids ? "all" : count, states: selectedStates }),
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

            {/* State filter — searchable multi-select (empty = All States) */}
            <div ref={pickerRef} className="relative">
              <label className="gem-label">Filter by delivery state</label>
              <button
                type="button"
                onClick={() => setPickerOpen((o) => !o)}
                className="gem-input w-full flex items-center justify-between text-left"
              >
                <span className={selectedStates.length ? "text-gem-text" : "text-gem-muted"}>
                  {selectedStates.length === 0
                    ? "All States"
                    : `${selectedStates.length} state${selectedStates.length > 1 ? "s" : ""} selected`}
                </span>
                <svg className={`w-4 h-4 text-gem-muted shrink-0 transition-transform ${pickerOpen ? "rotate-180" : ""}`}
                     viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path fillRule="evenodd" d="M5.23 7.21a.75.75 0 011.06.02L10 11.17l3.71-3.94a.75.75 0 111.08 1.04l-4.25 4.5a.75.75 0 01-1.08 0l-4.25-4.5a.75.75 0 01.02-1.06z" clipRule="evenodd" />
                </svg>
              </button>

              {pickerOpen && (
                <div className="absolute z-20 mt-1 w-full rounded-lg border border-gem-border bg-white shadow-lg overflow-hidden">
                  <div className="p-2 border-b border-gem-border">
                    <input
                      autoFocus
                      value={stateQuery}
                      onChange={(e) => setStateQuery(e.target.value)}
                      placeholder="Search states"
                      className="gem-input py-1.5 text-sm"
                    />
                  </div>
                  <div className="max-h-56 overflow-y-auto py-1">
                    <button type="button"
                      onClick={() => { setSelectedStates([]); setStateQuery(""); }}
                      className={`w-full flex items-center gap-2.5 px-3 py-1.5 text-sm text-left hover:bg-slate-50 ${selectedStates.length === 0 ? "text-gem-blue font-medium" : "text-gem-text"}`}>
                      <span className={`w-4 h-4 rounded border flex items-center justify-center text-[10px] leading-none ${selectedStates.length === 0 ? "bg-gem-blue border-gem-blue text-white" : "border-gem-border"}`}>
                        {selectedStates.length === 0 && "✓"}
                      </span>
                      All States
                    </button>
                    {filteredStateOptions.map((s) => {
                      const on = selectedStates.includes(s);
                      return (
                        <button key={s} type="button" onClick={() => toggleState(s)}
                          className="w-full flex items-center gap-2.5 px-3 py-1.5 text-sm text-left text-gem-text hover:bg-slate-50">
                          <span className={`w-4 h-4 rounded border flex items-center justify-center text-[10px] leading-none ${on ? "bg-gem-blue border-gem-blue text-white" : "border-gem-border"}`}>
                            {on && "✓"}
                          </span>
                          {s}
                        </button>
                      );
                    })}
                    {filteredStateOptions.length === 0 && (
                      <p className="px-3 py-2 text-xs text-gem-muted">No states match “{stateQuery}”.</p>
                    )}
                  </div>
                  {selectedStates.length > 0 && (
                    <div className="flex items-center justify-between px-3 py-2 border-t border-gem-border bg-slate-50">
                      <span className="text-xs text-gem-muted">{selectedStates.length} selected</span>
                      <button type="button" onClick={() => setSelectedStates([])}
                              className="text-xs text-gem-link underline">Clear all</button>
                    </div>
                  )}
                </div>
              )}

              {selectedStates.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {selectedStates.map((s) => (
                    <span key={s} className="gem-badge text-gem-blue bg-blue-50 border-blue-200 inline-flex items-center gap-1">
                      {s}
                      <button type="button" onClick={() => toggleState(s)}
                              className="text-gem-blue/60 hover:text-gem-blue leading-none text-sm">×</button>
                    </span>
                  ))}
                </div>
              )}

              <p className="gem-help mt-1.5">
                {selectedStates.length === 0
                  ? "Extracting from all states. Select one or more states to keep only bids delivered there."
                  : `Only bids delivered to the selected ${selectedStates.length > 1 ? "states" : "state"} are kept — the count above is how many matching bids to collect. State is derived from each bid’s consignee address.`}
              </p>
            </div>

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
                <span className="text-gem-muted">
                  {[job.filtered > 0 && `${job.filtered} other states`,
                    job.failed > 0 && `${job.failed} skipped`].filter(Boolean).join(" · ")}
                </span>
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
              {job.written} active bids extracted
              {job.filtered > 0 && `. ${job.filtered} in other states`}
              {job.failed > 0 && `. ${job.failed} skipped`}.
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
