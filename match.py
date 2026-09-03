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
SYNONYMS_CSV     = "synonyms.csv"            # ← concept → related-terms dictionary
OUTPUT_CSV       = "match_output.csv"
EMBEDDINGS_CACHE = "category_embeddings.pkl"

FUZZY_THRESHOLD     = 90
TFIDF_THRESHOLD     = 0.65
EMBEDDING_THRESHOLD = 0.75
MODEL_NAME          = "all-MiniLM-L6-v2"

# ── Schema contract (confirmed against the live data, do not drift) ────────────
#   CUSTOM QUERY TEXT               = "Item Category"       (buyer-authored free text)
#   CATEGORY CORPUS                 = category_reference.csv → "Name"  (deduped)
#   RELATED-CATEGORY (cross-check)  = "Relevant Categories" (buyer-selected GeM categories)
#   GeMARPTS partition marker       = presence of the GeMARPTS-block columns below
#
# A CUSTOM bid exists only because the item is NOT a standard GeM category — the buyer
# authors the Title/Specification themselves. A CATEGORY bid procures an item that already
# exists as a standard GeM category. The ONLY reliable partition key is the GeMARPTS block:
# custom-bid PDFs carry it, category PDFs do not (see gem_gemarpts_scraper.classify_and_extract,
# which only emits a row when "GeMARPTS" is found). We do NOT rely on bid-number format.
#
# The buyer's "Relevant Categories" field is ALREADY a GeM category string (evidence: 136/200
# of these values are verbatim category-master names). It must NEVER be used as the match
# query: matching a GeM category against the GeM category master is a category-vs-category
# comparison that trivially scores ~1.0 and proves nothing. It is kept ONLY as a cross-check
# column in the output. The custom-side query text must always come from the buyer title.
TITLE_COL            = "Item Category"          # ← the ONLY source of custom-side query text
SPEC_COL             = "Specification"          # ← appended when present (absent in current schema)
RELATED_COL          = "Relevant Categories"    # ← cross-check column only, NEVER the query
CATEGORY_NAME_COL    = "Name"
GEMARPTS_MARKER_COLS = ("Searched Strings", "Searched Result", "Relevant Categories")
assert TITLE_COL != RELATED_COL, "config error: query column must not be the related-category column"

# If more than this fraction of query texts are verbatim category-master names, the query is
# almost certainly being drawn from a category-domain field (the exact leak we guard against),
# so we refuse to run rather than silently ship a category-vs-category comparison.
VERBATIM_LEAK_RATE   = 0.50
REDFLAG_CSV          = "redflag_matches.csv"
LAST_RUN_STATS       = {}       # populated by run_matching_job for callers/tests

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


# ── Synonym / Hinglish query expansion ────────────────────────────────────────
# A curated concept → related-terms dictionary (synonyms, spelling variations and
# Hinglish transliterations) lives in SYNONYMS_CSV. When a buyer's search contains
# any of a concept's terms (e.g. "chashma", "mobail"), we append that concept's
# whole vocabulary to the query text before embedding, so the search vector is
# pulled toward the right category domain — matching by *meaning*, not just the
# exact words the buyer typed.
_SYNONYM_CONCEPTS = None   # list of {concept, hint, terms(set), expansion(str)}


def _norm(text):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    return " ".join(re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).split())


def load_synonyms():
    """Load and cache the concept dictionary from SYNONYMS_CSV (once)."""
    global _SYNONYM_CONCEPTS
    if _SYNONYM_CONCEPTS is not None:
        return _SYNONYM_CONCEPTS

    concepts = []
    if os.path.exists(SYNONYMS_CSV):
        with open(SYNONYMS_CSV, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                concept = (row.get("concept") or "").strip()
                hint    = (row.get("category_hint") or "").strip()
                terms   = {_norm(t) for t in (row.get("terms") or "").split(";") if t.strip()}
                terms.discard("")
                if not concept or not terms:
                    continue
                # The expansion text the query is enriched with when this concept
                # is hit: the concept label + its category hint + every term.
                expansion = " ".join([_norm(concept), hint] + sorted(terms))
                concepts.append({"concept": concept, "hint": hint,
                                 "terms": terms, "expansion": expansion})
        print(f"Loaded {len(concepts)} synonym concepts from {SYNONYMS_CSV}")
    else:
        print(f"No synonym dictionary at {SYNONYMS_CSV} — query expansion disabled")
    _SYNONYM_CONCEPTS = concepts
    return concepts


def expand_query(text_raw):
    """
    Enrich a search with synonym/Hinglish context.

    Returns (expanded_text, matched_concepts):
        expanded_text     — original text + the vocabulary of every concept it hits
        matched_concepts  — list of concept labels that were triggered

    A concept is triggered when any of its terms appears as a whole word/phrase in
    the query (word-boundary match, so "ac" won't fire on "machine"). If nothing
    matches, the original text is returned unchanged.
    """
    concepts = load_synonyms()
    padded   = f" {_norm(text_raw)} "
    if not concepts or not padded.strip():
        return text_raw, []

    matched, additions = [], []
    for c in concepts:
        if any(f" {term} " in padded for term in c["terms"]):
            matched.append(c["concept"])
            additions.append(c["expansion"])

    if not additions:
        return text_raw, []
    return f"{text_raw} {' '.join(additions)}", matched


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
        reader = csv.DictReader(f)
        # 6.1 (corpus side, structural): the category master must never carry the
        # GeMARPTS-block columns that identify a custom bid — otherwise the corpus
        # would contain custom-bid rows and the partition would leak.
        leaked = [c for c in GEMARPTS_MARKER_COLS if c in (reader.fieldnames or [])]
        if leaked:
            raise ValueError(
                f"PARTITION VIOLATION — category corpus {CATEGORY_CSV} carries GeMARPTS "
                f"columns {leaked}; it must be a plain category master (e.g. {CATEGORY_NAME_COL},URL)."
            )
        for row in reader:
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


# ── Single-item lookup (for the Bid Lookup panel) ─────────────────────────────
_SINGLE_CACHE = None


def _single_artifacts():
    """Prepare + cache the category artifacts once, for fast repeated lookups."""
    global _SINGLE_CACHE
    if _SINGLE_CACHE is None:
        cats, _bmap    = load_categories()
        clean_cats     = [clean(c) for c in cats]
        vectorizer, tfm = build_tfidf(clean_cats)
        model          = get_model()
        emb            = get_category_embeddings(cats, model)
        _SINGLE_CACHE  = (cats, clean_cats, vectorizer, tfm, model, emb)
    return _SINGLE_CACHE


def match_single(item_raw):
    """
    Match ONE custom item against the category list. Returns
    {category, score (0-100), label, layer} — running fuzzy → TF-IDF →
    embedding and falling back to the nearest embedding ("Weak") below threshold.
    """
    cats, clean_cats, vectorizer, tfm, model, emb = _single_artifacts()
    item = clean(item_raw)

    m, s = layer1_fuzzy(item, clean_cats, cats)
    if m:
        return {"category": m, "score": round(float(s), 1),
                "label": "Strong match", "layer": "fuzzy"}

    m, s = layer2_tfidf(item, vectorizer, tfm, cats)
    if m:
        return {"category": m, "score": round(float(s) * 100, 1),
                "label": "Likely match", "layer": "TF-IDF"}

    m, s = layer3_embeddings(item, model, emb, cats)
    if m:
        return {"category": m, "score": round(float(s) * 100, 1),
                "label": "Possible match", "layer": "embedding"}

    # below every threshold → nearest embedding, flagged weak
    sims = cosine_similarity(model.encode([item], convert_to_numpy=True), emb).flatten()
    idx  = int(np.argmax(sims))
    return {"category": cats[idx], "score": round(float(sims[idx]) * 100, 1),
            "label": "Weak (review)", "layer": "embedding (nearest)"}


def recommend(item_raw, k=5):
    """
    Recommend the existing GeM categories a buyer's item most closely resembles,
    ranked by semantic similarity so the primary is always the closest match and
    every score is on the same 0-100 scale.

    Returns:
        {
          "primary":    {category, score (0-100)},          # closest category
          "candidates": [{category, similarity (0-100)}, …] # next closest, excl. primary
        }
    The caller applies GeM policy to `primary.score` to advise whether a standard
    Category Bid should be used instead of a Custom Bid.

    The raw query is first passed through `expand_query`, so synonym / Hinglish
    searches (e.g. "chashma", "mobail") are matched by meaning rather than by the
    exact words typed. `matched_concepts` lists the concepts that were triggered.
    """
    cats, clean_cats, vectorizer, tfm, model, emb = _single_artifacts()
    expanded, matched_concepts = expand_query(item_raw)
    sims  = cosine_similarity(model.encode([clean(expanded)], convert_to_numpy=True), emb).flatten()
    order = np.argsort(sims)[::-1][:max(k, 1) + 1]
    ranked = [{"category": cats[int(i)], "similarity": round(float(sims[int(i)]) * 100, 1)} for i in order]
    primary = {"category": ranked[0]["category"], "score": ranked[0]["similarity"]}
    return {"primary": primary, "candidates": ranked[1:1 + k],
            "matched_concepts": matched_concepts}


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


# ── Partition & anti-leak guards (the core of the fix) ────────────────────────
def _has_gemarpts(row):
    """True iff the row carries GeMARPTS evidence (→ it is a Custom bid)."""
    return any((row.get(c) or "").strip() for c in GEMARPTS_MARKER_COLS)


def query_text(row):
    """
    The custom-side match text — the SINGLE source of truth for what gets matched.

    Drawn ONLY from the buyer-authored Title (TITLE_COL) plus Specification when the
    schema has one (SPEC_COL). NEVER from RELATED_COL. Every code path obtains its
    query here so a future edit cannot silently swap in a category-domain field and
    collapse the exercise into a category-vs-category match.
    """
    title = (row.get(TITLE_COL) or "").strip()
    spec  = (row.get(SPEC_COL) or "").strip()   # not present in the current schema → ""
    return f"{title} {spec}".strip() if spec else title


def assert_query_partition(custom_rows):
    """6.1 (query side): every query row must be a CUSTOM bid (GeMARPTS present)."""
    bad = [(row.get("Bid No") or f"row#{i}")
           for i, row in enumerate(custom_rows) if not _has_gemarpts(row)]
    if bad:
        raise ValueError(
            f"PARTITION VIOLATION — {len(bad)}/{len(custom_rows)} query rows carry NO GeMARPTS "
            f"marker {GEMARPTS_MARKER_COLS}; the query set must be CUSTOM bids only. "
            f"Offenders: {bad[:10]}"
        )


def assert_corpus_partition(category_names):
    """6.1 (corpus side): the corpus is the standard category master — no GeMARPTS."""
    tainted = [c for c in category_names if "gemarpts" in c.lower()]
    if tainted:
        raise ValueError(
            f"PARTITION VIOLATION — {len(tainted)} corpus entries look like GeMARPTS artifacts "
            f"(e.g. {tainted[:3]}); the corpus must be the standard category master, not custom-bid rows."
        )


def _layer_short(verdict):
    """Human-readable layer that produced an accepted match, from its verdict."""
    return {"Strong match": "Layer 1 · fuzzy",
            "Likely match": "Layer 2 · TF-IDF",
            "Possible match": "Layer 3 · embedding"}.get(verdict, "—")


def _write_redflag_csv(redflags, path=REDFLAG_CSV):
    """Section 7: red-flag matches (custom bids that clear threshold against a category)."""
    cols = ["custom_bid_id", "buyer_title", "matched_gem_category", "score", "match_layer",
            "buyer_declared_related_category", "verbatim_flag"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(redflags)


def _print_validation_counts(stats):
    """Section 6.4: loud, reproducible counts for every run."""
    print("\n" + "─" * 60)
    print("  VALIDATION SUMMARY (CUSTOM → CATEGORY partition)")
    print("─" * 60)
    print(f"  Total custom bids loaded        : {stats['total_custom_bids_loaded']}")
    print(f"  Query set (custom, GeMARPTS+)   : {stats['query_set']}")
    print(f"  Corpus size (unique categories) : {stats['corpus_size']}")
    print(f"  Matches · Layer 1 fuzzy         : {stats['layer1_fuzzy']}")
    print(f"  Matches · Layer 2 TF-IDF        : {stats['layer2_tfidf']}")
    print(f"  Matches · Layer 3 embeddings    : {stats['layer3_embed']}")
    print(f"  Weak / below threshold (gap)    : {stats['weak_below_threshold']}")
    print(f"  RED-FLAG matches (≥ threshold)  : {stats['redflag_matches']}")
    print(f"  ── watch buckets ──")
    print(f"  Verbatim-category-title queries : {stats['verbatim_category_titles']}  "
          f"(buyer titled the item exactly as an existing category)")
    print(f"  Title == own declared related   : {stats['title_equals_own_related']}  "
          f"(interesting, not a leak)")
    print("─" * 60)


def _print_sample_matches(results, k=20):
    """Section 7: random samples so a human can eyeball L=custom free text, R=category."""
    import random
    pool = [r for r in results if r.get("Match Label") in
            ("Strong match", "Likely match", "Possible match")] or results
    sample = random.sample(pool, min(k, len(pool)))
    print(f"\n  {min(k, len(pool))} random matches to eyeball "
          f"(custom title  ➜  matched GeM category  [layer, score, verbatim]):")
    for r in sample:
        vb = " ⚠verbatim" if r.get("Verbatim Category Title") == "Yes" else ""
        print(f"   • {(r.get('Item Category') or '')[:52]:<52} ➜ "
              f"{(r.get('Matched Category') or '')[:52]:<52} "
              f"[{r.get('Match Label')}, {r.get('Match Score')}{vb}]")


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

    # ── Guardrails: enforce a clean CUSTOM → CATEGORY partition BEFORE matching ──
    # (6.1) Every query row is a custom bid (GeMARPTS present); no corpus entry is.
    assert_query_partition(custom_bids)
    assert_corpus_partition(categories_original)
    cat_norm_set = {_norm(c) for c in categories_original}   # for verbatim detection (6.3)

    # Carry over EVERY column from the Custom Bid Extractor output. Keep only
    # rows that actually carry an item, preserving order.
    DEFAULT_FIELDS = ["Bid No", "Item Category", "Searched Strings",
                      "Searched Result", "Relevant Categories"]
    custom_fields  = []
    items          = []   # list of (full_row, item_raw, item_clean, verbatim_flag)
    verbatim_ct    = 0    # buyer title is byte-identical (normalised) to a category name
    own_related_ct = 0    # buyer title equals the buyer's OWN declared related category
    for row in custom_bids:
        # (6.2) Query text comes ONLY from the buyer title via query_text() — never
        # from RELATED_COL. This single call site is the guarded source of truth.
        item_raw = query_text(row)
        if not item_raw:
            continue
        if not custom_fields:
            custom_fields = [k for k in row.keys() if k] or DEFAULT_FIELDS
        item_norm = _norm(item_raw)
        vflag     = item_norm in cat_norm_set                       # (6.3)
        if vflag:
            verbatim_ct += 1
        if item_norm and item_norm == _norm(row.get(RELATED_COL)):  # (6.4) watch bucket
            own_related_ct += 1
        items.append((row, item_raw, clean(item_raw), vflag))
    if not custom_fields:
        custom_fields = DEFAULT_FIELDS

    total = len(items)

    # ── (6.2) Leak tripwire ───────────────────────────────────────────────────
    # Buyer titles are free text: only a tiny fraction should ever be verbatim
    # category names. If a large share are, the query is almost certainly being
    # drawn from a category field (e.g. RELATED_COL) — refuse rather than ship a
    # meaningless category-vs-category comparison.
    if total and (verbatim_ct / total) > VERBATIM_LEAK_RATE:
        raise ValueError(
            f"LEAK TRIPWIRE — {verbatim_ct}/{total} ({verbatim_ct / total:.0%}) query texts are "
            f"verbatim category-master names (> {VERBATIM_LEAK_RATE:.0%}). The custom-side query "
            f"is almost certainly sourced from a category field (e.g. '{RELATED_COL}') instead of "
            f"the buyer title '{TITLE_COL}'. Refusing to run a category-vs-category comparison."
        )

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
                match_info[i] = (cat, round(sc, 3), "Weak (review)",
                                 "Nearest · below confidence threshold")

    # ── Assemble results, ordered from strongest match to weakest ─────────────
    # Output = every Custom Bid Extractor column carried over as-is, plus the
    # matched category (from the static CSV), the strength label, and the score.
    # Rows are sorted by confidence tier first, then by the numeric score within
    # that tier, so the highest matches appear at the top of both the dashboard
    # list and the downloaded CSV.
    progress(phase="Building results")
    scored     = []
    redflags   = []       # confident matches only → the red-flag CSV (Section 7)
    matched_n  = 0
    layer_ct   = {"Strong match": 0, "Likely match": 0, "Possible match": 0, "Weak (review)": 0}
    CONFIDENT  = {"Strong match", "Likely match", "Possible match"}
    TIER_RANK  = {"Strong match": 4, "Likely match": 3, "Possible match": 2, "Weak (review)": 1}
    for i, (row, item_raw, _clean, vflag) in enumerate(items):
        match, score, verdict, _layer = match_info[i]
        layer_ct[verdict] = layer_ct.get(verdict, 0) + 1
        out = {k: (row.get(k, "") or "") for k in custom_fields}
        out["Matched Category"]         = match or "—"
        out["Match Label"]              = verdict or "No match"
        out["Match Score"]              = format_score(score, verdict)
        out["Verbatim Category Title"]  = "Yes" if vflag else "No"   # (6.3) cross-check flag
        # normalise the score to 0-100 (fuzzy is already 0-100; cosine is 0-1)
        norm = float(score) if verdict == "Strong match" else float(score) * 100.0
        if verdict in CONFIDENT:
            matched_n += 1
            redflags.append({
                "custom_bid_id":                   row.get("Bid No", "") or "",
                "buyer_title":                     item_raw,
                "matched_gem_category":            match or "",
                "score":                           round(norm, 1),
                "match_layer":                     _layer_short(verdict),
                "buyer_declared_related_category": row.get(RELATED_COL, "") or "",  # cross-check
                "verbatim_flag":                   "Yes" if vflag else "No",
            })
        scored.append(((TIER_RANK.get(verdict, 0), norm), out))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = [out for _, out in scored]

    # Always write the full CSV so the frontend can offer a download. The new
    # "Verbatim Category Title" column is additive; existing columns are unchanged.
    progress(phase="Writing CSV", processed=total, matched=matched_n)
    fieldnames = custom_fields + ["Matched Category", "Match Label", "Match Score",
                                  "Verbatim Category Title"]
    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    # Section 7: red-flag matches (custom bids that clear threshold against a category).
    redflags.sort(key=lambda r: r["score"], reverse=True)
    _write_redflag_csv(redflags, REDFLAG_CSV)

    # Section 6.4: record + log the validation counts for every run.
    global LAST_RUN_STATS
    LAST_RUN_STATS = {
        "total_custom_bids_loaded": len(custom_bids),
        "query_set":                total,
        "corpus_size":              len(categories_original),
        "layer1_fuzzy":             layer_ct.get("Strong match", 0),
        "layer2_tfidf":             layer_ct.get("Likely match", 0),
        "layer3_embed":             layer_ct.get("Possible match", 0),
        "weak_below_threshold":     layer_ct.get("Weak (review)", 0),
        "redflag_matches":          len(redflags),
        "verbatim_category_titles": verbatim_ct,
        "title_equals_own_related": own_related_ct,
    }
    _print_validation_counts(LAST_RUN_STATS)

    print(f"Comparison: {total} custom bids vs {len(categories_original)} "
          f"categories → {output_csv}  ({matched_n} confident matches, "
          f"{len(redflags)} red flags → {REDFLAG_CSV})")
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

    # Same partition guards as the batched path (6.1) — CUSTOM query set, CATEGORY corpus.
    assert_query_partition(custom_bids)
    assert_corpus_partition(categories_original)

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
        item_raw = query_text(row)      # (6.2) buyer title only — never RELATED_COL

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


def validate(custom_limit=None, output_csv="compare_output.csv"):
    """
    Run the hardened CUSTOM → CATEGORY comparison over the full dataset and print
    the Section-6 validation summary plus the Section-7 red-flag CSV, ending with
    20 random sample matches to eyeball that the LEFT side is always custom free
    text and the RIGHT side is always a GeM category.
    """
    results = run_matching_job(compare_all=True, custom_limit=custom_limit,
                               output_csv=output_csv)
    _print_sample_matches(results, k=20)
    print(f"\n  Red-flag matches CSV : {REDFLAG_CSV}")
    print(f"  Full comparison CSV  : {output_csv}\n")
    return results


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="GeM custom-bid → category matching (hardened).")
    ap.add_argument("--legacy", action="store_true",
                    help="run the old per-item CLI (writes match_output.csv) instead of validation")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of custom bids (for a quick check)")
    args = ap.parse_args()
    if args.legacy:
        run_matching()
    else:
        validate(custom_limit=args.limit)