"""
collect_categories.py
=====================
Collects Item Category ONLY from true Category Bid/RA PDFs.

A Category Bid has NO GeMARPTS block. A Custom Bid HAS one.
GeM's "Product Bid/RAs" filter includes both, so this script downloads
each PDF, skips any that contain "GeMARPTS" (those are Custom Bids),
and saves Item Category only from true Category Bids.

Step 1 — collect product bid doc IDs:
    python3 collect_categories.py --collect-ids

Step 2 — extract Item Category from those PDFs:
    python3 collect_categories.py --ids-file product_ids.txt

Output: category_bids.csv  (Category Name | Category Bid No | Doc ID)
Resumable: re-run and it skips bids already in the CSV.
"""

import argparse, csv, io, os, re, time, logging
import requests
import pdfplumber

from gem_gemarpts_scraper import (
    build_session, prime_session,
    LISTING_URL, LISTING_HEADERS, HEADERS,
    DOC_URL_TEMPLATE, REQUEST_DELAY_SECONDS, REQUEST_TIMEOUT,
    CID_RE,
)
import json

# ── Config ────────────────────────────────────────────────────────────────────
PRODUCT_IDS_FILE = "product_ids.txt"
OUTPUT_CSV       = "category_bids.csv"
FAILED_LOG       = "category_failed.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("collect_categories")

BID_NO_RE = re.compile(r"GEM/\d{4}/[A-Z]/\d+", re.IGNORECASE)


# ── Step 1: collect product bid IDs ──────────────────────────────────────────
def build_product_payload(page, token):
    return {
        "payload": json.dumps({
            "page": page,
            "param": {"searchBid": "", "searchType": "fullText"},
            "filter": {
                "bidStatusType": "ongoing_bids",
                "byType": "product",          # ← Product Bid/RA
                "highBidValue": "",
                "byEndDate": {"from": "", "to": ""},
                "sort": "Bid-End-Date-Oldest",
            },
        }),
        "csrf_bd_gem_nk": token,
    }


def fetch_product_ids_page(session, page, token):
    """Returns list of doc IDs from one listing page."""
    data = build_product_payload(page, token)
    r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        return []
    docs = (((j or {}).get("response") or {}).get("response") or {}).get("docs") or []
    ids = []
    for d in docs:
        doc_id = d.get("id")
        if not doc_id and d.get("b_id"):
            doc_id = d["b_id"][0]
        if doc_id:
            ids.append(str(doc_id))
    return ids


def collect_ids(out_path=PRODUCT_IDS_FILE):
    """Page through Product Bid/RA listing and save all doc IDs."""
    seen = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            seen = {ln.strip() for ln in f if ln.strip()}
        log.info("Resuming: %d IDs already saved.", len(seen))

    session = build_session()
    token   = prime_session(session)
    log.info("Got session. Paging through Product Bid/RA listing...")

    page         = 1
    empty_streak = 0

    with open(out_path, "a") as f:
        while True:
            try:
                ids = fetch_product_ids_page(session, page, token)
            except Exception as e:
                log.warning("Page %d failed (%s) — waiting 30s before retry.", page, e)
                time.sleep(30)
                try:
                    token = prime_session(session)
                except Exception:
                    log.warning("Re-prime also failed — waiting another 30s.")
                    time.sleep(30)
                continue

            if not ids:
                empty_streak += 1
                if empty_streak == 1:
                    log.info("Page %d empty — re-priming to confirm end.", page)
                    try:
                        token = prime_session(session)
                    except Exception:
                        pass
                    time.sleep(REQUEST_DELAY_SECONDS)
                    continue
                log.info("Page %d empty again — end of listing.", page)
                break

            empty_streak = 0
            new = 0
            for doc_id in ids:
                if doc_id not in seen:
                    seen.add(doc_id)
                    f.write(doc_id + "\n")
                    new += 1
            f.flush()
            log.info("Page %3d | %d new IDs | %d total", page, new, len(seen))
            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    log.info("Done. %d product bid IDs saved to %s", len(seen), out_path)


# ── Step 2: extract Item Category from PDFs ───────────────────────────────────
def fetch_pdf_bytes(session, doc_id):
    """Download PDF into RAM. Returns bytes or raises."""
    url = DOC_URL_TEMPLATE.format(doc_id=doc_id)
    resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    content = resp.content
    if len(content) == 0:
        raise ValueError("Empty response — bid PDF no longer exists on server")
    if content[:4] != b'%PDF':
        raise ValueError(f"Non-PDF response (first 50 bytes: {content[:50]})")
    return content


def extract_from_pdf(pdf_bytes):
    """
    Extract Item Category and Bid No from a CATEGORY Bid PDF.
    Returns (item_category, bid_no, is_custom).

    is_custom=True means the PDF has a GeMARPTS block → it's a Custom Bid,
    NOT a category bid, so the caller should skip it.
    """
    item_category = None
    bid_no        = None
    is_custom     = False

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Read all text to check for GeMARPTS block
        full_text = ""
        for page in pdf.pages:
            full_text += (page.extract_text() or "") + "\n"

        # GeMARPTS block present → this is a Custom Bid, not a Category Bid
        if "gemarpts" in full_text.lower():
            is_custom = True
            return item_category, bid_no, is_custom

        # Bid No from page 1 text
        text = pdf.pages[0].extract_text() or ""
        m = BID_NO_RE.search(text)
        if m:
            bid_no = m.group(0)

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
                            item_category = val
                            break
                if item_category:
                    break
            if item_category:
                break

    return item_category, bid_no, is_custom


def load_done_bids(csv_path):
    """Return set of Category Bid Nos already in the output CSV."""
    done = set()
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                bid_no = (row.get("Category Bid No") or "").strip()
                if bid_no and bid_no != "—":
                    done.add(bid_no)
    return done


def iter_doc_ids(ids_file):
    """Yield doc IDs from the IDs file."""
    with open(ids_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield line


def extract_categories(ids_file, output_csv=OUTPUT_CSV):
    """Download PDFs and extract Item Category + Bid No from Category Bids only."""
    done = load_done_bids(output_csv)
    log.info("%d bids already in CSV, skipping those.", len(done))

    write_header = not os.path.exists(output_csv) or os.path.getsize(output_csv) == 0
    session      = build_session()
    written = failed = skipped_custom = 0

    with open(output_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Category Name", "Category Bid No", "Doc ID"]
        )
        if write_header:
            writer.writeheader()

        for doc_id in iter_doc_ids(ids_file):
            try:
                pdf_bytes = fetch_pdf_bytes(session, doc_id)
            except Exception as e:
                log.error("doc %s failed: %s", doc_id, e)
                with open(FAILED_LOG, "a") as fl:
                    fl.write(f"{doc_id}\t{e}\n")
                failed += 1
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            item_category, bid_no, is_custom = extract_from_pdf(pdf_bytes)

            # Skip custom bids — we only want category bids here
            if is_custom:
                skipped_custom += 1
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            # Skip if this bid already processed
            if bid_no and bid_no in done:
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            if not item_category:
                log.warning("doc %s — no item category found, skipping.", doc_id)
                failed += 1
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            writer.writerow({
                "Category Name"   : item_category,
                "Category Bid No" : bid_no or "—",
                "Doc ID"          : doc_id,
            })
            f.flush()
            if bid_no:
                done.add(bid_no)
            written += 1
            log.info("[%d] %s | %s", written, bid_no or "—", item_category[:50])
            time.sleep(REQUEST_DELAY_SECONDS)

    log.info("Done. %d category bids written, %d custom bids skipped, %d failed (see %s).",
             written, skipped_custom, failed, FAILED_LOG)


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Collect Item Category from Category Bid/RA PDFs only."
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--collect-ids", action="store_true",
                   help="Step 1: save all product bid doc IDs to product_ids.txt")
    g.add_argument("--ids-file",
                   help="Step 2: extract Item Category from these doc IDs")
    p.add_argument("--output", default=OUTPUT_CSV,
                   help="Output CSV path (default: category_bids.csv)")
    args = p.parse_args()

    if args.collect_ids:
        collect_ids()
    else:
        extract_categories(args.ids_file, args.output)


if __name__ == "__main__":
    main()