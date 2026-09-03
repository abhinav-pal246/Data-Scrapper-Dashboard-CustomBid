import { useState, useEffect, useRef } from "react";

const API = ""; // same-origin; Vite proxies /api -> Flask (see vite.config.js)

const CONFIDENT = ["Strong match", "Likely match", "Possible match"];
const isConfident = (row) => CONFIDENT.includes(row["Match Label"]);

// ── Department insights ───────────────────────────────────────────────────────
// A Custom Bid is treated as "avoidable" when it matches an existing Category
// Bid at or above this score, i.e. a suitable Category Bid was already available.
const MATCH_THRESHOLD = 70;

function analyze(rows) {
  const total = rows.length;
  const deptCounts = {};   // department -> number of avoidable Custom Bids
  const avoidable  = [];   // one entry per Custom Bid that meets the threshold

  for (const r of rows) {
    const score = parseFloat(r["Match Score"]);          // reads "76%" and "91.0 / 100"
    const category = r["Matched Category"];
    if (isNaN(score) || score < MATCH_THRESHOLD || !category || category === "—") continue;
    const dept = (r["Department"] || "").trim() || "Unspecified department";
    deptCounts[dept] = (deptCounts[dept] || 0) + 1;
    avoidable.push({
      bidNo: r["Bid No"] || "—",
      dept,
      item: r["Item Category"] || "—",
      category,
      score: Math.round(score),
    });
  }

  const byDept = Object.entries(deptCounts)
    .map(([dept, count]) => ({ dept, count }))
    .sort((a, b) => b.count - a.count);

  // Details ordered by match score, highest first.
  avoidable.sort((a, b) => (b.score - a.score) || a.dept.localeCompare(b.dept));

  return { total, avoidableCount: avoidable.length, deptCount: byDept.length, byDept, avoidable };
}

// ── Department usage drill-down ────────────────────────────────────────────────
// Every department that raised Custom Bids, ranked by how many it raised, with
// each bid's closest existing Category Bid and the similarity to it. Powers the
// "Top Departments" view — the master list plus each department's detail.
function departmentUsage(rows) {
  const map = {};   // department -> { dept, count, withCategory, bids: [] }

  for (const r of rows) {
    const dept = (r["Department"] || "").trim() || "Unspecified department";
    if (!map[dept]) map[dept] = { dept, count: 0, withCategory: 0, bids: [] };

    const score     = parseFloat(r["Match Score"]);          // reads "76%" and "91.0 / 100"
    const category  = r["Matched Category"];
    const hasCat     = !!category && category !== "—";
    // A "similar category already exists under Category Bids" when the matcher
    // cleared its confidence bar (Strong / Likely / Possible). A Weak result is
    // the nearest category but below confidence — treated as no close category.
    const confident = isConfident(r);

    map[dept].count++;
    if (confident) map[dept].withCategory++;
    map[dept].bids.push({
      bidNo:    r["Bid No"] || "—",
      item:     r["Item Category"] || "—",
      category: hasCat ? category : null,
      score:    isNaN(score) ? null : Math.round(score),
      confident,
      label:    r["Match Label"] || "",
      state:    rowState(r),
    });
  }

  const list = Object.values(map).sort(
    (a, b) => b.count - a.count || a.dept.localeCompare(b.dept));
  // Within a department, surface the bids that had a category available first,
  // then by similarity descending.
  for (const d of list)
    d.bids.sort((a, b) => (b.confident - a.confident) || ((b.score || 0) - (a.score || 0)));
  return list;
}

// Colour an indicator on a red→green scale: RED for the HIGHEST value, transitioning
// to GREEN for the LOWEST. Used for both match counts (Top Departments) and match
// scores (Comparison List). Returns inline style so the transition is smooth across
// the full range (not fixed Tailwind buckets). Non-numeric values get a neutral grey.
function heatStyle(value, min, max) {
  if (!Number.isFinite(value))
    return { color: "#64748b", backgroundColor: "#f1f5f9", borderColor: "#e2e8f0" };
  const t   = max > min ? (value - min) / (max - min) : 0.5; // 1 = highest, 0 = lowest
  const hue = Math.round(120 * (1 - t));                     // 0 = red (highest) … 120 = green (lowest)
  return {
    color:           `hsl(${hue}, 68%, 32%)`,
    backgroundColor: `hsl(${hue}, 80%, 95%)`,
    borderColor:     `hsl(${hue}, 60%, 78%)`,
  };
}

// ── Supporting analyses ───────────────────────────────────────────────────────
const _normText = (s) => (s || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
// Significant word tokens: drop short words and standalone numbers so that bids
// differing only in numeric spec values are still recognised as similar wording.
const _wordTokens = (s) => _normText(s).split(" ").filter((w) => w.length > 2 && !/^\d+$/.test(w));

// State the custom bid was raised from (buyer/consignee delivery state),
// derived by the extractor from the bid's consignee address.
const rowState = (r) => (r["State"] || "").trim() || "Unknown";

// State-wise distribution: how many custom bids came from each state.
function stateDistribution(rows) {
  const counts = {};
  for (const r of rows) counts[rowState(r)] = (counts[rowState(r)] || 0) + 1;
  return Object.entries(counts)
    .map(([state, count]) => ({ state, count }))
    // Real states first (alphabetical), "Unknown" always last.
    .sort((a, b) =>
      a.state === "Unknown" ? 1 : b.state === "Unknown" ? -1 : b.count - a.count);
}

// Category Activity: categories that repeatedly attract Custom Bids.
function categoryActivity(rows) {
  const cats = {};
  for (const r of rows) {
    const c = r["Matched Category"];
    if (!c || c === "—") continue;
    if (!cats[c]) cats[c] = { count: 0, depts: new Set() };
    cats[c].count++;
    const d = (r["Department"] || "").trim();
    if (d) cats[c].depts.add(d);
  }
  return Object.entries(cats)
    .map(([category, v]) => ({ category, count: v.count, depts: v.depts.size }))
    .filter((x) => x.count >= 2)
    .sort((a, b) => b.count - a.count);
}

// Similarity: Custom Bids whose requirement wording is essentially the same.
function similarityGroups(rows) {
  const sig = {};
  for (const r of rows) {
    const t = [...new Set(_wordTokens(r["Item Category"]))].sort();
    if (t.length < 2) continue;
    const key = t.join(" ");
    (sig[key] = sig[key] || []).push({
      bidNo: r["Bid No"] || "—",
      item: r["Item Category"] || "—",
      dept: (r["Department"] || "").trim() || "Unspecified department",
      state: rowState(r),
    });
  }
  return Object.values(sig).filter((g) => g.length >= 2).sort((a, b) => b.length - a.length);
}

// Specification: Custom Bids with unusually specific, uncommon or irregular detail.
function specificationFlags(rows) {
  const UNIT = /\b(mm|cm|mtr|metre|meter|kg|gm|gram|ltr|litre|liter|volt|kv|kva|watt|kw|hz|khz|mhz|ghz|mbps|gbps|amp|ampere|inch|ton|tonne|psi|bar|rpm|micron|ppm|nm)\b/i;
  const CODE  = /\b(is|iec|astm|bis|din|mil|jss|iso|en)\s?[:\-]?\s?\d/i;
  const MODEL = /\b[a-z]{2,}[-/ ]?\d{2,}\b/i;
  const out = [];
  for (const r of rows) {
    const item = r["Item Category"] || "";
    const numTokens = (item.match(/\b\d[\d.,/x×-]*\b/gi) || []).length;

    // Genuine specificity signals — a bid is flagged only when it shows one.
    const signals = [];
    if (numTokens >= 4 || (UNIT.test(item) && numTokens >= 2)) signals.push("Detailed measurements or units");
    if (CODE.test(item)) signals.push("Cites a standard or specification code");
    else if (MODEL.test(item)) signals.push("Contains a model or part code");
    if (item.length > 160) signals.push("Very long specification");
    if (signals.length === 0) continue;

    // Supporting context (does not flag on its own).
    const reasons = [...signals];
    const score = parseFloat(r["Match Score"]);
    if (!isNaN(score) && score < 70) reasons.push("No close standard category");

    out.push({
      bidNo: r["Bid No"] || "—",
      item,
      dept: (r["Department"] || "").trim() || "Unspecified department",
      state: rowState(r),
      reasons,
      weight: signals.length * 10 + numTokens + (item.length > 160 ? 5 : 0),
    });
  }
  return out.sort((a, b) => b.weight - a.weight);
}

function ResultsVisualizer({ rows }) {
  const { total, avoidableCount, deptCount, byDept, avoidable } = analyze(rows);
  const maxDept = byDept.length ? byDept[0].count : 1;
  const pct = total ? Math.round((avoidableCount / total) * 100) : 0;
  // Red (highest match) → green (lowest match) for the score column.
  const _av   = avoidable.map((a) => a.score);
  const avLo  = _av.length ? Math.min(..._av) : 0;
  const avHi  = _av.length ? Math.max(..._av) : 0;

  return (
    <div className="space-y-4">
      {/* Summary */}
      <div className="gem-card gem-card-pad">
        <h3 className="text-base font-semibold text-gem-text">Custom Bids with an available Category Bid</h3>
        <p className="text-sm text-gem-muted mt-1 leading-relaxed">
          The figures below count Custom Bids that matched an existing Category Bid at {MATCH_THRESHOLD}% or above.
          Each of these could have been placed as a standard Category Bid.
        </p>
        <div className="flex flex-wrap gap-x-12 gap-y-4 mt-4">
          <div>
            <p className="text-3xl font-bold text-gem-text tabular-nums">{avoidableCount.toLocaleString()}</p>
            <p className="text-xs text-gem-muted">of {total.toLocaleString()} Custom Bids ({pct}%)</p>
          </div>
          <div>
            <p className="text-3xl font-bold text-gem-text tabular-nums">{deptCount.toLocaleString()}</p>
            <p className="text-xs text-gem-muted">{deptCount === 1 ? "department involved" : "departments involved"}</p>
          </div>
        </div>
      </div>

      {avoidableCount === 0 ? (
        <div className="gem-card gem-card-pad">
          <p className="text-sm text-gem-text">
            No Custom Bids matched an existing Category Bid at {MATCH_THRESHOLD}% or above.
            No avoidable Custom Bids were identified in this comparison.
          </p>
        </div>
      ) : (
        <>
          {/* Department-wise usage */}
          <div className="gem-card overflow-hidden">
            <div className="gem-card-pad pb-3">
              <h3 className="text-base font-semibold text-gem-text">Department-wise usage</h3>
              <p className="text-sm text-gem-muted mt-1">
                How many times each department raised a Custom Bid while a matching Category Bid was available.
              </p>
            </div>
            <div className="overflow-x-auto">
              <table className="gem-table">
                <thead>
                  <tr>
                    <th>Department</th>
                    <th className="text-right whitespace-nowrap">Avoidable Custom Bids</th>
                    <th className="w-2/5">Relative share</th>
                  </tr>
                </thead>
                <tbody>
                  {byDept.map(({ dept, count }) => (
                    <tr key={dept}>
                      <td className="text-gem-text">{dept}</td>
                      <td className="text-right font-semibold text-gem-text tabular-nums">{count.toLocaleString()}</td>
                      <td>
                        <div className="bg-slate-100 rounded h-2.5 overflow-hidden">
                          <div className="bg-gem-blue h-2.5" style={{ width: `${Math.round((count / maxDept) * 100)}%` }} />
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Matching details */}
          <div className="gem-card overflow-hidden">
            <div className="gem-card-pad pb-3">
              <h3 className="text-base font-semibold text-gem-text">Matching Category Bids</h3>
              <p className="text-sm text-gem-muted mt-1">
                Each Custom Bid above, with the existing Category Bid it could have used.
              </p>
            </div>
            <div className="overflow-auto max-h-[60vh]">
              <table className="gem-table">
                <thead className="sticky top-0 z-10">
                  <tr>
                    <th>Department</th><th>Custom Bid No</th><th>Custom Item</th>
                    <th>Matching Category</th><th className="text-right">Match</th>
                  </tr>
                </thead>
                <tbody>
                  {avoidable.map((a, i) => (
                    <tr key={i}>
                      <td className="text-gem-text text-xs">{a.dept}</td>
                      <td className="font-mono text-xs text-gem-blue whitespace-nowrap">{a.bidNo}</td>
                      <td className="text-gem-text max-w-xs">{a.item}</td>
                      <td className="text-gem-text max-w-xs">{a.category}</td>
                      <td className="text-right font-semibold tabular-nums" style={{ color: heatStyle(a.score, avLo, avHi).color }}>{a.score}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ── Analysis view (separate tab): category activity, similarity, specifications ─
function AnalysisView({ rows }) {
  const [stateFilter, setStateFilter] = useState("all");

  // State distribution over ALL rows (so the filter always lists every state),
  // and whether any state was actually derived (older extractions won't have it).
  const stateDist    = stateDistribution(rows);
  const hasStateData = stateDist.some((s) => s.state !== "Unknown");
  const maxState     = stateDist.length ? Math.max(...stateDist.map((s) => s.count)) : 1;

  // Everything below the state card reflects the selected state.
  const scoped = stateFilter === "all" ? rows : rows.filter((r) => rowState(r) === stateFilter);

  const catActivity = categoryActivity(scoped);
  const simGroups   = similarityGroups(scoped);
  const specFlags   = specificationFlags(scoped);
  const maxCat      = catActivity.length ? catActivity[0].count : 1;

  return (
    <div className="space-y-4">
      {/* State-wise distribution + filter */}
      <div className="gem-card overflow-hidden">
        <div className="gem-card-pad pb-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-gem-text">State-wise distribution</h3>
            <p className="text-sm text-gem-muted mt-1">
              The state each custom bid was raised from, derived from its consignee (delivery) address.
              Use the filter to scope the analyses below to a single state.
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm whitespace-nowrap">
            <span className="text-gem-muted">State</span>
            <select
              value={stateFilter}
              onChange={(e) => setStateFilter(e.target.value)}
              className="gem-input py-1.5 pr-8"
            >
              <option value="all">All states ({rows.length.toLocaleString()})</option>
              {stateDist.map(({ state, count }) => (
                <option key={state} value={state}>{state} ({count.toLocaleString()})</option>
              ))}
            </select>
          </label>
        </div>

        {!hasStateData ? (
          <p className="gem-card-pad pt-0 text-sm text-gem-text">
            No state information is available for these results. State is derived during extraction from
            each bid’s consignee address — run a fresh comparison to populate it.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="gem-table">
              <thead>
                <tr>
                  <th>State</th>
                  <th className="text-right whitespace-nowrap">Custom Bids</th>
                  <th className="w-2/5">Relative share</th>
                </tr>
              </thead>
              <tbody>
                {stateDist.map(({ state, count }) => (
                  <tr
                    key={state}
                    onClick={() => setStateFilter((f) => (f === state ? "all" : state))}
                    className={`cursor-pointer hover:bg-slate-50 ${stateFilter === state ? "bg-blue-50" : ""}`}
                  >
                    <td className={`text-gem-text ${state === "Unknown" ? "italic text-gem-muted" : ""}`}>{state}</td>
                    <td className="text-right font-semibold text-gem-text tabular-nums">{count.toLocaleString()}</td>
                    <td>
                      <div className="bg-slate-100 rounded h-2.5 overflow-hidden">
                        <div className="bg-gem-blue h-2.5" style={{ width: `${Math.round((count / maxState) * 100)}%` }} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {stateFilter !== "all" && (
        <div className="flex items-center gap-2 text-sm text-gem-text">
          <span className="gem-badge text-gem-blue bg-blue-50 border-blue-200">
            Filtered to {stateFilter} · {scoped.length.toLocaleString()} bids
          </span>
          <button onClick={() => setStateFilter("all")} className="text-gem-link underline text-xs">Clear filter</button>
        </div>
      )}
      {/* Category Activity Analysis */}
      <div className="gem-card overflow-hidden">
        <div className="gem-card-pad pb-3">
          <h3 className="text-base font-semibold text-gem-text">Category Activity Analysis</h3>
          <p className="text-sm text-gem-muted mt-1">Which categories show unusually high custom-bid activity.</p>
        </div>
        {catActivity.length ? (
          <div className="overflow-x-auto">
            <table className="gem-table">
              <thead>
                <tr>
                  <th>Category</th>
                  <th className="text-right whitespace-nowrap">Custom Bids</th>
                  <th className="text-right">Departments</th>
                  <th className="w-1/4">Activity</th>
                </tr>
              </thead>
              <tbody>
                {catActivity.slice(0, 15).map((c) => (
                  <tr key={c.category}>
                    <td className="text-gem-text">{c.category}</td>
                    <td className="text-right font-semibold text-gem-text tabular-nums">{c.count}</td>
                    <td className="text-right text-gem-text tabular-nums">{c.depts}</td>
                    <td>
                      <div className="bg-slate-100 rounded h-2.5 overflow-hidden">
                        <div className="bg-gem-blue h-2.5" style={{ width: `${Math.round((c.count / maxCat) * 100)}%` }} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="gem-card-pad pt-0 text-sm text-gem-text">
            No category attracted more than one Custom Bid. No unusual concentration of activity was found.
          </p>
        )}
      </div>

      {/* Similarity Analysis */}
      <div className="gem-card">
        <div className="gem-card-pad pb-3">
          <h3 className="text-base font-semibold text-gem-text">Similarity Analysis</h3>
          <p className="text-sm text-gem-muted mt-1">
            Custom bids that share highly similar or nearly identical requirements.
          </p>
        </div>
        {simGroups.length ? (
          <div className="gem-card-pad pt-0 space-y-3">
            <p className="text-sm text-gem-text">
              {simGroups.length} {simGroups.length === 1 ? "set" : "sets"} of similar Custom Bids identified.
            </p>
            {simGroups.slice(0, 10).map((g, i) => (
              <div key={i} className="border border-gem-border rounded-md p-3">
                <p className="text-xs font-semibold text-gem-text mb-2">{g.length} bids with matching requirements</p>
                <div className="space-y-1">
                  {g.map((b, j) => (
                    <div key={j} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs">
                      <span className="font-mono text-gem-blue">{b.bidNo}</span>
                      <span className="text-gem-text">{b.item}</span>
                      <span className="text-gem-muted">{b.dept}</span>
                      <span className="text-gem-muted">· {b.state}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="gem-card-pad pt-0 text-sm text-gem-text">
            No Custom Bids with matching requirement wording were found.
          </p>
        )}
      </div>

      {/* Specification Analysis */}
      <div className="gem-card overflow-hidden">
        <div className="gem-card-pad pb-3">
          <h3 className="text-base font-semibold text-gem-text">Specification Analysis</h3>
          <p className="text-sm text-gem-muted mt-1">
            Custom bids that contain unusually specific, uncommon, or irregular specifications.
          </p>
        </div>
        {specFlags.length ? (
          <div className="overflow-auto max-h-[60vh]">
            <table className="gem-table">
              <thead className="sticky top-0 z-10">
                <tr><th>Custom Bid No</th><th>State</th><th>Department</th><th>Custom Item</th><th>Observation</th></tr>
              </thead>
              <tbody>
                {specFlags.slice(0, 25).map((f, i) => (
                  <tr key={i}>
                    <td className="font-mono text-xs text-gem-blue whitespace-nowrap">{f.bidNo}</td>
                    <td className="text-gem-text text-xs whitespace-nowrap">{f.state}</td>
                    <td className="text-gem-text text-xs">{f.dept}</td>
                    <td className="text-gem-text max-w-xs">{f.item}</td>
                    <td>
                      <div className="flex flex-wrap gap-1">
                        {f.reasons.map((rs, k) => (
                          <span key={k} className="gem-badge bg-amber-50 text-amber-700 border-amber-300 font-normal">{rs}</span>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="gem-card-pad pt-0 text-sm text-gem-text">
            No Custom Bids showed unusually specific or irregular specifications.
          </p>
        )}
      </div>
    </div>
  );
}

// ── Top Departments view: top 10 orgs by Custom Bid usage, with drill-down ──────
function DepartmentDetail({ dept }) {
  const withCat = dept.withCategory;

  return (
    <div className="gem-card overflow-hidden">
      <div className="gem-card-pad pb-3">
        <h3 className="text-base font-semibold text-gem-text">{dept.dept}</h3>
        <p className="text-sm text-gem-muted mt-1 leading-relaxed">
          Every Custom Bid this department raised — the buyer’s stated requirement, whether the item was
          already available as a Category Bid, and the matching category where one exists.
        </p>
        <div className="flex flex-wrap gap-x-10 gap-y-3 mt-4">
          <div>
            <p className="text-2xl font-bold text-gem-text tabular-nums">{dept.count.toLocaleString()}</p>
            <p className="text-xs text-gem-muted">Custom Bids raised</p>
          </div>
          {withCat > 0 && (
            <div>
              <p className="text-2xl font-bold text-amber-600 tabular-nums">{withCat.toLocaleString()}</p>
              <p className="text-xs text-gem-muted">had a matching Category Bid available</p>
            </div>
          )}
        </div>
      </div>
      <div className="overflow-auto max-h-[60vh]">
        <table className="gem-table">
          <thead className="sticky top-0 z-10">
            <tr>
              <th>Custom Bid No</th>
              <th>Buyer’s requirement / custom description</th>
              <th className="whitespace-nowrap">Bid type</th>
              <th>Matching category</th>
            </tr>
          </thead>
          <tbody>
            {dept.bids.map((b, i) => (
              <tr key={i}>
                <td className="font-mono text-xs text-gem-blue whitespace-nowrap align-top">{b.bidNo}</td>
                <td className="text-gem-text align-top max-w-md">{b.item}</td>
                <td className="whitespace-nowrap align-top">
                  {b.confident ? (
                    <span className="gem-badge text-amber-700 bg-amber-50 border-amber-300">Category Bid available</span>
                  ) : (
                    <span className="gem-badge text-gem-muted bg-slate-100 border-gem-border">Custom Bid</span>
                  )}
                </td>
                <td className="text-gem-text align-top max-w-xs">
                  {b.confident && b.category ? (
                    <span>
                      {b.category}
                      {b.score != null && <span className="text-gem-muted"> · {b.score}% match</span>}
                    </span>
                  ) : (
                    <span className="text-gem-muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TopDepartmentsView({ rows }) {
  const depts    = departmentUsage(rows);
  const top      = depts.slice(0, 10);
  // Open the busiest department by default so the drill-down is never empty.
  const [selected, setSelected] = useState(top.length ? top[0].dept : null);
  const active = depts.find((d) => d.dept === selected) || null;

  // Departments that had a matching Category Bid available yet still raised a
  // Custom Bid — ranked by how many category matches they had (most first, down
  // to 1). Each keeps only its matched bids (category + buyer's description).
  const flagged = depts
    .filter((d) => d.withCategory > 0)
    .map((d) => ({ ...d, matched: d.bids.filter((b) => b.confident) }))
    .sort((a, b) =>
      b.withCategory - a.withCategory || b.count - a.count || a.dept.localeCompare(b.dept));

  // Range of category-match counts, for the red (most) → green (fewest) indicator.
  const _mc      = flagged.map((d) => d.withCategory);
  const minMatch = _mc.length ? Math.min(..._mc) : 0;
  const maxMatch = _mc.length ? Math.max(..._mc) : 0;

  if (!depts.length) {
    return (
      <div className="gem-card gem-card-pad">
        <p className="text-sm text-gem-text">No departments were found in these results.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Master list */}
      <div className="gem-card overflow-hidden">
        <div className="gem-card-pad pb-3">
          <h3 className="text-base font-semibold text-gem-text">Top departments by Custom Bid usage</h3>
          <p className="text-sm text-gem-muted mt-1 leading-relaxed">
            The ten departments and organisations that raised the most Custom Bids. Select a department to see
            each bid — the buyer’s stated requirement, whether the item was already available as a Category Bid,
            and the matching category where one exists.
          </p>
        </div>
        <div className="overflow-x-auto">
          <table className="gem-table">
            <thead>
              <tr>
                <th className="w-8">#</th>
                <th>Department / Organisation</th>
                <th className="text-right whitespace-nowrap">Custom Bids</th>
                <th className="text-right whitespace-nowrap">Category Bid available</th>
              </tr>
            </thead>
            <tbody>
              {top.map((d, i) => (
                <tr
                  key={d.dept}
                  onClick={() => setSelected((s) => (s === d.dept ? null : d.dept))}
                  className={`cursor-pointer hover:bg-slate-50 ${selected === d.dept ? "bg-blue-50" : ""}`}
                >
                  <td className="text-gem-muted tabular-nums">{i + 1}</td>
                  <td className="text-gem-text">{d.dept}</td>
                  <td className="text-right font-semibold text-gem-text tabular-nums">{d.count.toLocaleString()}</td>
                  <td className="text-right tabular-nums">
                    {d.withCategory > 0 ? (
                      <span className="font-semibold"
                            style={{ color: heatStyle(d.withCategory, minMatch, maxMatch).color }}>
                        {d.withCategory.toLocaleString()}
                        <span className="text-gem-muted font-normal"> / {d.count.toLocaleString()}</span>
                      </span>
                    ) : (
                      <span className="text-gem-muted">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Selected department drill-down */}
      {active && <DepartmentDetail dept={active} />}

      {/* Departments that bypassed an available category (ranked by match count) */}
      {flagged.length > 0 && (
        <div className="gem-card overflow-hidden">
          <div className="gem-card-pad pb-3">
            <h3 className="text-base font-semibold text-gem-text">
              Custom Bids raised despite an available category
            </h3>
            <p className="text-sm text-gem-muted mt-1 leading-relaxed">
              Departments that already had a matching GeM category yet still raised a Custom Bid, ranked by the
              number of category matches (most first). For each, the matching category and the buyer’s stated
              requirement are shown.
            </p>
          </div>
          <div className="gem-card-pad pt-0 space-y-4">
            {flagged.map((d) => (
              <div key={d.dept} className="border border-gem-border rounded-md overflow-hidden">
                <div className="flex items-center justify-between gap-3 px-3 py-2 bg-slate-50 border-b border-gem-border">
                  <span className="text-sm font-semibold text-gem-text">{d.dept}</span>
                  <span className="gem-badge whitespace-nowrap"
                        style={heatStyle(d.withCategory, minMatch, maxMatch)}>
                    {d.withCategory} category {d.withCategory === 1 ? "match" : "matches"}
                  </span>
                </div>
                <div className="overflow-x-auto">
                  <table className="gem-table">
                    <thead>
                      <tr>
                        <th>Custom Bid No</th>
                        <th>Matching category</th>
                        <th>Buyer’s custom description / reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {d.matched.map((b, i) => (
                        <tr key={i}>
                          <td className="font-mono text-xs text-gem-blue whitespace-nowrap align-top">{b.bidNo}</td>
                          <td className="text-gem-text align-top max-w-xs">
                            {b.category}
                            {b.score != null && <span className="text-gem-muted"> · {b.score}% match</span>}
                          </td>
                          <td className="text-gem-text align-top max-w-md">{b.item}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

    </div>
  );
}

export default function ComparisonDashboard() {
  const [customCount, setCustomCount] = useState("");
  const [compareAll, setCompareAll]   = useState(false);

  const [status, setStatus] = useState("idle"); // idle | running | done | error
  const [phase, setPhase]   = useState("extract"); // extract | compare
  const [extractJob, setExtractJob] = useState(null);
  const [job, setJob]       = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError]   = useState("");
  const [resultView, setResultView] = useState("list"); // list | analytics

  const pollRef        = useRef(null);
  const phaseRef       = useRef("extract");
  const compareStarted = useRef(false);

  useEffect(() => {
    if (status !== "running") return;
    pollRef.current = setInterval(async () => {
      try {
        if (phaseRef.current === "extract") {
          const data = await (await fetch(`${API}/api/extract/custom/status`)).json();
          setExtractJob(data);
          if (data.done) {
            if (data.status === "error") {
              setError(data.error || "Custom Bid extraction failed.");
              setStatus("error"); clearInterval(pollRef.current); return;
            }
            if (!compareStarted.current) {
              compareStarted.current = true;
              phaseRef.current = "compare"; setPhase("compare");
              setJob({ phase: "Starting", total: 0, processed: 0, encoded: 0, encode_total: 0 });
              const res = await fetch(`${API}/api/compare`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ compareAll: true }),
              });
              if (!res.ok) {
                const d = await res.json().catch(() => ({}));
                setError(d.error || "Could not start comparison.");
                setStatus("error"); clearInterval(pollRef.current);
              }
            }
          }
          return;
        }
        const data = await (await fetch(`${API}/api/compare/status`)).json();
        setJob(data);
        if (data.done) {
          clearInterval(pollRef.current);
          if (data.status === "error") { setError(data.error || "Comparison failed."); setStatus("error"); return; }
          const r = await fetch(`${API}/api/compare/result`);
          setResult(await r.json()); setStatus("done");
        }
      } catch (err) { console.error("poll error:", err); }
    }, 800);
    return () => clearInterval(pollRef.current);
  }, [status]);

  const handleRun = async () => {
    if (!compareAll) {
      const c = Number(customCount);
      if (!customCount || isNaN(c) || c <= 0) {
        alert("Please enter a valid number of custom bids, or select the option to compare all active custom bids."); return;
      }
    }
    setError(""); setResult(null); setJob(null);
    setExtractJob({ phase: "collecting", written: 0, total: 0, collected: 0, failed: 0 });
    compareStarted.current = false; phaseRef.current = "extract"; setPhase("extract"); setStatus("running");
    try {
      const res = await fetch(`${API}/api/extract/custom/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: compareAll ? "all" : Number(customCount) }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        setError(d.error || "The fresh custom bid extraction could not be started. "
                 + "An extraction may already be running in the Custom Bid Extractor tab.");
        setStatus("error");
      }
    } catch (err) { setError(String(err)); setStatus("error"); }
  };

  const activeBase = () => (phaseRef.current === "extract" ? `${API}/api/extract/custom` : `${API}/api/compare`);
  const handlePause  = () => fetch(`${activeBase()}/pause`,  { method: "POST" });
  const handleResume = () => fetch(`${activeBase()}/resume`, { method: "POST" });
  const handleRetry  = () => fetch(`${activeBase()}/retry`,  { method: "POST" });
  const handleExportActive = () => window.open(`${activeBase()}/download`, "_blank");
  const handleCancel = async () => {
    clearInterval(pollRef.current);
    try { await fetch(`${activeBase()}/cancel`, { method: "POST" }); } catch (e) { console.error(e); }
    reset();
  };
  const handleDownload = () => window.open(`${API}/api/compare/download`, "_blank");

  // While extraction is paused/on-hold: stop collecting more and compare the
  // custom bids gathered so far, then show all results.
  const handleCompareSoFar = async () => {
    try { await fetch(`${API}/api/extract/custom/cancel`, { method: "POST" }); } catch (e) { console.error(e); }
    compareStarted.current = true;
    phaseRef.current = "compare"; setPhase("compare");
    setExtractJob((p) => ({ ...(p || {}), status: "done", done: true }));
    setJob({ phase: "Starting", total: 0, processed: 0, encoded: 0, encode_total: 0 });
    const res = await fetch(`${API}/api/compare`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ compareAll: true }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(d.error || "Could not start comparison."); setStatus("error");
    }
  };

  const reset = () => {
    setStatus("idle"); setPhase("extract"); phaseRef.current = "extract";
    compareStarted.current = false; setExtractJob(null); setJob(null); setResult(null); setError("");
  };

  const confidentCount = result?.rows ? result.rows.filter(isConfident).length : null;
  const weakCount = result?.rows ? result.rows.filter((r) => !isConfident(r) && r["Matched Category"] !== "—").length : null;

  // Score range across the current results — drives the red (highest match) → green
  // (lowest match) colour of the Match Label and Score in the Comparison List.
  const _listScores = result?.rows ? result.rows.map((r) => parseFloat(r["Match Score"])).filter(Number.isFinite) : [];
  const loScore = _listScores.length ? Math.min(..._listScores) : 0;
  const hiScore = _listScores.length ? Math.max(..._listScores) : 0;

  const aj = phase === "extract" ? extractJob : job;
  const ajStatus = aj?.status;
  const extractPct = extractJob?.total > 0 ? Math.round((extractJob.written / extractJob.total) * 100) : 0;
  const itemPct    = job?.total > 0 ? Math.round((job.processed / job.total) * 100) : 0;
  const encodePct  = job?.encode_total > 0 ? Math.round((job.encoded / job.encode_total) * 100) : 0;
  const isEncoding = job?.phase?.startsWith("Encoding");

  return (
    <div className="gem-wrap max-w-6xl py-8 space-y-6">

      <div>
        <h1 className="gem-title">Custom Bid to Category Comparison</h1>
        <p className="gem-sub">
          Each run first performs a fresh Custom Bid extraction, then matches the newly extracted bids against the
          fixed category reference list. This ensures every comparison reflects the latest data. All matching is
          performed locally and remains fully explainable.
        </p>
      </div>

      {/* Controls */}
      <div className="gem-card gem-card-accent gem-card-pad space-y-5">
        <div>
          <label className="gem-label">Number of Custom Bids to extract and compare</label>
          <input type="number" value={customCount} onChange={(e) => setCustomCount(e.target.value)}
                 disabled={compareAll || status === "running"} placeholder="For example, 50" className="gem-input" />
          <p className="gem-help mt-1.5">
            A fresh extraction runs first for this number of active custom bids, after which each bid is
            matched against the full category reference list.
          </p>
        </div>

        <label className="flex items-center gap-3 cursor-pointer">
          <input type="checkbox" checked={compareAll} onChange={(e) => setCompareAll(e.target.checked)}
                 disabled={status === "running"} className="w-4 h-4 accent-gem-blue" />
          <span className="text-sm text-gem-text">
            Extract and compare <span className="font-semibold">all</span> active custom bids.
            This ignores the number above and may take some time.
          </span>
        </label>

        <button onClick={handleRun} disabled={status === "running"} className="gem-btn gem-btn-primary w-full py-3">
          {status === "running" ? (phase === "extract" ? "Extracting custom bids" : "Matching") : "Extract and Run Comparison"}
        </button>

        <div className="flex flex-wrap items-center gap-4 text-xs text-gem-muted pt-1 border-t border-gem-border">
          <span className="pt-3 text-gem-muted">Match strength (red = highest, green = lowest):</span>
          {[
            ["Strong (fuzzy above 90)", 4],
            ["Likely (TF-IDF above 0.65)", 3],
            ["Possible (embedding above 0.75)", 2],
            ["Weak (review)", 1],
          ].map(([label, rank]) => (
            <span key={label} className="flex items-center gap-1.5 pt-3">
              <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: heatStyle(rank, 1, 4).color }} /> {label}
            </span>
          ))}
        </div>
      </div>

      {/* Running — two-phase */}
      {status === "running" && (
        <div className="gem-card gem-card-pad space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className={`px-2.5 py-1 rounded ${phase === "extract" ? "bg-blue-100 text-gem-blue" : "bg-green-50 text-gem-green"}`}>
              Step 1. {phase === "extract" ? "Custom Bid extraction" : "Custom Bid extraction complete"}
            </span>
            <span className={`px-2.5 py-1 rounded ${phase === "compare" ? "bg-blue-100 text-gem-blue" : "bg-slate-100 text-gem-muted"}`}>
              Step 2. Category matching
            </span>
          </div>

          <div className="flex items-center gap-3">
            {ajStatus === "paused"   ? <div className="text-amber-500 text-lg leading-none">⏸</div>
             : ajStatus === "hold"    ? <div className="text-gem-red text-lg leading-none">⏸</div>
             : ajStatus === "retrying"? <div className="w-4 h-4 border-2 border-amber-500 border-t-transparent rounded-full animate-spin" />
             : <div className="w-4 h-4 border-2 border-gem-blue border-t-transparent rounded-full animate-spin" />}
            <p className="text-sm font-semibold text-gem-text">
              {ajStatus === "paused"   ? "Paused. Progress saved. You can export the data or resume."
               : ajStatus === "hold"    ? `On hold. Unable to reach GeM after ${aj?.max_attempts} attempts. Progress saved.`
               : ajStatus === "retrying"? `Network issue detected. Retrying automatically (attempt ${aj?.attempt} of ${aj?.max_attempts}).`
               : phase === "extract"    ? "Extracting custom bids from GeM."
               : (job?.phase || "Matching")}
            </p>
          </div>

          {phase === "extract" && extractJob && (
            <div>
              <div className="w-full bg-slate-200 rounded-full h-2.5 overflow-hidden">
                <div className="bg-gem-blue h-2.5 rounded-full transition-all duration-300" style={{ width: `${extractPct}%` }} />
              </div>
              <div className="flex justify-between text-xs mt-2 text-gem-muted">
                <span>{extractJob.written} extracted{extractJob.total ? ` of ${extractJob.total}` : ""}{extractJob.failed > 0 && ` · ${extractJob.failed} skipped`}</span>
                <span className="text-gem-blue">{extractJob.total ? `${extractPct}%` : `${extractJob.collected} scanned`}</span>
              </div>
            </div>
          )}
          {phase === "compare" && job?.total > 0 && !isEncoding && (
            <div>
              <div className="w-full bg-slate-200 rounded-full h-2.5 overflow-hidden">
                <div className="bg-gem-blue h-2.5 rounded-full transition-all duration-300" style={{ width: `${itemPct}%` }} />
              </div>
              <div className="flex justify-between text-xs mt-2 text-gem-muted">
                <span>{job.processed?.toLocaleString()} / {job.total?.toLocaleString()} custom bids</span>
                <span className="text-gem-blue">{itemPct}%</span>
              </div>
            </div>
          )}
          {phase === "compare" && isEncoding && job?.encode_total > 0 && (
            <div>
              <div className="w-full bg-slate-200 rounded-full h-2 overflow-hidden">
                <div className="bg-purple-500 h-2 rounded-full transition-all duration-300" style={{ width: `${encodePct}%` }} />
              </div>
              <div className="flex justify-between text-xs mt-2 text-gem-muted">
                <span>{job.phase} · {job.encoded?.toLocaleString()} / {job.encode_total?.toLocaleString()}</span>
                <span className="text-purple-600">{encodePct}%</span>
              </div>
            </div>
          )}

          <div className="flex justify-center gap-3">
            {ajStatus === "paused" && (
              <>
                <button onClick={handleResume} className="gem-btn gem-btn-success">Resume</button>
                <button onClick={handleExportActive} className="gem-btn gem-btn-ghost">Export collected data</button>
              </>
            )}
            {ajStatus === "hold" && (
              <>
                <button onClick={handleRetry} className="gem-btn gem-btn-success">Retry</button>
                <button onClick={handleExportActive} className="gem-btn gem-btn-ghost">Export collected data</button>
              </>
            )}
            {phase === "extract" && (ajStatus === "paused" || ajStatus === "hold") && extractJob?.written > 0 && (
              <button onClick={handleCompareSoFar} className="gem-btn gem-btn-primary">
                Compare {extractJob.written} collected bids
              </button>
            )}
            {(!ajStatus || ajStatus === "running" || ajStatus === "retrying") && (
              <button onClick={handlePause} className="gem-btn gem-btn-warn">Pause</button>
            )}
            <button onClick={handleCancel} className="gem-btn gem-btn-danger">Cancel</button>
          </div>
        </div>
      )}

      {/* Error */}
      {status === "error" && (
        <div className="gem-card gem-card-pad text-center space-y-3">
          <div className="mx-auto w-12 h-12 rounded-full bg-red-100 text-gem-red flex items-center justify-center text-2xl">!</div>
          <p className="text-gem-red font-semibold">The comparison could not be completed</p>
          <p className="text-gem-muted text-xs break-all">{error}</p>
          <button onClick={reset} className="gem-btn gem-btn-ghost">Try again</button>
        </div>
      )}

      {/* Done */}
      {status === "done" && result && (
        <div className="space-y-4">
          <div className="gem-card gem-card-pad flex flex-wrap items-center justify-between gap-4">
            <div className="flex flex-wrap gap-8">
              <div>
                <p className="text-2xl font-bold text-gem-text">{result.total.toLocaleString()}</p>
                <p className="text-xs text-gem-muted">Custom bids compared</p>
              </div>
              {confidentCount !== null && (
                <div>
                  <p className="text-2xl font-bold text-gem-red">{confidentCount.toLocaleString()}</p>
                  <p className="text-xs text-gem-muted">Confident matches</p>
                </div>
              )}
              {weakCount !== null && (
                <div>
                  <p className="text-2xl font-bold text-gem-green">{weakCount.toLocaleString()}</p>
                  <p className="text-xs text-gem-muted">Weak, needs review</p>
                </div>
              )}
            </div>
            <div className="flex gap-3">
              <button onClick={handleDownload} className="gem-btn gem-btn-success">Download CSV</button>
              <button onClick={reset} className="gem-btn gem-btn-ghost">New comparison</button>
            </div>
          </div>

          {/* View switcher — List / Comparison Analytics / Analysis (one at a time) */}
          {result.rows && result.rows.length > 0 && (
            <div className="inline-flex rounded-md border border-gem-border overflow-hidden text-sm">
              {[
                ["list", "Comparison List"],
                ["analytics", "Comparison Analytics"],
                ["analysis", "Analysis"],
                ["departments", "Top Departments"],
              ].map(([id, label], i) => (
                <button
                  key={id}
                  onClick={() => setResultView(id)}
                  className={`px-4 py-2 font-medium transition-colors ${i > 0 ? "border-l border-gem-border" : ""} ${
                    resultView === id ? "bg-gem-blue text-white" : "bg-white text-gem-text hover:bg-slate-50"}`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          {/* Comparison Analytics — department-focused view */}
          {result.rows && result.rows.length > 0 && resultView === "analytics" && (
            <ResultsVisualizer rows={result.rows} />
          )}

          {/* Analysis — category activity, similarity, and specification analyses */}
          {result.rows && result.rows.length > 0 && resultView === "analysis" && (
            <AnalysisView rows={result.rows} />
          )}

          {/* Top Departments — top 10 orgs by Custom Bid usage, with drill-down */}
          {result.rows && result.rows.length > 0 && resultView === "departments" && (
            <TopDepartmentsView rows={result.rows} />
          )}

          {/* List — the full result table, every row, all columns (no cap) */}
          {result.rows && result.rows.length > 0 && resultView === "list" && (
            <div className="gem-card overflow-hidden">
              <p className="px-4 pt-3 text-xs text-gem-muted">
                Showing all <span className="font-semibold text-gem-text">{result.rows.length.toLocaleString()}</span> records.
                The downloaded file also includes the Searched Strings, Searched Result and Relevant Categories columns.
              </p>
              <div className="overflow-auto max-h-[70vh]">
                <table className="gem-table">
                  <thead className="sticky top-0 z-10">
                    <tr>
                      <th>Bid No</th><th>Department</th><th>Item Category</th><th>Matched Category</th><th>Match Label</th><th>Score</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.map((row, i) => {
                      // Red for the highest match %, green for the lowest.
                      const hs = heatStyle(parseFloat(row["Match Score"]), loScore, hiScore);
                      return (
                        <tr key={i}>
                          <td className="font-mono text-xs text-gem-blue whitespace-nowrap">{row["Bid No"]}</td>
                          <td className="text-gem-text text-xs">{row["Department"] || "—"}</td>
                          <td className="text-gem-text max-w-xs">{row["Item Category"]}</td>
                          <td className="text-gem-text max-w-xs">{row["Matched Category"]}</td>
                          <td className="whitespace-nowrap">
                            <span className="gem-badge" style={hs}>{row["Match Label"]}</span>
                          </td>
                          <td className="whitespace-nowrap text-xs font-semibold" style={{ color: hs.color }}>{row["Match Score"]}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
