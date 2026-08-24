"""
lookup_bid_nos.py
=================
Targeted fix: finds Category Bid Nos for only the matched categories
in match_output.csv. Downloads ~1 PDF per matched category instead
of all 37,000.

Updates match_output.csv in place with real Category Bid Nos.

Run:
    python3 lookup_bid_nos.py
"""

import io, csv, re, time, logging
import requests
import pdfplumber

from gem_gemarpts_scraper import (
    build_session, prime_session,
    LISTING_URL, LISTING_HEADERS, HEADERS,
    DOC_URL_TEMPLATE, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT,
    CID_RE,
)
import json

MATCH_CSV = "match_output.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lookup")


# ── Search listing for a specific category name ───────────────────────────────
def search_listing_for_category(session, token, category_name):
    """
    Search the Product Bid listing using the category name as keyword.
    Returns list of doc IDs from the first page of results.
    """
    payload_obj = {
        "page": 1,
        "param": {
            "searchBid": category_name,    # ← search by category name
            "searchType": "fullText",
        },
        "filter": {
            "bidStatusType": "ongoing_bids",
            "byType": "product",
            "highBidValue": "",
            "byEndDate": {"from": "", "to": ""},
            "sort": "Bid-End-Date-Oldest",
        },
    }
    data = {
        "payload": json.dumps(payload_obj),
        "csrf_bd_gem_nk": token,
    }
    try:
        r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        docs = (((j or {}).get("response") or {}).get("response") or {}).get("docs") or []
        ids = []
        for d in docs:
            doc_id = d.get("id")
            if not doc_id and d.get("b_id"):
                doc_id = d["b_id"][0]
            if doc_id:
                ids.append(str(doc_id))
        return ids
    except Exception as e:
        log.warning("Search failed for '%s': %s", category_name[:40], e)
        return []


# ── Download PDF and extract item_category + bid_no ──────────────────────────
def get_category_and_bidno_from_pdf(session, doc_id):
    url = DOC_URL_TEMPLATE.format(doc_id=doc_id)
    try:
        resp    = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        content = resp.content
        if len(content) == 0 or content[:4] != b'%PDF':
            return None, None

        category = None
        bid_no   = None

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            # Bid number from page 1 text
            text = pdf.pages[0].extract_text() or ""
            m = re.search(r"(GEM/\d{4}/[A-Z]/\d+)", text, re.IGNORECASE)
            if m:
                bid_no = m.group(1)

            # Item Category from tables
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        cells = [c for c in row if c]
                        if len(cells) < 2:
                            continue
                        label = CID_RE.sub(" ", cells[0])
                        label = re.sub(r"[\u0900-\u097F]+", " ", label)
                        label = re.sub(r"\s+", " ", label).strip().lower()
                        if "item category" in label:
                            val = (cells[1] or "").replace("\n", " ").strip()
                            if val:
                                category = val
                                break
                    if category:
                        break
                if category:
                    break

        return category, bid_no
    except Exception:
        return None, None


def normalise(s):
    return re.sub(r"\s+", " ", (s or "").lower().strip())


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    # Load match_output.csv
    rows = []
    with open(MATCH_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)

    # Find unique matched categories that still have "—" bid no
    need_lookup = {}   # category_name → None (placeholder)
    for row in rows:
        cat    = (row.get("Matched Category") or "").strip()
        bid_no = (row.get("Category Bid No")  or "").strip()
        if cat and cat != "—" and bid_no in ("—", ""):
            need_lookup[cat] = None

    log.info("Need to find bid nos for %d unique matched categories", len(need_lookup))

    if not need_lookup:
        log.info("All category bid nos already filled in — nothing to do.")
        return

    session = build_session()
    token   = prime_session(session)

    # For each matched category, search + verify with PDF
    for i, category_name in enumerate(need_lookup, 1):
        log.info("[%d/%d] Searching for: %s", i, len(need_lookup), category_name[:60])

        doc_ids = search_listing_for_category(session, token, category_name)
        time.sleep(REQUEST_DELAY_SECONDS)

        found_bid_no = None

        for doc_id in doc_ids[:5]:   # check first 5 results max
            cat_in_pdf, bid_no_in_pdf = get_category_and_bidno_from_pdf(session, doc_id)
            time.sleep(REQUEST_DELAY_SECONDS)

            if cat_in_pdf and normalise(cat_in_pdf) == normalise(category_name):
                found_bid_no = bid_no_in_pdf
                log.info("  ✓ Found: %s", found_bid_no)
                break

        if not found_bid_no:
            # Didn't find exact match — use first result's bid no as best guess
            if doc_ids:
                _, bid_no_in_pdf = get_category_and_bidno_from_pdf(session, doc_ids[0])
                found_bid_no = bid_no_in_pdf
                log.info("  ~ Best guess: %s", found_bid_no)
                time.sleep(REQUEST_DELAY_SECONDS)
            else:
                log.info("  ✗ No results found")

        need_lookup[category_name] = found_bid_no or "—"

    # Update rows with found bid nos
    updated = 0
    for row in rows:
        cat = (row.get("Matched Category") or "").strip()
        if cat in need_lookup and need_lookup[cat] and need_lookup[cat] != "—":
            row["Category Bid No"] = need_lookup[cat]
            updated += 1

    # Write back to CSV
    with open(MATCH_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Done. Updated %d rows with Category Bid Nos → %s", updated, MATCH_CSV)


if __name__ == "__main__":
    run()