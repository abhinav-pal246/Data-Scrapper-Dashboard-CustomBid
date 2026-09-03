# CLAUDE.md

Guidance for working in this repo. It's an internal **GeM Analytics** dashboard that
scrapes live bid data from `bidplus.gem.gov.in`, extracts it, and matches Product
Custom Bids against a category reference list — fully local, no LLM/API in the pipeline.

## Architecture

Two processes talk over HTTP:

- **Backend** — Flask (`app.py`) on **port 5000**. Runs scraping/extraction and the
  matching engine in background threads.
- **Frontend** — React + Vite + Tailwind in `my-react-app/` on **port 5173**.
  `vite.config.js` proxies `/api/*` → `http://127.0.0.1:5000`, so the browser talks
  **same-origin** (components use `const API = ""`). Do not hardcode the backend URL.

```
Browser (5173) ──/api──▶ Vite dev proxy ──▶ Flask (5000) ──▶ bidplus.gem.gov.in
```

## Run it

```bash
# Terminal 1 — backend (needs the Python deps below)
python3 app.py

# Terminal 2 — frontend
cd my-react-app && npm install && npm run dev   # opens http://localhost:5173
```

Python deps (no requirements.txt yet): `flask`, `flask-cors`, `requests`,
`pdfplumber`, `beautifulsoup4`, `rapidfuzz`, `scikit-learn`, `numpy`,
`sentence-transformers`.

The frontend `/api` proxy is a **dev-server** feature; a static production build
(`npm run build`) would need the backend URL configured separately.

## The five dashboard tabs (all functional — no decorative/fake features)

1. **Custom Bid Extractor** — active Product Custom Bids → `gemarpts_output.csv`.
2. **Category Bid Extractor** — active standard Category Bids (skips GeMARPTS/custom PDFs)
   → `category_bids.csv`.
3. **Comparison** — runs a **fresh custom-bid extraction first**, then matches those bids
   against the static category reference (`category_reference.csv`) → `compare_output.csv`.
   Results have three views: Comparison List, Comparison Analytics (department-focused), and
   **Analysis** — category-activity / similarity / specification analyses, plus a **state-wise
   filter + distribution** (from the `State` column) that scopes all three to one state.
4. **Bid Lookup** — enter one bid number → classify live (Custom vs Category) → if Custom,
   match it. On-demand, synchronous.
5. **Category Recommendation** — enter an item/requirement (or a shorthand/Hinglish term)
   → closest existing GeM categories + a GeM-policy verdict on whether a Category Bid
   should be used instead of a Custom Bid. On-demand via `/api/recommend`; uses
   `synonyms.csv` query expansion (see below) so searches match by meaning, not exact words.

## Key files

| File | Role |
|------|------|
| `app.py` | Flask API. `JobControl` pausable-job framework; `_run_extractor` (both extractors); `run_compare`; `/api/lookup`; `/api/recommend`. |
| `gem_gemarpts_scraper.py` | Scraper core: listing paging, PDF fetch/parse, `resolve_target` (RA→parent), `search_bid_docid`, `classify_and_extract`. `extract_consignee_state`/`derive_state` derive the buyer state from the bid PDF's consignee address (state name → pincode → 2-letter code → major-city fallback). |
| `collect_categories.py` | Category-bid scraping helpers (product listing, `extract_from_pdf`). |
| `match.py` | Matching engine. `run_matching_job` (batched), `match_single` (one item), `recommend` (closest categories for the Recommendation tab). `expand_query`/`load_synonyms` add synonym+Hinglish query expansion from `synonyms.csv`. `CATEGORY_CSV = category_reference.csv` (reads the `Name` column). |
| `lookup_bid_nos.py` | Standalone bid-number lookup CLI helper. |
| `build_synonyms.py` | Generator for `synonyms.csv` (curated concept → term lists). Edit the `CONCEPTS` dict here, then `python3 build_synonyms.py` to regenerate. |
| `my-react-app/src/App.jsx` | Shell: GeM masthead (real logo `assets/gem-star.png`), nav tabs, bottom bar. |
| `my-react-app/src/components/` | `ExtractorDashboard.jsx` (custom+category), `ComparisonDashboard.jsx`, `BidLookupDashboard.jsx`, `RecommendationDashboard.jsx`. |

Data files: `gemarpts_output.csv` (custom bids; columns Bid No, Item Category, Searched
Strings, Searched Result, Relevant Categories, Department, **State** — State is derived
from the consignee address at extraction time and carries through to `compare_output.csv`),
`category_bids.csv` (scraped categories),
`category_reference.csv` (static ~9.7k category names, user-supplied),
`synonyms.csv` (~132 curated concepts, each with 50-60 related terms: synonyms,
spelling variants, Hinglish; drives Recommendation query expansion; regenerate via
`build_synonyms.py`), `all_ids.txt` /
`product_ids.txt` (collected doc-id pools), `emb_cache/*.npy` (category embedding cache),
`custom_meta.json` / `category_meta.json` (per-extractor resume metadata).

## Core behaviours & conventions

- **Fresh, active-only each run.** Every extraction Start overwrites its CSV and collects
  live active bids; expired/empty bids are skipped and never stored.
- **RA parent-doc fix.** Active custom bids are usually RA (`GEM/…/R/…`) whose own
  `showbidDocument` is empty — the real GeMARPTS PDF is on the **parent** (`GEM/…/B/…`).
  `resolve_target()` maps to the parent. Don't "fix" empty-PDF handling without this.
- **Pausable-job framework** (`JobControl`, shared by all three long-running modules):
  Start = fresh · **Pause** = stop & keep (thread stays alive) · **Resume** = continue
  cumulatively · **Cancel** = discard & reset. Endpoints: `/api/extract/<mod>/{start,
  pause,resume,cancel,status,download}`, `/api/compare/{…}`.
- **Auto-retry → hold → manual-retry** (`_retry_guard`): transient network/5xx errors
  auto-retry (×4) then go to a **hold** state (progress preserved) until manual retry;
  re-runs from the exact point. Permanent skips (expired/empty PDFs) are not retried.
- **Matching pipeline** (`match.py`): fuzzy (rapidfuzz > 90) → TF-IDF cosine (> 0.65) →
  sentence embeddings (all-MiniLM-L6-v2, > 0.75); below all thresholds it returns the
  nearest embedding candidate as "Weak — review". Category embeddings are cached in
  `emb_cache/`. First run is slow (model load + encode); later runs reuse the cache.
- **Synonym / Hinglish query expansion** (`match.expand_query` / `load_synonyms`): the
  Recommendation tab enriches the search before embedding. `synonyms.csv` holds ~132
  curated **concepts** (mobile, chashma, almirah, takiya, istri…), each with 50–60 related
  terms (synonyms, spelling variants, Hinglish). When any of a concept's terms appears as a
  whole word in the query, that concept's whole vocabulary is appended, pulling the search
  vector toward the right category domain — so "chashma" → eyewear, "bartan" → utensils.
  This is a **concept dictionary (~132 rows), not one row per each of the ~9.7k categories**:
  a true per-category 50–60-synonym file can't be hand-authored or generated locally without
  an LLM, which the pipeline deliberately avoids. Extend it by editing `build_synonyms.py`
  and regenerating; word-boundary matching means avoid ambiguous 1–2 char terms.
- **Presentation**: GeM-styled light theme via `tailwind.config.js` `gem.*` colors +
  `src/styles.css` `@layer components` (`.gem-card`, `.gem-btn-*`, `.gem-input`,
  `.gem-table`, `.gem-badge`, `.gem-tab`). Restyle by editing tokens/component classes,
  not per-element utilities.

## Gotchas

- **macOS + port 5000**: if Flask isn't running, port 5000 answers `403` from AirPlay,
  and the dashboard shows "API offline". Just start `python3 app.py`.
- **Rate limits**: GeM scraping sleeps ~1.5s/bid, so "Compare/Extract ALL" is slow by
  design — Pause/Cancel are available throughout.
- The Comparison can only match custom bids it just extracted; the category side always
  uses the full `category_reference.csv`.
- **State is derived, not authoritative.** GeM's listing has no state field, so `State`
  is inferred from each bid PDF's consignee (delivery) address. The pincode→state map is
  by postal circle (analytics-grade — border pincodes can be off), some GeM addresses are
  masked (only the city survives → major-city fallback), and unresolved rows show as
  "Unknown". It only appears after a fresh extraction — older CSVs lack the column, and the
  Analysis view degrades gracefully (shows a "run a fresh comparison" note) when it's absent.
