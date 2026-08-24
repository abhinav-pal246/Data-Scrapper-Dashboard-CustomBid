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

import csv, os, pickle, re, time
import numpy as np
from rapidfuzz import process as fuzz_process, fuzz
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer

# ── Config ────────────────────────────────────────────────────────────────────
INPUT_CSV        = "gemarpts_output.csv"
CATEGORY_CSV     = "category_bids.csv"     # ← now a CSV with bid nos
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
def load_categories():
    """
    Loads category_bids.csv.
    Returns:
        categories_original  — list of category names (for matching)
        category_bid_map     — dict {category_name: Category Bid No}
    """
    categories_original = []
    category_bid_map    = {}

    with open(CATEGORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name   = (row.get("Category Name") or "").strip()
            bid_no = (row.get("Category Bid No") or "—").strip()
            if name:
                categories_original.append(name)
                category_bid_map[name] = bid_no

    print(f"Loaded {len(categories_original)} standard categories from {CATEGORY_CSV}")
    return categories_original, category_bid_map


def load_custom_bids():
    rows = []
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if any(row.values()):
                rows.append(row)
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