import { useState, useEffect, useRef } from "react";

const API = "http://127.0.0.1:5000";

const CONFIDENT = ["Strong match", "Likely match", "Possible match"];
const isConfident = (row) => CONFIDENT.includes(row["Match Label"]);

// Colour-code by the match strength label.
function verdictStyle(row) {
  const label = row["Match Label"] || "";
  if (label === "Strong match")   return "bg-emerald-950 text-emerald-300 border-emerald-700";
  if (label === "Likely match")   return "bg-blue-950 text-blue-300 border-blue-700";
  if (label === "Possible match") return "bg-purple-950 text-purple-300 border-purple-700";
  if (label.startsWith("Weak"))   return "bg-amber-950 text-amber-300 border-amber-700";
  return "bg-gray-800 text-gray-400 border-gray-700";
}

export default function ComparisonDashboard() {
  const [customCount, setCustomCount] = useState("");
  const [compareAll, setCompareAll]   = useState(false);

  const [status, setStatus] = useState("idle"); // idle | running | done | error
  const [phase, setPhase]   = useState("extract"); // extract | compare (while running)
  const [extractJob, setExtractJob] = useState(null); // fresh-extraction progress
  const [job, setJob]       = useState(null);   // comparison progress
  const [result, setResult] = useState(null);
  const [error, setError]   = useState("");

  const pollRef        = useRef(null);
  const phaseRef       = useRef("extract");     // synchronous phase for the poller
  const compareStarted = useRef(false);

  // ── Poll the active phase while running ────────────────────────────────────
  useEffect(() => {
    if (status !== "running") return;

    pollRef.current = setInterval(async () => {
      try {
        // ── Phase 1: fresh Custom Bid extraction ──
        if (phaseRef.current === "extract") {
          const data = await (await fetch(`${API}/api/extract/custom/status`)).json();
          setExtractJob(data);
          if (data.done) {
            if (data.status === "error") {
              setError(data.error || "Custom Bid extraction failed.");
              setStatus("error");
              clearInterval(pollRef.current);
              return;
            }
            // Extraction finished → kick off the comparison over the fresh data.
            if (!compareStarted.current) {
              compareStarted.current = true;
              phaseRef.current = "compare";
              setPhase("compare");
              setJob({ phase: "Starting", total: 0, processed: 0, encoded: 0, encode_total: 0 });
              const res = await fetch(`${API}/api/compare`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ compareAll: true }), // compare all freshly-extracted
              });
              if (!res.ok) {
                const d = await res.json().catch(() => ({}));
                setError(d.error || "Could not start comparison.");
                setStatus("error");
                clearInterval(pollRef.current);
              }
            }
          }
          return;
        }

        // ── Phase 2: comparison ──
        const data = await (await fetch(`${API}/api/compare/status`)).json();
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
        console.error("poll error:", err);
      }
    }, 800);

    return () => clearInterval(pollRef.current);
  }, [status]);

  const handleRun = async () => {
    if (!compareAll) {
      const c = Number(customCount);
      if (!customCount || isNaN(c) || c <= 0) {
        alert("Enter a valid number of custom bids to extract, or turn on Compare All.");
        return;
      }
    }
    setError("");
    setResult(null);
    setJob(null);
    setExtractJob({ phase: "collecting", written: 0, total: 0, collected: 0, failed: 0 });
    compareStarted.current = false;
    phaseRef.current = "extract";
    setPhase("extract");
    setStatus("running");

    try {
      // Phase 1: always start a FRESH custom-bid extraction first.
      const res = await fetch(`${API}/api/extract/custom/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: compareAll ? "all" : Number(customCount) }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.error || "Could not start the fresh custom-bid extraction "
                 + "(one may already be running in the Custom Bid Extractor tab).");
        setStatus("error");
      }
    } catch (err) {
      setError(String(err));
      setStatus("error");
    }
  };

  // Controls act on whichever phase is currently active.
  const activeBase = () => (phaseRef.current === "extract" ? `${API}/api/extract/custom` : `${API}/api/compare`);
  const handlePause  = () => fetch(`${activeBase()}/pause`,  { method: "POST" });
  const handleResume = () => fetch(`${activeBase()}/resume`, { method: "POST" });
  const handleRetry  = () => fetch(`${activeBase()}/retry`,  { method: "POST" });
  const handleExportActive = () => window.open(`${activeBase()}/download`, "_blank");

  const handleCancel = async () => {
    clearInterval(pollRef.current);
    try {
      await fetch(`${activeBase()}/cancel`, { method: "POST" });
    } catch (err) { console.error("cancel error:", err); }
    reset();
  };

  const handleDownload = () => window.open(`${API}/api/compare/download`, "_blank");

  const reset = () => {
    setStatus("idle"); setPhase("extract"); phaseRef.current = "extract";
    compareStarted.current = false;
    setExtractJob(null); setJob(null); setResult(null); setError("");
  };

  const confidentCount = result?.rows ? result.rows.filter(isConfident).length : null;
  const weakCount = result?.rows
    ? result.rows.filter((r) => !isConfident(r) && r["Matched Category"] !== "—").length
    : null;

  // Active-phase job + derived progress.
  const aj = phase === "extract" ? extractJob : job;
  const ajStatus = aj?.status;
  const extractPct = extractJob?.total > 0 ? Math.round((extractJob.written / extractJob.total) * 100) : 0;
  const itemPct    = job?.total > 0 ? Math.round((job.processed / job.total) * 100) : 0;
  const encodePct  = job?.encode_total > 0 ? Math.round((job.encoded / job.encode_total) * 100) : 0;
  const isEncoding = job?.phase?.startsWith("Encoding");

  return (
    <div className="min-h-screen bg-gray-950 text-white flex items-start justify-center p-6">
      <div className="w-full max-w-6xl space-y-6">

        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold text-white">Custom Bid → Category Comparison</h1>
          <p className="text-gray-400 text-sm mt-1">
            Runs a <span className="text-gray-200">fresh Custom Bid extraction</span> first, then matches
            those newly extracted bids against your fixed category reference CSV — so every run uses the
            latest data. Local, explainable, three-layer matching (fuzzy → TF-IDF → embeddings).
          </p>
        </div>

        {/* Controls */}
        <div className="bg-gray-900 rounded-2xl shadow-2xl p-6 space-y-5">
          <div className="space-y-2">
            <label className="text-sm text-gray-300"># of Custom Bids to extract &amp; compare</label>
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
            <p className="text-xs text-gray-500">
              A fresh extraction runs first (this many active custom bids, live from GeM ~1.5s each),
              then they're matched against the full category reference list.
            </p>
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
              Extract &amp; compare <span className="font-semibold text-white">ALL</span> active custom bids
              (ignores the number above — can take a while)
            </span>
          </label>

          <button
            onClick={handleRun}
            disabled={status === "running"}
            className="w-full bg-blue-600 hover:bg-blue-500 disabled:opacity-50
                       text-white font-semibold py-3 rounded-lg transition-colors"
          >
            {status === "running"
              ? (phase === "extract" ? "Extracting fresh custom bids…" : "Matching…")
              : "Extract fresh & Run Comparison"}
          </button>

          {/* Layer legend */}
          <div className="flex flex-wrap gap-3 text-xs text-gray-400 pt-1">
            <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-emerald-500" /> Strong (fuzzy &gt; 90)</span>
            <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-blue-500" /> Likely (TF-IDF &gt; 0.65)</span>
            <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-purple-500" /> Possible (embed &gt; 0.75)</span>
            <span className="flex items-center gap-1.5"><span className="w-2.5 h-2.5 rounded-full bg-amber-500" /> Weak — review</span>
          </div>
        </div>

        {/* Running — two-phase progress */}
        {status === "running" && (
          <div className="bg-gray-900 rounded-2xl p-6 space-y-4">
            {/* Step indicator */}
            <div className="flex items-center gap-2 text-xs">
              <span className={`px-2 py-1 rounded ${phase === "extract" ? "bg-blue-900 text-blue-200" : "bg-gray-800 text-green-400"}`}>
                {phase === "extract" ? "①" : "✓"} Fresh Custom Bid extraction
              </span>
              <span className="text-gray-600">→</span>
              <span className={`px-2 py-1 rounded ${phase === "compare" ? "bg-blue-900 text-blue-200" : "bg-gray-800 text-gray-500"}`}>
                ② Match against categories
              </span>
            </div>

            {/* Status line + banners */}
            <div className="flex items-center gap-3">
              {ajStatus === "paused"   ? <div className="text-amber-400 text-lg leading-none">⏸</div>
               : ajStatus === "hold"    ? <div className="text-red-400 text-lg leading-none">⏸</div>
               : ajStatus === "retrying"? <div className="w-4 h-4 border-2 border-yellow-400 border-t-transparent rounded-full animate-spin" />
               : <div className="w-4 h-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />}
              <p className="text-sm font-semibold text-white">
                {ajStatus === "paused"   ? "Paused — progress saved (export or resume)"
                 : ajStatus === "hold"    ? `On hold — couldn't reach GeM after ${aj?.max_attempts} tries. Progress saved.`
                 : ajStatus === "retrying"? `Network issue — auto-retrying (attempt ${aj?.attempt}/${aj?.max_attempts})…`
                 : phase === "extract"    ? "Extracting fresh custom bids from GeM…"
                 : (job?.phase || "Matching…")}
              </p>
            </div>

            {/* Phase 1 progress: extraction */}
            {phase === "extract" && extractJob && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                  <div className="bg-blue-500 h-3 rounded-full transition-all duration-300" style={{ width: `${extractPct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>
                    {extractJob.written} extracted{extractJob.total ? ` of ${extractJob.total}` : ""}
                    {extractJob.failed > 0 && ` · ${extractJob.failed} skipped`}
                  </span>
                  <span className="text-blue-400">{extractJob.total ? `${extractPct}%` : `${extractJob.collected} scanned`}</span>
                </div>
              </div>
            )}

            {/* Phase 2 progress: matching */}
            {phase === "compare" && job?.total > 0 && !isEncoding && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-3 overflow-hidden">
                  <div className="bg-blue-500 h-3 rounded-full transition-all duration-300" style={{ width: `${itemPct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>{job.processed?.toLocaleString()} / {job.total?.toLocaleString()} custom bids</span>
                  <span className="text-blue-400">{itemPct}%</span>
                </div>
              </div>
            )}
            {phase === "compare" && isEncoding && job?.encode_total > 0 && (
              <div>
                <div className="w-full bg-gray-700 rounded-full h-2 overflow-hidden">
                  <div className="bg-purple-500 h-2 rounded-full transition-all duration-300" style={{ width: `${encodePct}%` }} />
                </div>
                <div className="flex justify-between text-xs mt-2 text-gray-400">
                  <span>{job.phase} · {job.encoded?.toLocaleString()} / {job.encode_total?.toLocaleString()}</span>
                  <span className="text-purple-400">{encodePct}%</span>
                </div>
              </div>
            )}

            {/* Controls (act on the active phase) */}
            <div className="flex justify-center gap-3">
              {ajStatus === "paused" && (
                <>
                  <button onClick={handleResume} className="bg-green-600 hover:bg-green-500 text-white font-semibold px-5 py-2 rounded-lg text-sm">Resume</button>
                  <button onClick={handleExportActive} className="bg-gray-700 hover:bg-gray-600 text-white px-5 py-2 rounded-lg text-sm">Export so far</button>
                </>
              )}
              {ajStatus === "hold" && (
                <>
                  <button onClick={handleRetry} className="bg-green-600 hover:bg-green-500 text-white font-semibold px-5 py-2 rounded-lg text-sm">Retry now</button>
                  <button onClick={handleExportActive} className="bg-gray-700 hover:bg-gray-600 text-white px-5 py-2 rounded-lg text-sm">Extract so far</button>
                </>
              )}
              {(!ajStatus || ajStatus === "running" || ajStatus === "retrying") && (
                <button onClick={handlePause} className="bg-amber-600 hover:bg-amber-500 text-white font-semibold px-5 py-2 rounded-lg text-sm">Pause</button>
              )}
              <button onClick={handleCancel} className="bg-red-900 hover:bg-red-800 text-red-200 px-5 py-2 rounded-lg text-sm">Cancel</button>
            </div>
          </div>
        )}

        {/* Error */}
        {status === "error" && (
          <div className="bg-gray-900 rounded-2xl p-6 text-center space-y-3">
            <div className="text-red-400 text-3xl">✗</div>
            <p className="text-red-400 font-semibold">Run failed</p>
            <p className="text-gray-500 text-xs break-all">{error}</p>
            <button onClick={reset} className="bg-gray-700 hover:bg-gray-600 text-white px-4 py-2 rounded-lg text-sm">Try again</button>
          </div>
        )}

        {/* Done */}
        {status === "done" && result && (
          <div className="space-y-4">
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
                <button onClick={handleDownload} className="bg-green-600 hover:bg-green-500 text-white font-semibold px-5 py-2.5 rounded-lg">Download CSV</button>
                <button onClick={reset} className="bg-gray-700 hover:bg-gray-600 text-white px-4 py-2.5 rounded-lg text-sm">New comparison</button>
              </div>
            </div>

            {!result.inline && (
              <div className="bg-gray-900 rounded-2xl p-8 text-center space-y-3">
                <div className="text-4xl">📄</div>
                <p className="text-white font-semibold">{result.total.toLocaleString()} rows — too large to display here</p>
                <p className="text-gray-400 text-sm max-w-md mx-auto">
                  Results above 300 rows aren't rendered here. Use <span className="text-green-400 font-medium">Download CSV</span> for the
                  full file — every Custom Bid Extractor column plus Matched Category, Match Label and Score.
                </p>
              </div>
            )}

            {result.inline && result.rows && (
              <div className="bg-gray-900 rounded-2xl overflow-hidden">
                <p className="px-4 pt-3 text-xs text-gray-500">
                  Showing key columns — the downloaded CSV includes all extractor fields
                  (Searched Strings, Searched Result, Relevant Categories) too.
                </p>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-800 text-gray-300 text-left">
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Bid No</th>
                        <th className="px-4 py-3 font-semibold">Item Category</th>
                        <th className="px-4 py-3 font-semibold">Matched Category</th>
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Match Label</th>
                        <th className="px-4 py-3 font-semibold whitespace-nowrap">Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={i} className="border-t border-gray-800 hover:bg-gray-800/50 align-top">
                          <td className="px-4 py-3 font-mono text-xs text-blue-300 whitespace-nowrap">{row["Bid No"]}</td>
                          <td className="px-4 py-3 text-gray-200 max-w-xs">{row["Item Category"]}</td>
                          <td className="px-4 py-3 text-gray-200 max-w-xs">{row["Matched Category"]}</td>
                          <td className="px-4 py-3 whitespace-nowrap">
                            <span className={`inline-block px-2.5 py-1 rounded-md border text-xs font-semibold ${verdictStyle(row)}`}>
                              {row["Match Label"]}
                            </span>
                          </td>
                          <td className="px-4 py-3 whitespace-nowrap text-gray-300 text-xs">{row["Match Score"]}</td>
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
