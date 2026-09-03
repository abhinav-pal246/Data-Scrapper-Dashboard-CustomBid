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

    Returns (pdf_doc_id, bid_no, department) as strings.
    """
    own_id = _first(doc.get("id")) or _first(doc.get("b_id"))
    own_no = _first(doc.get("b_bid_number")) or ""
    dept   = _first(doc.get("ba_official_details_deptName")) or ""

    par_id = _first(doc.get("b_id_parent"))
    par_no = _first(doc.get("b_bid_number_parent")) or ""

    if par_id:
        return str(par_id), (par_no or own_no), dept
    return (str(own_id) if own_id else None), own_no, dept


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
    Return [(pdf_doc_id, bid_no, department), …] for one listing page — each
    already resolved to the parent document that carries the GeMARPTS PDF.
    """
    targets = []
    for d in fetch_listing_docs(session, page, token):
        pdf_id, bid_no, dept = resolve_target(d)
        if pdf_id:
            targets.append((pdf_id, bid_no, dept))
    return targets


def fetch_listing_page(session, page, token):
    """Back-compat: page of resolved PDF doc-ids (parent-aware)."""
    return [t[0] for t in fetch_listing_targets(session, page, token)]


# ── Single-bid lookup: bid number → doc id, and classify (custom vs category) ──
def build_search_payload(page, token, query, by_type):
    import json as _json
    obj = {
        "page": page,
        "param": {"searchBid": query, "searchType": "fullText"},
        "filter": {
            "bidStatusType": "ongoing_bids",
            "byType": by_type,
            "highBidValue": "",
            "byEndDate": {"from": "", "to": ""},
            "sort": "Bid-End-Date-Oldest",
        },
    }
    return {"payload": _json.dumps(obj), "csrf_bd_gem_nk": token}


def _search_listing_docs(session, token, query, by_type):
    data = build_search_payload(1, token, query, by_type)
    r = session.post(LISTING_URL, data=data, headers=LISTING_HEADERS,
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        return []
    return (((j or {}).get("response") or {}).get("response") or {}).get("docs") or []


def search_bid_docid(session, token, bid_no):
    """
    Find the PDF doc id for a bid NUMBER by searching the ongoing listing
    (custom + product). Prefers an exact match on the bid's own or parent bid
    number; returns (pdf_doc_id, resolved_bid_no) or (None, None).
    """
    want = bid_no.strip().upper()
    for by in ("custom", "product"):
        try:
            docs = _search_listing_docs(session, token, want, by)
        except Exception:
            docs = []
        for d in docs:
            own = (_first(d.get("b_bid_number")) or "").upper()
            par = (_first(d.get("b_bid_number_parent")) or "").upper()
            if want in (own, par):
                pdf_id, resolved_no, _dept = resolve_target(d)
                return pdf_id, resolved_no
    return None, None


def classify_and_extract(session, doc_id):
    """
    Fetch the bid PDF and classify it: returns ('custom', item_category) when a
    GeMARPTS block is present, else ('category', None). Raises if the PDF can't
    be fetched (empty/expired).
    """
    pdf_bytes = fetch_pdf_bytes(session, doc_id)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = extract_full_text(pdf)
        if GEMARPTS_RE.search(text or ""):
            fields = extract_fields(pdf)
            return "custom", (fields.get("item_category") or "").strip()
        return "category", None


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
# State derivation (from the bid's Consignee / delivery address)
# ----------------------------------------------------------------------------
# GeM's listing carries no geographic state — only department/ministry. The one
# place a location appears is the "Consignee → Address" table inside the bid PDF,
# e.g. "…Jagdalpur, Dist.Bastar, CG 494001". We derive the state from that
# address, in order of reliability: an explicit state NAME, then the 6-digit
# PINCODE, then a 2-letter state CODE sitting right before the pincode.
#
# The pincode→state map is by postal circle (first 2-3 digits) and is
# analytics-grade, not authoritative — a handful of pincodes near circle borders
# can be attributed to a neighbouring state. Good enough for a state-wise
# breakdown; not a substitute for the consignee's own declared address.

# Full state / UT names (and common spellings) — matched first, most explicit.
_STATE_NAME_ALIASES = {
    "Andhra Pradesh": ["ANDHRA PRADESH"], "Arunachal Pradesh": ["ARUNACHAL PRADESH"],
    "Assam": ["ASSAM"], "Bihar": ["BIHAR"],
    "Chhattisgarh": ["CHHATTISGARH", "CHATTISGARH", "CHHATISGARH"],
    "Goa": ["GOA"], "Gujarat": ["GUJARAT"], "Haryana": ["HARYANA"],
    "Himachal Pradesh": ["HIMACHAL PRADESH", "HIMACHAL"], "Jharkhand": ["JHARKHAND"],
    "Jammu and Kashmir": ["JAMMU AND KASHMIR", "JAMMU & KASHMIR", "JAMMU AND KASHMIR"],
    "Karnataka": ["KARNATAKA"], "Kerala": ["KERALA"],
    "Madhya Pradesh": ["MADHYA PRADESH"], "Maharashtra": ["MAHARASHTRA"],
    "Manipur": ["MANIPUR"], "Meghalaya": ["MEGHALAYA"], "Mizoram": ["MIZORAM"],
    "Nagaland": ["NAGALAND"], "Odisha": ["ODISHA", "ORISSA"], "Punjab": ["PUNJAB"],
    "Rajasthan": ["RAJASTHAN"], "Sikkim": ["SIKKIM"],
    "Tamil Nadu": ["TAMIL NADU", "TAMILNADU"], "Telangana": ["TELANGANA"],
    "Tripura": ["TRIPURA"], "Uttar Pradesh": ["UTTAR PRADESH"],
    "Uttarakhand": ["UTTARAKHAND", "UTTARANCHAL"], "West Bengal": ["WEST BENGAL"],
    "Delhi": ["NEW DELHI", "DELHI"], "Ladakh": ["LADAKH"],
    "Puducherry": ["PUDUCHERRY", "PONDICHERRY"], "Chandigarh": ["CHANDIGARH"],
    "Andaman and Nicobar Islands": ["ANDAMAN AND NICOBAR", "ANDAMAN"],
    "Dadra and Nagar Haveli and Daman and Diu": ["DADRA AND NAGAR HAVELI", "DAMAN AND DIU"],
    "Lakshadweep": ["LAKSHADWEEP"],
}

# Canonical state / UT names the extractor can emit, for the UI state filter.
# The dropdown must use these exact strings so a selected state matches what
# derive_state() returns for a bid.
STATE_NAMES = sorted(_STATE_NAME_ALIASES.keys())

# 2-letter state codes as written in addresses (last-resort, only when hugging
# the pincode, e.g. "CG 494001").
_STATE_ABBR = {
    "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam", "BR": "Bihar",
    "CG": "Chhattisgarh", "CT": "Chhattisgarh", "GA": "Goa", "GJ": "Gujarat",
    "HR": "Haryana", "HP": "Himachal Pradesh", "JH": "Jharkhand",
    "JK": "Jammu and Kashmir", "KA": "Karnataka", "KL": "Kerala",
    "MP": "Madhya Pradesh", "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
    "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "OR": "Odisha",
    "PB": "Punjab", "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu",
    "TS": "Telangana", "TG": "Telangana", "TR": "Tripura", "UP": "Uttar Pradesh",
    "UK": "Uttarakhand", "UA": "Uttarakhand", "WB": "West Bengal", "DL": "Delhi",
    "LA": "Ladakh", "AN": "Andaman and Nicobar Islands", "CH": "Chandigarh",
    "PY": "Puducherry", "LD": "Lakshadweep",
}

# Pincode → state by first three digits (overrides), then first two (postal circle).
_PIN3_STATE = {
    "403": "Goa", "605": "Puducherry", "737": "Sikkim",
    "744": "Andaman and Nicobar Islands",
    "790": "Arunachal Pradesh", "791": "Arunachal Pradesh", "792": "Arunachal Pradesh",
    "793": "Meghalaya", "794": "Meghalaya", "795": "Manipur", "796": "Mizoram",
    "797": "Nagaland", "798": "Nagaland", "799": "Tripura",
    "248": "Uttarakhand", "249": "Uttarakhand", "263": "Uttarakhand",
}
_PIN2_STATE = {
    "11": "Delhi", "12": "Haryana", "13": "Haryana",
    "14": "Punjab", "15": "Punjab", "16": "Punjab", "17": "Himachal Pradesh",
    "18": "Jammu and Kashmir", "19": "Jammu and Kashmir",
    "20": "Uttar Pradesh", "21": "Uttar Pradesh", "22": "Uttar Pradesh",
    "23": "Uttar Pradesh", "24": "Uttar Pradesh", "25": "Uttar Pradesh",
    "26": "Uttar Pradesh", "27": "Uttar Pradesh", "28": "Uttar Pradesh",
    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan", "33": "Rajasthan",
    "34": "Rajasthan", "36": "Gujarat", "37": "Gujarat", "38": "Gujarat",
    "39": "Gujarat", "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra", "45": "Madhya Pradesh",
    "46": "Madhya Pradesh", "47": "Madhya Pradesh", "48": "Madhya Pradesh",
    "49": "Chhattisgarh", "50": "Telangana", "51": "Andhra Pradesh",
    "52": "Andhra Pradesh", "53": "Andhra Pradesh", "56": "Karnataka",
    "57": "Karnataka", "58": "Karnataka", "59": "Karnataka", "60": "Tamil Nadu",
    "61": "Tamil Nadu", "62": "Tamil Nadu", "63": "Tamil Nadu", "64": "Tamil Nadu",
    "65": "Tamil Nadu", "66": "Tamil Nadu", "67": "Kerala", "68": "Kerala",
    "69": "Kerala", "70": "West Bengal", "71": "West Bengal", "72": "West Bengal",
    "73": "West Bengal", "74": "West Bengal", "75": "Odisha", "76": "Odisha",
    "77": "Odisha", "78": "Assam", "80": "Bihar", "81": "Jharkhand",
    "82": "Jharkhand", "83": "Jharkhand", "84": "Bihar", "85": "Bihar",
}
# Major cities / district HQs → state. Used as a last resort, mainly for bids
# where GeM masks the address (e.g. "***********HYDERABAD") so the pincode is
# hidden but the city name survives. Not exhaustive; covers the common ones.
_CITY_STATE = {
    "HYDERABAD": "Telangana", "WARANGAL": "Telangana", "NIZAMABAD": "Telangana",
    "KARIMNAGAR": "Telangana", "SECUNDERABAD": "Telangana",
    "VISAKHAPATNAM": "Andhra Pradesh", "VIJAYAWADA": "Andhra Pradesh",
    "GUNTUR": "Andhra Pradesh", "TIRUPATI": "Andhra Pradesh", "NELLORE": "Andhra Pradesh",
    "AMARAVATI": "Andhra Pradesh", "KAKINADA": "Andhra Pradesh",
    "MUMBAI": "Maharashtra", "PUNE": "Maharashtra", "NAGPUR": "Maharashtra",
    "NASHIK": "Maharashtra", "AURANGABAD": "Maharashtra", "THANE": "Maharashtra",
    "SOLAPUR": "Maharashtra", "KOLHAPUR": "Maharashtra", "AMRAVATI": "Maharashtra",
    "NAVI MUMBAI": "Maharashtra", "PANVEL": "Maharashtra",
    "BENGALURU": "Karnataka", "BANGALORE": "Karnataka", "MYSURU": "Karnataka",
    "MYSORE": "Karnataka", "HUBBALLI": "Karnataka", "HUBLI": "Karnataka",
    "MANGALURU": "Karnataka", "MANGALORE": "Karnataka", "BELAGAVI": "Karnataka",
    "KALABURAGI": "Karnataka", "GULBARGA": "Karnataka",
    "CHENNAI": "Tamil Nadu", "COIMBATORE": "Tamil Nadu", "MADURAI": "Tamil Nadu",
    "TIRUCHIRAPPALLI": "Tamil Nadu", "TRICHY": "Tamil Nadu", "SALEM": "Tamil Nadu",
    "TIRUNELVELI": "Tamil Nadu", "ERODE": "Tamil Nadu", "VELLORE": "Tamil Nadu",
    "THIRUVANANTHAPURAM": "Kerala", "TRIVANDRUM": "Kerala", "KOCHI": "Kerala",
    "COCHIN": "Kerala", "KOZHIKODE": "Kerala", "CALICUT": "Kerala",
    "THRISSUR": "Kerala", "KOLLAM": "Kerala", "KANNUR": "Kerala",
    "KOLKATA": "West Bengal", "HOWRAH": "West Bengal", "SILIGURI": "West Bengal",
    "DURGAPUR": "West Bengal", "ASANSOL": "West Bengal", "KHARAGPUR": "West Bengal",
    "BHUBANESWAR": "Odisha", "CUTTACK": "Odisha", "ROURKELA": "Odisha",
    "SAMBALPUR": "Odisha", "BERHAMPUR": "Odisha", "PURI": "Odisha",
    "PATNA": "Bihar", "GAYA": "Bihar", "BHAGALPUR": "Bihar", "MUZAFFARPUR": "Bihar",
    "DARBHANGA": "Bihar", "PURNIA": "Bihar",
    "RANCHI": "Jharkhand", "JAMSHEDPUR": "Jharkhand", "DHANBAD": "Jharkhand",
    "BOKARO": "Jharkhand", "HAZARIBAGH": "Jharkhand",
    "LUCKNOW": "Uttar Pradesh", "KANPUR": "Uttar Pradesh", "AGRA": "Uttar Pradesh",
    "VARANASI": "Uttar Pradesh", "PRAYAGRAJ": "Uttar Pradesh", "ALLAHABAD": "Uttar Pradesh",
    "MEERUT": "Uttar Pradesh", "GHAZIABAD": "Uttar Pradesh", "NOIDA": "Uttar Pradesh",
    "BAREILLY": "Uttar Pradesh", "GORAKHPUR": "Uttar Pradesh", "ALIGARH": "Uttar Pradesh",
    "JAIPUR": "Rajasthan", "JODHPUR": "Rajasthan", "UDAIPUR": "Rajasthan",
    "KOTA": "Rajasthan", "AJMER": "Rajasthan", "BIKANER": "Rajasthan",
    "BHOPAL": "Madhya Pradesh", "INDORE": "Madhya Pradesh", "GWALIOR": "Madhya Pradesh",
    "JABALPUR": "Madhya Pradesh", "UJJAIN": "Madhya Pradesh", "SAGAR": "Madhya Pradesh",
    "RAIPUR": "Chhattisgarh", "BHILAI": "Chhattisgarh", "BILASPUR": "Chhattisgarh",
    "KORBA": "Chhattisgarh", "JAGDALPUR": "Chhattisgarh",
    "AHMEDABAD": "Gujarat", "SURAT": "Gujarat", "VADODARA": "Gujarat",
    "RAJKOT": "Gujarat", "BHAVNAGAR": "Gujarat", "GANDHINAGAR": "Gujarat",
    "JAMNAGAR": "Gujarat",
    "GURUGRAM": "Haryana", "GURGAON": "Haryana", "FARIDABAD": "Haryana",
    "PANIPAT": "Haryana", "AMBALA": "Haryana", "HISAR": "Haryana", "KARNAL": "Haryana",
    "ROHTAK": "Haryana",
    "LUDHIANA": "Punjab", "AMRITSAR": "Punjab", "JALANDHAR": "Punjab",
    "PATIALA": "Punjab", "BATHINDA": "Punjab", "MOHALI": "Punjab",
    "SHIMLA": "Himachal Pradesh", "DHARAMSHALA": "Himachal Pradesh", "MANDI": "Himachal Pradesh",
    "SOLAN": "Himachal Pradesh",
    "SRINAGAR": "Jammu and Kashmir", "JAMMU": "Jammu and Kashmir",
    "LEH": "Ladakh",
    "DEHRADUN": "Uttarakhand", "HARIDWAR": "Uttarakhand", "HALDWANI": "Uttarakhand",
    "RUDRAPUR": "Uttarakhand", "ROORKEE": "Uttarakhand", "NAINITAL": "Uttarakhand",
    "GUWAHATI": "Assam", "DIBRUGARH": "Assam", "SILCHAR": "Assam", "JORHAT": "Assam",
    "TEZPUR": "Assam",
    "ITANAGAR": "Arunachal Pradesh", "SHILLONG": "Meghalaya", "IMPHAL": "Manipur",
    "AIZAWL": "Mizoram", "KOHIMA": "Nagaland", "DIMAPUR": "Nagaland",
    "AGARTALA": "Tripura", "GANGTOK": "Sikkim",
    "PANAJI": "Goa", "PANJIM": "Goa", "MARGAO": "Goa", "VASCO": "Goa",
    "CHANDIGARH": "Chandigarh", "PUDUCHERRY": "Puducherry", "PONDICHERRY": "Puducherry",
    "PORT BLAIR": "Andaman and Nicobar Islands",
    "NEW DELHI": "Delhi", "DELHI": "Delhi",
}
# Match multi-word cities before their single-word substrings.
_CITY_STATE_ORDERED = sorted(_CITY_STATE, key=len, reverse=True)

_PINCODE_RE = re.compile(r"\b(\d{6})\b")


def _pin_to_state(pincode):
    """Map a 6-digit pincode to a state (3-digit override, then 2-digit circle)."""
    if not pincode or len(pincode) != 6:
        return ""
    return _PIN3_STATE.get(pincode[:3]) or _PIN2_STATE.get(pincode[:2], "")


def derive_state(address_text):
    """
    Best-effort state from a consignee/address string:
    explicit state name → pincode → 2-letter code before the pincode. "" if none.
    """
    if not address_text:
        return ""
    text  = str(address_text)
    upper = text.upper()

    # 1. Explicit state / UT name (word-boundary match).
    for state, aliases in _STATE_NAME_ALIASES.items():
        for alias in aliases:
            if re.search(r"\b" + re.escape(alias) + r"\b", upper):
                return state

    # 2. Pincode → postal circle.
    m = _PINCODE_RE.search(text)
    if m:
        st = _pin_to_state(m.group(1))
        if st:
            return st

    # 3. A 2-letter code hugging the pincode, e.g. "CG 494001" / "CG-494001".
    m = re.search(r"\b([A-Z]{2})\b[\s,\-]*\d{6}", upper)
    if m and m.group(1) in _STATE_ABBR:
        return _STATE_ABBR[m.group(1)]

    # 4. A known city / district HQ (mainly for masked addresses like
    #    "***********HYDERABAD" where the pincode is redacted). Longest names
    #    first so "NAVI MUMBAI" wins over "MUMBAI", "NEW DELHI" over "DELHI".
    for city in _CITY_STATE_ORDERED:
        if re.search(r"\b" + re.escape(city) + r"\b", upper):
            return _CITY_STATE[city]
    return ""


# Consignee/address table header cues.
_CONSIGNEE_HDR_RE = re.compile(r"consignee|reporting", re.IGNORECASE)
_ADDRESS_HDR_RE   = re.compile(r"address", re.IGNORECASE)


def extract_consignee_state(pdf):
    """
    Derive the buyer/consignee state from the first Consignee→Address row of the
    bid PDF. Falls back to scanning any address-like text with a pincode.
    Returns a state name or "" when it can't be determined.
    """
    # Preferred: the Consignee/Address table — read the address cell of row 1.
    for page in pdf.pages:
        for table in page.extract_tables():
            if not table or len(table) < 2:
                continue
            header = " ".join((c or "") for c in table[0])
            if not (_CONSIGNEE_HDR_RE.search(header) and _ADDRESS_HDR_RE.search(header)):
                continue
            for row in table[1:]:
                # Derive from the whole row: the address may be masked
                # ("***********HYDERABAD") so the city — not a pincode — is the
                # only signal left, and derive_state handles both.
                joined = " ".join((c or "").replace("\n", " ") for c in row)
                st = derive_state(joined)
                if st:
                    return st
    # Fallback: first address-like line anywhere in the document text.
    text = extract_full_text(pdf)
    for line in (text or "").splitlines():
        if _PINCODE_RE.search(line):
            st = derive_state(line)
            if st:
                return st
    return ""


# ----------------------------------------------------------------------------
# PILLAR 6 : CSV
# ----------------------------------------------------------------------------

CSV_HEADER = ["Bid No", "Item Category", "Searched Strings",
              "Searched Result", "Relevant Categories", "Department", "State"]


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
                    row["searched_result"], row["relevant_categories"],
                    row.get("department", ""), row.get("state", "")])
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
        state  = extract_consignee_state(pdf)   # buyer/consignee delivery state

    resolved = bid_no or (BID_NO_RE.search(text).group(0)
                          if BID_NO_RE.search(text) else doc_id)

    if not any(fields.values()):
        log_failure(resolved, doc_id, "no GeMARPTS fields found")
        return None

    return {"bid_no": resolved, "state": state, **fields}


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