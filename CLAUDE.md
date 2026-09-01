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

## The four dashboard tabs (all functional — no decorative/fake features)

1. **Custom Bid Extractor** — active Product Custom Bids → `gemarpts_output.csv`.
2. **Category Bid Extractor** — active standard Category Bids (skips GeMARPTS/custom PDFs)
   → `category_bids.csv`.
3. **Comparison** — runs a **fresh custom-bid extraction first**, then matches those bids
   against the static category reference (`category_reference.csv`) → `compare_output.csv`.
4. **Bid Lookup** — enter one bid number → classify live (Custom vs Category) → if Custom,
   match it. On-demand, synchronous.

## Key files

| File | Role |
|------|------|
| `app.py` | Flask API. `JobControl` pausable-job framework; `_run_extractor` (both extractors); `run_compare`; `/api/lookup`. |
| `gem_gemarpts_scraper.py` | Scraper core: listing paging, PDF fetch/parse, `resolve_target` (RA→parent), `search_bid_docid`, `classify_and_extract`. |
| `collect_categories.py` | Category-bid scraping helpers (product listing, `extract_from_pdf`). |
| `match.py` | Matching engine. `run_matching_job` (batched), `match_single` (one item), `recommend` (closest categories for the Recommendation tab). `expand_query`/`load_synonyms` add synonym+Hinglish query expansion from `synonyms.csv`. `CATEGORY_CSV = category_reference.csv` (reads the `Name` column). |
| `lookup_bid_nos.py` | Standalone bid-number lookup CLI helper. |
| `my-react-app/src/App.jsx` | Shell: GeM masthead (real logo `assets/gem-star.png`), nav tabs, bottom bar. |
| `my-react-app/src/components/` | `ExtractorDashboard.jsx` (custom+category), `ComparisonDashboard.jsx`, `BidLookupDashboard.jsx`. |

Data files: `gemarpts_output.csv` (custom bids), `category_bids.csv` (scraped categories),
`category_reference.csv` (static ~9.7k category names, user-supplied),
`synonyms.csv` (curated concept → 50-60 related terms: synonyms, spelling variants,
Hinglish; drives Recommendation query expansion), `all_ids.txt` /
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
