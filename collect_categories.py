"""
collect_categories.py
=====================
Collects ALL unique item category names + Category Bid Nos from Product Bid/RAs.
Saves to category_bids.csv  (Category Name | Category Bid No | Doc ID)

Resumable: re-run and it skips already-processed doc IDs.

Run:
    python3 collect_categories.py
"""

import io, os, re, time, logging, json, csv
import requests
import pdfplumber

from gem_gemarpts_scraper import (
    build_session, prime_session,
    LISTING_URL, LISTING_HEADERS, HEADERS,
    DOC_URL_TEMPLATE, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT,
    CID_RE,
)

OUTPUT_CSV    = "category_bids.csv"
PROGRESS_FILE = "category_progress.txt"

# Known Solr field names GeM uses — we try all of them
BID_NO_FIELDS  = ["bid_no", "b_bid_no", "bidNo", "b_no", "bid_number",
                   "b_bid_no_s", "bidno", "Bid No"]
CAT_FIELDS     = ["cat_name", "b_cat_name", "category_name", "b_category_name",
                   "item_category", "name"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collect_categories")


def build_product_payload(page, token):
    return {
        "payload": json.dumps({
            "page": page,
            "param": {"searchBid": "", "searchType": "fullText"},
            "filter": {
                "bidStatusType": "ongoing_bids",
                "byType": "product",
                "highBidValue": "",
                "byEndDate": {"from": "", "to": ""},
                "sort": "Bid-End-Date-Oldest",
            },
        }),
        "csrf_bd_gem_nk": token,
    }


def get_field(doc, fields):
    """Try a list of field names and return the first non-empty value."""
    for f in fields:
        val = doc.get(f)
        if val:
            return val[0] if isinstance(val, list) else str(val)
    return None


def fetch_listing_page(session, page, token):
    """Returns list of (doc_id, bid_no_or_None, category_or_None)."""
    data = build_product_payload(page, token)
    r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        return []

    docs = (((j or {}).get("response") or {}).get("response") or {}).get("docs") or []

    # ── Debug: print ALL fields of first doc on page 1 so we know what's available
    if page == 1 and docs:
        log.info("DEBUG — Available JSON fields in first doc: %s",
                 list(docs[0].keys()))

    results = []
    for d in docs:
        doc_id = get_field(d, ["id", "b_id"])
        if not doc_id:
            continue
        bid_no   = get_field(d, BID_NO_FIELDS)
        category = get_field(d, CAT_FIELDS)
        results.append((str(doc_id), bid_no, category))
    return results


def extract_from_pdf(session, doc_id):
    """
    Called ONLY when the JSON didn't return the category name.
    Gets both category and bid_no from the PDF in one download.
    """
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


def load_existing():
    category_map = {}    # category_name → {bid_no, doc_id}
    if os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                name = (row.get("Category Name") or "").strip()
                if name:
                    category_map[name] = {
                        "bid_no": row.get("Category Bid No", "—"),
                        "doc_id": row.get("Doc ID", "—"),
                    }
    return category_map


def collect_all_categories():
    category_map  = load_existing()
    log.info("Loaded %d existing categories.", len(category_map))

    processed_ids = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            processed_ids = {ln.strip() for ln in f if ln.strip()}
        log.info("Skipping %d already-processed doc IDs.", len(processed_ids))

    session = build_session()
    token   = prime_session(session)
    log.info("Session ready. Starting to page through Product Bid/RAs...")

    page          = 1
    empty_streak  = 0
    pdf_fallbacks = 0

    write_header = not os.path.exists(OUTPUT_CSV) or os.path.getsize(OUTPUT_CSV) == 0
    cat_f  = open(OUTPUT_CSV,    "a", newline="", encoding="utf-8")
    prog_f = open(PROGRESS_FILE, "a", encoding="utf-8")
    writer = csv.DictWriter(
        cat_f, fieldnames=["Category Name", "Category Bid No", "Doc ID"]
    )
    if write_header:
        writer.writeheader()

    try:
        while True:
            try:
                results = fetch_listing_page(session, page, token)
            except Exception as e:
                log.warning("Page %d failed (%s) — re-priming.", page, e)
                try:
                    token = prime_session(session)
                except Exception:
                    pass
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            if not results:
                empty_streak += 1
                if empty_streak == 1:
                    log.info("Page %d empty — re-priming to confirm.", page)
                    try:
                        token = prime_session(session)
                    except Exception:
                        pass
                    time.sleep(REQUEST_DELAY_SECONDS)
                    continue
                log.info("Page %d empty again — end of listing.", page)
                break

            empty_streak  = 0
            new_this_page = 0

            for doc_id, bid_no, category in results:
                if doc_id in processed_ids:
                    continue

                # Only download PDF if category name not in JSON
                # (bid_no absence alone is NOT a reason to download PDF)
                if not category:
                    cat_pdf, bid_pdf = extract_from_pdf(session, doc_id)
                    category = cat_pdf
                    if not bid_no:
                        bid_no = bid_pdf
                    pdf_fallbacks += 1
                    time.sleep(REQUEST_DELAY_SECONDS)

                if category and category.strip() not in category_map:
                    cat_clean = category.strip()
                    category_map[cat_clean] = {
                        "bid_no": bid_no or "—",
                        "doc_id": doc_id,
                    }
                    writer.writerow({
                        "Category Name"   : cat_clean,
                        "Category Bid No" : bid_no or "—",
                        "Doc ID"          : doc_id,
                    })
                    cat_f.flush()
                    new_this_page += 1

                processed_ids.add(doc_id)
                prog_f.write(doc_id + "\n")
                prog_f.flush()

            log.info(
                "Page %3d | new: %3d | total unique: %5d | pdf fallbacks: %d",
                page, new_this_page, len(category_map), pdf_fallbacks,
            )
            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    finally:
        cat_f.close()
        prog_f.close()

    log.info("Done. %d unique categories saved to %s", len(category_map), OUTPUT_CSV)


if __name__ == "__main__":
    collect_all_categories()