"""
GeM GeMARPTS Extractor  (v2 - table-aware)
==========================================

Pulls the 4 GeMARPTS fields out of GeM "Product Custom Bid/RA" PDFs at scale,
WITHOUT ever saving a PDF to disk. Each PDF is fetched into RAM, read as a
TABLE (not flat text, because the block is a bilingual Hindi-English table),
reduced to 4 fields, and discarded. Only a CSV grows on disk.

The 4 fields:
    1. Item Category
    2. Searched Strings
    3. Searched Result
    4. Relevant Categories (selected for notification)

Run:
    python3 gem_gemarpts_scraper.py --ids-file ids.txt
    python3 gem_gemarpts_scraper.py --ids-file ids.txt --dump   # diagnostic
    python3 gem_gemarpts_scraper.py --crawl                     # needs wiring

Output: gemarpts_output.csv  (+ failed_bids.log)
Resumable: re-run and it skips bids already in the CSV.
"""

import argparse
import csv
import io
import logging
import os
import re
import time

import requests
import pdfplumber
from bs4 import BeautifulSoup


# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

BASE_URL = "https://bidplus.gem.gov.in"
DOC_URL_TEMPLATE = BASE_URL + "/showbidDocument/{doc_id}"

OUTPUT_CSV = "gemarpts_output.csv"
FAILED_LOG = "failed_bids.log"

REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT = 30
MAX_RETRIES = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    )
}

FIELD_MATCHERS = [
    ("item_category",       re.compile(r"Item\s+Category", re.IGNORECASE)),
    ("searched_strings",    re.compile(r"Searched\s+Strings", re.IGNORECASE)),
    ("searched_result",     re.compile(r"Searched\s+Result", re.IGNORECASE)),
    ("relevant_categories", re.compile(r"Relevant\s+Categories", re.IGNORECASE)),
]

LABEL_STRIP = {
    "item_category":       re.compile(r"Item\s+Category", re.IGNORECASE),
    "searched_strings":    re.compile(r"Searched\s+Strings(\s+used\s+in\s+GeMARPTS)?", re.IGNORECASE),
    "searched_result":     re.compile(r"Searched\s+Result(\s+generated\s+in\s+GeMARPTS)?", re.IGNORECASE),
    "relevant_categories": re.compile(r"Relevant\s+Categories(\s+selected\s+for\s+notification)?", re.IGNORECASE),
}

CID_RE        = re.compile(r"\(cid:\d+\)")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]+")
GEMARPTS_RE   = re.compile(r"GeMARPTS", re.IGNORECASE)
BID_NO_RE     = re.compile(r"GEM/\d{4}/[A-Z]/\d+", re.IGNORECASE)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("gemarpts")


# ----------------------------------------------------------------------------
# cleaning
# ----------------------------------------------------------------------------

def clean_cell(s):
    if not s:
        return ""
    s = CID_RE.sub(" ", s)
    s = DEVANAGARI_RE.sub(" ", s)
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_label(value, key):
    v = LABEL_STRIP[key].sub(" ", value)
    v = GEMARPTS_RE.sub(" ", v)
    v = v.replace("/", " ")
    v = re.sub(r"\s+", " ", v).strip(" :/|-.")
    return v.strip()


# ----------------------------------------------------------------------------
# PILLAR 1 + 2 : collect showbidDocument IDs
# ----------------------------------------------------------------------------

LISTING_URL = BASE_URL + "/all-bids-data"
ALL_BIDS_URL = BASE_URL + "/all-bids"
IDS_FILE = "all_ids.txt"

LISTING_HEADERS = dict(HEADERS)
LISTING_HEADERS.update({
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Referer": ALL_BIDS_URL,
    "Accept": "application/json, text/javascript, */*; q=0.01",
})

DOC_ID_RE = re.compile(r"showbidDocument\\?/(\d+)")


def prime_session(session):
    r = session.get(ALL_BIDS_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    token = session.cookies.get("csrf_gem_cookie")
    if not token:
        m = re.search(r"csrf[\w-]*['\"]?\s*[:=]\s*['\"]([0-9a-f]{16,})", r.text, re.I)
        token = m.group(1) if m else ""
    if not token:
        raise RuntimeError("Could not obtain CSRF token from /all-bids")
    return token


def build_payload(page, token):
    payload_obj = {
        "page": page,
        "param": {"searchBid": "", "searchType": "fullText"},
        "filter": {
            "bidStatusType": "ongoing_bids",
            "byType": "custom",
            "highBidValue": "",
            "byEndDate": {"from": "", "to": ""},
            "sort": "Bid-End-Date-Oldest",
        },
    }
    import json as _json
    return {"payload": _json.dumps(payload_obj), "csrf_bd_gem_nk": token}


def _first(v):
    """Solr fields arrive as single-item lists — unwrap them."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def resolve_target(doc):
    """
    Map one listing record to the document that actually holds the GeMARPTS
    data. Active custom bids are often RA (Reverse Auction) entries whose own
    showbidDocument is EMPTY — the real PDF lives under their parent bid
    (GEM/…/B/…). So prefer the parent id + parent bid number when present.

    Returns (pdf_doc_id, bid_no) as strings.
    """
    own_id = _first(doc.get("id")) or _first(doc.get("b_id"))
    own_no = _first(doc.get("b_bid_number")) or ""

    par_id = _first(doc.get("b_id_parent"))
    par_no = _first(doc.get("b_bid_number_parent")) or ""

    if par_id:
        return str(par_id), (par_no or own_no)
    return str(own_id) if own_id else None, own_no


def fetch_listing_docs(session, page, token):
    """Return the raw listing records for a page (for parent resolution)."""
    data = build_payload(page, token)
    r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        return []
    return (((j or {}).get("response") or {}).get("response") or {}).get("docs") or []


def fetch_listing_targets(session, page, token):
    """
    Return [(pdf_doc_id, bid_no), …] for one listing page — each already
    resolved to the parent document that carries the GeMARPTS PDF.
    """
    targets = []
    for d in fetch_listing_docs(session, page, token):
        pdf_id, bid_no = resolve_target(d)
        if pdf_id:
            targets.append((pdf_id, bid_no))
    return targets


def fetch_listing_page(session, page, token):
    """Back-compat: page of resolved PDF doc-ids (parent-aware)."""
    return [pdf_id for pdf_id, _ in fetch_listing_targets(session, page, token)]


def collect_ids_to_file(session, out_path=IDS_FILE):
    seen = set()
    if os.path.exists(out_path):
        with open(out_path) as f:
            seen = {ln.strip() for ln in f if ln.strip()}
        log.info("Resuming id-collection: %d ids already saved.", len(seen))

    token = prime_session(session)
    log.info("Got CSRF token, starting to page through the listing.")

    page = 1
    empty_streak = 0
    with open(out_path, "a") as f:
        while True:
            try:
                ids = fetch_listing_page(session, page, token)
            except Exception as e:
                log.warning("page %d failed (%s) - re-priming session.", page, e)
                token = prime_session(session)
                time.sleep(REQUEST_DELAY_SECONDS)
                continue

            if not ids:
                empty_streak += 1
                if empty_streak == 1:
                    log.info("page %d empty, re-priming to confirm end.", page)
                    token = prime_session(session)
                    time.sleep(REQUEST_DELAY_SECONDS)
                    continue
                log.info("page %d empty again - reached the end.", page)
                break

            empty_streak = 0
            new = 0
            for doc_id in ids:
                if doc_id not in seen:
                    seen.add(doc_id)
                    f.write(doc_id + "\n")
                    new += 1
            f.flush()
            log.info("page %d: %d ids (%d new, %d total)", page, len(ids), new, len(seen))
            page += 1
            time.sleep(REQUEST_DELAY_SECONDS)

    log.info("Done collecting. %d unique ids in %s", len(seen), out_path)
    return len(seen)


# ----------------------------------------------------------------------------
# PILLAR 3 : bytes into RAM  ← UPDATED
# ----------------------------------------------------------------------------

def fetch_pdf_bytes(session, doc_id):
    """
    Fetch the PDF for doc_id into memory.

    Three outcomes handled:

      1. Empty body (len == 0)
         → Bid has expired / PDF deleted from GeM's server.
            The server still returns 200 + Content-Type: application/pdf
            but sends zero bytes. This is permanent — no point retrying.
            Raises immediately.

      2. Non-PDF body (doesn't start with %PDF)
         → Server returned an HTML page (rate-limit, session error, etc).
            This is transient — worth retrying with exponential back-off.

      3. Real PDF (starts with %PDF)
         → Return the bytes.
    """
    url = DOC_URL_TEMPLATE.format(doc_id=doc_id)
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        wait = min(2 ** attempt, 60)

        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            content = resp.content

            # ── Case 1: empty body ─────────────────────────────────────────
            # Bid expired or PDF removed. Server sends 200 but zero bytes.
            # Permanent failure — raise immediately, do NOT retry.
            if len(content) == 0:
                raise ValueError(
                    "Empty response body — bid PDF no longer exists on server "
                    "(bid likely expired before extraction)"
                )

            # ── Case 2: non-PDF body ───────────────────────────────────────
            # Server returned HTML instead of a PDF (session/rate-limit page).
            # Transient — log and retry with back-off.
            if content[:4] != b'%PDF':
                last_err = ValueError(
                    f"Non-PDF response (first 100 bytes: {content[:100]})"
                )
                log.warning(
                    "doc %s | attempt %d/%d: got non-PDF response "
                    "(session/rate-limit page). Retrying in %ds...",
                    doc_id, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            # ── Case 3: real PDF ───────────────────────────────────────────
            return content

        except ValueError:
            # Re-raise ValueError immediately — covers the empty-body case
            # so we don't fall through to the retry loop.
            raise

        except Exception as e:
            last_err = e
            log.warning(
                "doc %s | attempt %d/%d: %s. Retrying in %ds...",
                doc_id, attempt, MAX_RETRIES, e, wait,
            )
            time.sleep(wait)

    raise last_err


# ----------------------------------------------------------------------------
# PILLAR 4 + 5 : table-based field extraction
# ----------------------------------------------------------------------------

def match_key(cell):
    for key, matcher in FIELD_MATCHERS:
        if matcher.search(cell):
            return key
    return None


def extract_fields(pdf):
    result = {k: "" for k, _ in FIELD_MATCHERS}

    for page in pdf.pages:
        for table in page.extract_tables():
            for row in table:
                cells = [clean_cell(c) for c in row if c is not None]
                cells = [c for c in cells if c]
                if not cells:
                    continue

                label_idx = None
                key = None
                for i, cell in enumerate(cells):
                    k = match_key(cell)
                    if k:
                        label_idx, key = i, k
                        break
                if key is None or result[key]:
                    continue

                others = [c for i, c in enumerate(cells) if i != label_idx]
                value = " ".join(others).strip()
                if not value:
                    value = strip_label(cells[label_idx], key)
                else:
                    value = strip_label(value, key)

                if value:
                    result[key] = value

    return result


def extract_full_text(pdf):
    parts = []
    for page in pdf.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# PILLAR 6 : CSV
# ----------------------------------------------------------------------------

CSV_HEADER = ["Bid No", "Item Category", "Searched Strings",
              "Searched Result", "Relevant Categories"]


def load_processed_bids(csv_path):
    done = set()
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and row[0] and row[0] != "Bid No":
                    done.add(row[0])
    return done


def append_row(csv_path, row):
    new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(CSV_HEADER)
        w.writerow([row["bid_no"], row["item_category"], row["searched_strings"],
                    row["searched_result"], row["relevant_categories"]])
        w.writerow([])


def log_failure(bid_no, doc_id, reason):
    with open(FAILED_LOG, "a", encoding="utf-8") as f:
        f.write(f"{bid_no}\t{doc_id}\t{reason}\n")


# ----------------------------------------------------------------------------
# diagnostics
# ----------------------------------------------------------------------------

def dump_structure(session, doc_id):
    pdf_bytes = fetch_pdf_bytes(session, doc_id)
    print("\n" + "=" * 70)
    print("DOC ID:", doc_id, "  size:", len(pdf_bytes), "bytes")
    print("=" * 70)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables()
            print(f"\n--- page {pno}: {len(tables)} table(s) ---")
            for tno, table in enumerate(tables, 1):
                print(f"\n  TABLE {tno}:")
                for row in table:
                    cells = [clean_cell(c) for c in (row or [])]
                    if any(cells):
                        print("   ", cells)
            if pno == 1:
                print("\n--- page 1 RAW TEXT (cleaned) ---")
                print(clean_cell(page.extract_text() or "")[:1500])


# ----------------------------------------------------------------------------
# orchestration
# ----------------------------------------------------------------------------

def iter_targets(ids_file):
    with open(ids_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            m = re.search(r"(\d+)$", line)
            if m:
                yield "", m.group(1)


def build_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def process_one(session, bid_no, doc_id):
    pdf_bytes = fetch_pdf_bytes(session, doc_id)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = extract_full_text(pdf)
        if not text.strip():
            log_failure(bid_no or "?", doc_id, "no text layer (scanned, needs OCR)")
            return None
        fields = extract_fields(pdf)

    resolved = bid_no or (BID_NO_RE.search(text).group(0)
                          if BID_NO_RE.search(text) else doc_id)

    if not any(fields.values()):
        log_failure(resolved, doc_id, "no GeMARPTS fields found")
        return None

    return {"bid_no": resolved, **fields}


def debug_listing(session):
    token = prime_session(session)
    data = build_payload(1, token)
    r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                     timeout=REQUEST_TIMEOUT)
    print("STATUS:", r.status_code)
    print("CONTENT-TYPE:", r.headers.get("Content-Type"))
    print("LENGTH:", len(r.text))
    with open("listing_page1.json", "w", encoding="utf-8") as f:
        f.write(r.text)
    print("Saved full response to listing_page1.json")
    print("=" * 70)
    print(r.text[:3000])
    print("=" * 70)


def main():
    p = argparse.ArgumentParser(description="Extract GeMARPTS fields from GeM custom-bid PDFs.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--collect-ids", action="store_true",
                   help="step 1: crawl the listing and save every doc id to a file")
    g.add_argument("--debug-listing", action="store_true",
                   help="fetch page 1 of the listing and print the raw response")
    g.add_argument("--ids-file", help="step 2: extract from a file of ids (one per line)")
    p.add_argument("--ids-out", default=IDS_FILE, help="where --collect-ids writes ids")
    p.add_argument("--output", default=OUTPUT_CSV, help="CSV path for extraction")
    p.add_argument("--dump", action="store_true",
                   help="with --ids-file: print table structure instead of writing CSV")
    args = p.parse_args()

    session = build_session()

    if args.debug_listing:
        debug_listing(session)
        return

    if args.collect_ids:
        collect_ids_to_file(session, args.ids_out)
        return

    if args.dump:
        for bid_no, doc_id in iter_targets(args.ids_file):
            dump_structure(session, doc_id)
            time.sleep(REQUEST_DELAY_SECONDS)
        return

    done = load_processed_bids(args.output)
    log.info("%d bids already in CSV, skipping those.", len(done))

    written = failed = 0
    for bid_no, doc_id in iter_targets(args.ids_file):
        if bid_no and bid_no in done:
            continue
        try:
            row = process_one(session, bid_no, doc_id)
        except Exception as e:
            log.error("doc %s failed: %s", doc_id, e)
            log_failure(bid_no or "?", doc_id, str(e))
            failed += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        if row is None:
            failed += 1
        elif row["bid_no"] not in done:
            append_row(args.output, row)
            done.add(row["bid_no"])
            written += 1
            log.info("[%d] %s | %s", written, row["bid_no"], row["item_category"][:45])
        time.sleep(REQUEST_DELAY_SECONDS)

    log.info("Done. %d written, %d failed (see %s).", written, failed, FAILED_LOG)


if __name__ == "__main__":
    main()