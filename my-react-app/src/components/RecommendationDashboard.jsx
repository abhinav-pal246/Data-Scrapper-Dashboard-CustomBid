import { useState } from "react";

const API = ""; // same-origin; Vite proxies /api -> Flask

function simStyle(pct) {
  if (pct >= 70) return "text-gem-green bg-green-50 border-green-300";
  if (pct >= 40) return "text-amber-700 bg-amber-50 border-amber-300";
  return "text-gem-red bg-red-50 border-red-300";
}

// GeM policy applied to the closest-match score.
function policyVerdict(score) {
  if (score >= 70) {
    return {
      band: "use",
      style: "border-green-300 bg-green-50",
      title: "A standard Category Bid should be used",
      body:
        "A regular GeM category closely matching this requirement is available. Under GeM policy, a Custom or " +
        "BOQ bid for an item that has an available regular category is not permitted and is liable to be cancelled " +
        "without notice, unless it is bunched with a major regular category item. Place this requirement as a " +
        "Category Bid under the recommended category below.",
    };
  }
  if (score >= 40) {
    return {
      band: "verify",
      style: "border-amber-300 bg-amber-50",
      title: "A possible category match exists — verify before proceeding",
      body:
        "A category that may cover this requirement was found. Confirm it on the GeM catalogue. If it covers the " +
        "requirement, use a Category Bid, since a Custom bid for an item that has an available regular category may " +
        "be cancelled without notice.",
    };
  }
  return {
    band: "custom",
    style: "border-gem-border bg-slate-50",
    title: "A Custom Bid may be justified",
    body:
      "No closely matching standard category was found, so this requirement may not be available as a regular " +
      "category on GeM. A Custom or BOQ bid is appropriate only if the item is genuinely unavailable as a standard " +
      "category. Confirm on the GeM catalogue before proceeding.",
  };
}

export default function RecommendationDashboard() {
  const [text, setText]       = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult]   = useState(null);
  const [error, setError]     = useState("");

  const run = async () => {
    const t = text.trim();
    if (t.length < 3) { setError("Enter the item category or requirement to get a recommendation."); return; }
    setError(""); setLoading(true); setResult(null);
    try {
      const res  = await fetch(`${API}/api/recommend`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: t }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.error || "Recommendation failed."); setLoading(false); return; }
      setResult(data);
    } catch (err) { setError(String(err)); }
    setLoading(false);
  };

  const primary  = result?.primary;
  const verdict  = primary ? policyVerdict(primary.score) : null;

  return (
    <div className="gem-wrap max-w-4xl py-8 space-y-6">
      <div>
        <h1 className="gem-title">Category Recommendation</h1>
        <p className="gem-sub">
          Enter a buyer’s item category or requirement to find the existing GeM Category Bid it most closely
          resembles, with a policy-based recommendation on whether a standard Category Bid should be used.
        </p>
      </div>

      {/* Input */}
      <div className="gem-card gem-card-pad space-y-3">
        <label className="gem-label">Item category or requirement</label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder="For example: Online UPS and Batteries set as per buyer requirement"
          className="gem-input resize-y"
        />
        <div className="flex items-center gap-3">
          <button onClick={run} disabled={loading} className="gem-btn gem-btn-primary px-6">
            {loading ? "Analysing" : "Recommend Category"}
          </button>
          {error && <span className="text-xs text-amber-700">{error}</span>}
        </div>
      </div>

      {/* Result */}
      {result && primary && (
        <div className="space-y-4">
          {/* Policy verdict */}
          <div className={`gem-card gem-card-pad border ${verdict.style}`}>
            <h3 className="text-base font-semibold text-gem-text">{verdict.title}</h3>
            <p className="text-sm text-gem-text/90 mt-1.5 leading-relaxed">{verdict.body}</p>
          </div>

          {/* Understood-as concepts (synonym / Hinglish expansion) */}
          {result.matched_concepts?.length > 0 && (
            <div className="gem-card gem-card-pad">
              <p className="text-xs text-gem-muted uppercase tracking-wide">Understood your search as</p>
              <div className="flex flex-wrap gap-2 mt-2">
                {result.matched_concepts.map((c, i) => (
                  <span key={i} className="gem-badge text-gem-blue bg-blue-50 border-blue-200">{c}</span>
                ))}
              </div>
              <p className="text-xs text-gem-muted mt-2">
                Matched using synonyms and Hinglish variations, not just the exact words typed.
              </p>
            </div>
          )}

          {/* Primary recommendation */}
          <div className="gem-card gem-card-pad">
            <p className="text-xs text-gem-muted uppercase tracking-wide">Closest existing category</p>
            <div className="flex flex-wrap items-center justify-between gap-3 mt-1">
              <p className="text-lg font-semibold text-gem-text">{primary.category}</p>
              <span className={`gem-badge ${simStyle(primary.score)}`}>{primary.score}% match</span>
            </div>
          </div>

          {/* Alternatives */}
          {result.candidates?.length > 1 && (
            <div className="gem-card overflow-hidden">
              <div className="gem-card-pad pb-3">
                <h3 className="text-base font-semibold text-gem-text">Alternative categories</h3>
                <p className="text-sm text-gem-muted mt-1">Other closely related categories, ranked by similarity.</p>
              </div>
              <div className="overflow-x-auto">
                <table className="gem-table">
                  <thead><tr><th>Category</th><th className="text-right">Similarity</th></tr></thead>
                  <tbody>
                    {result.candidates.map((c, i) => (
                      <tr key={i}>
                        <td className="text-gem-text">{c.category}</td>
                        <td className="text-right whitespace-nowrap">
                          <span className={`gem-badge ${simStyle(c.similarity)}`}>{c.similarity}%</span>
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

      {/* Policy basis — always shown */}
      <div className="gem-card gem-card-pad">
        <h3 className="text-sm font-semibold text-gem-text">Policy basis</h3>
        <p className="text-sm text-gem-muted mt-1.5 leading-relaxed">
          Under GeM policy, a Custom or BOQ bid published for an item for which a regular GeM category is available
          is prohibited and may be cancelled by GeM without notice, unless the custom or BOQ item is bunched with a
          major regular category item. Custom bids are intended for products or services that are not available as
          standard categories on GeM. These recommendations are guidance based on catalogue similarity; the buyer
          should verify the category on the current GeM catalogue before finalising the bid.
        </p>
        <p className="text-xs text-gem-muted mt-2">
          Reference: GeM Buyer FAQs and General Terms &amp; Conditions,{" "}
          <a href="https://gem.gov.in/userFaqs" target="_blank" rel="noreferrer" className="text-gem-link underline">gem.gov.in</a>.
        </p>
      </div>
    </div>
  );
}
