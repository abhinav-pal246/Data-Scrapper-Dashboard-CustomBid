"""
match.py
========
Checks whether each Product Custom Bid could have been a Category Bid.

Reads:
    gemarpts_output.csv   — your custom bid data
    category_bids.csv     — standard category names + Category Bid Nos

Writes:
    match_output.csv      — 6 columns:
                            Bid No | Custom Item | Matched Category |
                            Category Bid No | Why It Could Belong | Score

Run:
    python3 match.py
"""

import csv, os, pickle, re, time, hashlib
import numpy as np
from rapidfuzz import process as fuzz_process, fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer

# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV        = "gemarpts_output.csv"
CATEGORY_CSV     = "category_reference.csv"  # ← static category list (Name,URL,Created At)
OUTPUT_CSV       = "match_output.csv"
EMBEDDINGS_CACHE = "category_embeddings.pkl"

FUZZY_THRESHOLD     = 90
TFIDF_THRESHOLD     = 0.65
EMBEDDING_THRESHOLD = 0.75
MODEL_NAME          = "all-MiniLM-L6-v2"

STOPWORDS = {
    "and", "or", "the", "for", "of", "in", "a", "an", "with",
    "to", "from", "by", "on", "at", "as", "is", "its", "are",
    "be", "this", "that", "it", "not", "but", "if", "than",
    "has", "have", "was", "were", "will", "can", "used", "use",
    "item", "product", "goods", "supply", "procurement", "suitable"
}


# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text):
    return " ".join(text.lower().strip().split()) if text else ""


def extract_keywords(text):
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower()).split()
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def generate_reason(custom_item, matched_category, verdict):
    if not matched_category or "No match" in verdict:
        return "No standard category found on GeM — potential catalogue gap"

    common = extract_keywords(custom_item) & extract_keywords(matched_category)
    keyword_phrase = ", ".join(sorted(common)[:5]) if common else None

    if verdict == "Strong match":
        if keyword_phrase:
            return (
                f"Names are nearly identical — both describe the same item "
                f"({keyword_phrase}). Could have been placed under this standard category."
            )
        return "Names are nearly identical. Could have been placed under this standard category directly."

    elif verdict == "Likely match":
        if keyword_phrase:
            return (
                f"Significant keyword overlap on procurement terms: {keyword_phrase}. "
                f"The item likely falls within the scope of this standard category."
            )
        return "High term overlap in procurement language. The item likely falls within this standard category."

    elif verdict == "Possible match":
        return (
            "Semantically related — both items belong to the same product domain. "
            "The custom bid may fit this category despite different naming."
        )

    elif verdict == "Weak — review":
        if keyword_phrase:
            return (
                f"Closest catalogue category by meaning ({keyword_phrase}), but below the "
                f"confidence threshold — verify manually before treating it as a category match."
            )
        return (
            "Closest catalogue category by meaning, but below the confidence threshold — "
            "likely a genuine catalogue gap; verify manually before treating it as a match."
        )

    return "Inconclusive match — manual review recommended."


def format_score(score, verdict):
    if not score or score == 0:
        return "—"
    try:
        s = float(score)
        return f"{s:.1f} / 100" if verdict == "Strong match" else f"{round(s * 100)}%"
    except (ValueError, TypeError):
        return str(score)


# ── Load data ─────────────────────────────────────────────────────────────────
CATEGORY_BID_RE = re.compile(r"GEM/\d{4}/[A-Z]/\d+", re.IGNORECASE)


def load_categories(limit=None):
    """
    Loads the static category reference CSV (columns: Name, URL, Created At).
    The category list is now a fixed file provided by the user — no live
    scraping. Deduplicates by name, preserving order.

    Args:
        limit — if set, keep only the first N categories.

    Returns:
        categories_original  — list of category names (for matching)
        category_url_map     — dict {category_name: search URL} (for reference)
    """
    categories_original = []
    category_url_map    = {}
    seen                = set()

    with open(CATEGORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row.get("Name") or row.get("Category Name") or "").strip()
            url  = (row.get("URL") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            categories_original.append(name)
            category_url_map[name] = url
            if limit is not None and len(categories_original) >= limit:
                break

    print(f"Loaded {len(categories_original)} categories from {CATEGORY_CSV}")
    return categories_original, category_url_map


def load_custom_bids(limit=None):
    rows = []
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if any(row.values()):
                rows.append(row)
                if limit is not None and len(rows) >= limit:
                    break
    print(f"Loaded {len(rows)} custom bids from {INPUT_CSV}")
    return rows


# ── Layer 1: Fuzzy ────────────────────────────────────────────────────────────
def layer1_fuzzy(item, categories_clean, categories_original):
    result = fuzz_process.extractOne(
        item, categories_clean,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=FUZZY_THRESHOLD,
    )
    if result:
        _, score, idx = result
        return categories_original[idx], round(score, 1)
    return None, 0


# ── Layer 2: TF-IDF ──────────────────────────────────────────────────────────
def build_tfidf(categories_clean):
    vectorizer = TfidfVectorizer(ngram_range=(1, 3), analyzer="word")
    matrix = vectorizer.fit_transform(categories_clean)
    print(f"TF-IDF matrix: {matrix.shape[0]} categories, {matrix.shape[1]} features")
    return vectorizer, matrix


def layer2_tfidf(item, vectorizer, matrix, categories_original):
    scores     = cosine_similarity(vectorizer.transform([item]), matrix).flatten()
    best_idx   = int(np.argmax(scores))
    best_score = float(scores[best_idx])
    if best_score >= TFIDF_THRESHOLD:
        return categories_original[best_idx], round(best_score, 3)
    return None, 0


# ── Layer 3: Embeddings ───────────────────────────────────────────────────────
def build_or_load_embeddings(model, categories_original):
    if os.path.exists(EMBEDDINGS_CACHE):
        print("Loading embeddings from cache...")
        with open(EMBEDDINGS_CACHE, "rb") as f:
            return pickle.load(f)
    print(f"Encoding {len(categories_original)} categories (first run — saved after)...")
    embeddings = model.encode(
        categories_original, batch_size=64,
        show_progress_bar=True, convert_to_numpy=True,
    )
    with open(EMBEDDINGS_CACHE, "wb") as f:
        pickle.dump(embeddings, f)
    return embeddings


def layer3_embeddings(item, model, cat_embeddings, categories_original):
    scores     = cosine_similarity(
        model.encode([item], convert_to_numpy=True), cat_embeddings
    ).flatten()
    best_idx   = int(np.argmax(scores))
    best_score = float(scores[best_idx])
    if best_score >= EMBEDDING_THRESHOLD:
        return categories_original[best_idx], round(best_score, 3)
    return None, 0


# ── Shared model (loaded once, reused across API calls) ─────────────────────────
_MODEL = None
EMB_CACHE_DIR = "emb_cache"      # per-category-set embedding cache on disk
_EMB_MEM      = {}               # in-memory embedding cache {hash: ndarray}
ENCODE_BATCH  = 256              # texts encoded per model.encode() call
COSINE_CHUNK  = 512              # items per cosine-similarity chunk (memory cap)


def get_model():
    """Lazily load the sentence-transformer once and reuse it."""
    global _MODEL
    if _MODEL is None:
        print(f"Loading sentence transformer ({MODEL_NAME})...")
        _MODEL = SentenceTransformer(MODEL_NAME)
    return _MODEL


def _noop(**_kwargs):
    """Default progress sink."""
    pass


def _noop_cp():
    pass


def encode_in_batches(model, texts, progress=_noop, phase="Encoding", checkpoint=_noop_cp):
    """Encode texts in batches, reporting progress (and honouring pause/cancel)."""
    n = len(texts)
    progress(phase=phase, encoded=0, encode_total=n)
    if n == 0:
        return np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)

    chunks = []
    for i in range(0, n, ENCODE_BATCH):
        checkpoint()
        vecs = model.encode(
            texts[i:i + ENCODE_BATCH],
            batch_size=ENCODE_BATCH, convert_to_numpy=True,
        )
        chunks.append(vecs)
        progress(encoded=min(i + ENCODE_BATCH, n))
    return np.vstack(chunks)


def get_category_embeddings(categories, model, progress=_noop, checkpoint=_noop_cp):
    """
    Return embeddings for `categories`, cached by a hash of the exact set so
    any slice (or the full 14k catalogue) is encoded only once, then reused
    across requests and process restarts.
    """
    key  = hashlib.md5("\n".join(categories).encode("utf-8")).hexdigest()
    if key in _EMB_MEM:
        return _EMB_MEM[key]

    path = os.path.join(EMB_CACHE_DIR, f"{key}.npy")
    if os.path.exists(path):
        emb = np.load(path)
        _EMB_MEM[key] = emb
        return emb

    emb = encode_in_batches(model, categories, progress,
                            phase="Encoding categories", checkpoint=checkpoint)
    os.makedirs(EMB_CACHE_DIR, exist_ok=True)
    np.save(path, emb)
    _EMB_MEM[key] = emb
    return emb


def _best_match_chunked(item_vectors, cat_matrix, threshold, always=False):
    """
    For each item vector, return the best-matching (category_index, score).
    When `always` is False, entries below `threshold` come back as (None, 0.0);
    when True, the nearest candidate is always returned regardless of score
    (used to surface the closest category even for weak matches).
    Processes items in chunks so the similarity matrix never blows up memory
    at 14k × 14k. Works for both sparse (TF-IDF) and dense (embedding) inputs.
    """
    n = item_vectors.shape[0]
    out = []
    for i in range(0, n, COSINE_CHUNK):
        chunk = item_vectors[i:i + COSINE_CHUNK]
        sims  = cosine_similarity(chunk, cat_matrix)       # (chunk × n_cat)
        best_idx   = sims.argmax(axis=1)
        best_score = sims[np.arange(sims.shape[0]), best_idx]
        for idx, sc in zip(best_idx, best_score):
            if always or sc >= threshold:
                out.append((int(idx), float(sc)))
            else:
                out.append((None, 0.0))
    return out


def run_matching_job(custom_limit=None, category_limit=None, compare_all=False,
                     output_csv="compare_output.csv", progress=_noop,
                     custom_rows=None, checkpoint=_noop_cp):
    """
    Batched, cached, progress-aware matching pipeline — built to scale to
    ~14k custom bids × ~14k categories.

    Runs the same three layers as the CLI, but layer-at-a-time across ALL
    remaining items (vectorized TF-IDF / embedding cosine, batched encoding)
    instead of one item at a time.

    `progress(**fields)` is called throughout to update a shared status dict.

    `custom_rows`, when given, is the list of custom-bid dicts to match
    (each with "Bid No" + "Item Category") — used when the caller has already
    collected them on demand. When None, they are read from INPUT_CSV.

    Returns the list of result-row dicts (also written to `output_csv`).
    """
    if compare_all:
        category_limit = None
        if custom_rows is None:
            custom_limit = None

    progress(phase="Loading data")
    categories_original, category_url_map = load_categories(limit=category_limit)
    categories_clean = [clean(c) for c in categories_original]
    custom_bids      = custom_rows if custom_rows is not None \
        else load_custom_bids(limit=custom_limit)

    # Carry over EVERY column from the Custom Bid Extractor output. Keep only
    # rows that actually carry an item, preserving order.
    DEFAULT_FIELDS = ["Bid No", "Item Category", "Searched Strings",
                      "Searched Result", "Relevant Categories"]
    custom_fields = []
    items = []   # list of (full_row, item_raw, item_clean)
    for row in custom_bids:
        item_raw = (row.get("Item Category") or "").strip()
        if not item_raw:
            continue
        if not custom_fields:
            custom_fields = [k for k in row.keys() if k] or DEFAULT_FIELDS
        items.append((row, item_raw, clean(item_raw)))
    if not custom_fields:
        custom_fields = DEFAULT_FIELDS

    total = len(items)
    progress(phase="Matching", total=total, processed=0, matched=0)

    # match_info[i] = (matched_category_or_None, score, verdict, layer_label)
    match_info = [None] * total
    pending    = list(range(total))   # indices still needing a match

    # ── Layer 1 — fuzzy (per item; rapidfuzz scans categories in C) ───────────
    progress(phase="Layer 1 · fuzzy string matching")
    still = []
    for count, i in enumerate(pending, 1):
        m, s = layer1_fuzzy(items[i][2], categories_clean, categories_original)
        if m:
            match_info[i] = (m, s, "Strong match", "Layer 1 · fuzzy string (token-sort)")
        else:
            still.append(i)
        if count % 200 == 0 or count == len(pending):
            checkpoint()
            progress(processed=count)
    pending = still

    # ── Layer 2 — TF-IDF cosine (vectorized, chunked) ─────────────────────────
    if pending:
        checkpoint()
        progress(phase="Layer 2 · TF-IDF cosine similarity")
        vectorizer, tfidf_matrix = build_tfidf(categories_clean)
        item_vecs = vectorizer.transform([items[i][2] for i in pending])
        results2  = _best_match_chunked(item_vecs, tfidf_matrix, TFIDF_THRESHOLD)
        still = []
        for i, (idx, sc) in zip(pending, results2):
            if idx is not None:
                match_info[i] = (categories_original[idx], round(sc, 3),
                                 "Likely match", "Layer 2 · TF-IDF cosine (n-gram 1-3)")
            else:
                still.append(i)
        pending = still

    # ── Layer 3 — sentence embeddings (batched encode, chunked cosine) ────────
    if pending:
        checkpoint()
        progress(phase="Layer 3 · sentence embeddings")
        model          = get_model()
        cat_embeddings = get_category_embeddings(categories_original, model, progress, checkpoint)

        item_embeddings = encode_in_batches(
            model, [items[i][1] for i in pending], progress,
            phase="Encoding custom items", checkpoint=checkpoint,
        )
        # always=True → every item gets its nearest category, even below threshold,
        # so the Matched Category / Category Bid No columns are never empty.
        results3 = _best_match_chunked(item_embeddings, cat_embeddings,
                                       EMBEDDING_THRESHOLD, always=True)
        for i, (idx, sc) in zip(pending, results3):
            cat = categories_original[idx]
            if sc >= EMBEDDING_THRESHOLD:
                match_info[i] = (cat, round(sc, 3), "Possible match",
                                 "Layer 3 · sentence embeddings (semantic)")
            else:
                # nearest catalogue category, but below the confidence bar
                match_info[i] = (cat, round(sc, 3), "Weak — review",
                                 "Nearest · below confidence threshold")

    # ── Assemble results in original order ────────────────────────────────────
    # Output = every Custom Bid Extractor column carried over as-is, plus the
    # matched category (from the static CSV), the strength label, and the score.
    progress(phase="Building results")
    results   = []
    matched_n = 0
    CONFIDENT = {"Strong match", "Likely match", "Possible match"}
    for i, (row, item_raw, _clean) in enumerate(items):
        match, score, verdict, _layer = match_info[i]
        if verdict in CONFIDENT:
            matched_n += 1
        out = {k: (row.get(k, "") or "") for k in custom_fields}
        out["Matched Category"] = match or "—"
        out["Match Label"]      = verdict or "No match"
        out["Match Score"]      = format_score(score, verdict)
        results.append(out)

    # Always write the full CSV so the frontend can offer a download.
    progress(phase="Writing CSV", processed=total, matched=matched_n)
    fieldnames = custom_fields + ["Matched Category", "Match Label", "Match Score"]
    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(f"Comparison: {total} custom bids vs {len(categories_original)} "
          f"categories → {output_csv}  ({matched_n} confident matches)")
    return results


def run_matching_api(custom_limit=None, category_limit=None,
                     compare_all=False, output_csv="compare_output.csv"):
    """Synchronous wrapper (no progress) — kept for direct/scripted use."""
    return run_matching_job(custom_limit, category_limit, compare_all, output_csv)


# ── Main ──────────────────────────────────────────────────────────────────────
def run_matching():
    print("\n" + "=" * 55)
    print("GeM Custom Bid → Category Matching")
    print("=" * 55)

    categories_original, category_bid_map = load_categories()
    categories_clean = [clean(c) for c in categories_original]
    custom_bids      = load_custom_bids()

    print("\nBuilding TF-IDF matrix...")
    vectorizer, tfidf_matrix = build_tfidf(categories_clean)

    print(f"\nLoading sentence transformer ({MODEL_NAME})...")
    model          = SentenceTransformer(MODEL_NAME)
    cat_embeddings = build_or_load_embeddings(model, categories_original)

    print(f"\nMatching {len(custom_bids)} custom bids...\n")

    results = []
    counts  = {"Strong match": 0, "Likely match": 0,
               "Possible match": 0, "No match": 0}

    for i, row in enumerate(custom_bids, 1):
        bid_no   = (row.get("Bid No") or "").strip()
        item_raw = (row.get("Item Category") or "").strip()

        if not item_raw:
            continue

        item = clean(item_raw)

        # Layer 1 — fuzzy
        match, score = layer1_fuzzy(item, categories_clean, categories_original)
        verdict = "Strong match" if match else None

        # Layer 2 — TF-IDF
        if not match:
            match, score = layer2_tfidf(item, vectorizer, tfidf_matrix, categories_original)
            verdict = "Likely match" if match else None

        # Layer 3 — embeddings
        if not match:
            match, score = layer3_embeddings(item, model, cat_embeddings, categories_original)
            verdict = "Possible match" if match else "No match — genuine catalogue gap"

        # Look up Category Bid No from the map
        cat_bid_no = category_bid_map.get(match, "—") if match else "—"

        if verdict in counts:
            counts[verdict] += 1
        else:
            counts["No match"] += 1

        results.append({
            "Bid No"              : bid_no or "—",
            "Custom Item"         : item_raw,
            "Matched Category"    : match or "—",
            "Category Bid No"     : cat_bid_no,
            "Why It Could Belong" : generate_reason(item_raw, match or "", verdict),
            "Score"               : format_score(score, verdict),
        })

        if i % 50 == 0 or i == len(custom_bids):
            print(f"  [{i}/{len(custom_bids)}]  " +
                  "  ".join(f"{k}: {v}" for k, v in counts.items()))

    # Write CSV
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Bid No", "Custom Item", "Matched Category",
                           "Category Bid No", "Why It Could Belong", "Score"]
        )
        writer.writeheader()
        writer.writerows(results)

    total = len(results)
    print(f"\n{'─'*55}")
    print(f"  Total processed      : {total}")
    for label, count in counts.items():
        print(f"  {label:<22} : {count}  ({round(count/total*100) if total else 0}%)")
    print(f"  Output saved to      : {OUTPUT_CSV}")
    print(f"{'─'*55}\n")


if __name__ == "__main__":
    run_matching()