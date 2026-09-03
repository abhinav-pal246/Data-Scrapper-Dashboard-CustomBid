from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import threading
import os
import time
import csv as _csv
import requests

import re
import io as _io
import pdfplumber as _pdfplumber
from gem_gemarpts_scraper import (
    process_one, build_session, append_row,
    load_processed_bids, prime_session, fetch_listing_page,
    fetch_listing_targets, search_bid_docid, classify_and_extract,
    extract_consignee_state, STATE_NAMES
)
from collect_categories import (
    fetch_product_ids_page, fetch_pdf_bytes as cat_fetch_pdf, extract_from_pdf
)
from match import run_matching_job, match_single, recommend

BID_NO_PATTERN = re.compile(r"^GEM/\d{4}/[A-Z]/\d+$", re.IGNORECASE)

app = Flask(__name__)
CORS(app)

COMPARE_CSV       = "compare_output.csv"
INLINE_ROW_LIMIT  = 300     # results at/below this are shown in the dashboard


# ══════════════════════════════════════════════════════════════════════════════
#  Pausable job framework — shared by Custom Extractor, Category Extractor and
#  the Comparison pipeline so Pause / Resume / Cancel behave identically.
#
#    Start  = fresh run (overwrites that module's output)
#    Pause  = stop but keep everything collected so far (already on disk) and
#             hold position; the worker thread stays alive for instant resume
#    Resume = continue the SAME job, appending — final file = old + new
#    Cancel = discard the current partial run and reset so a new range can start
# ══════════════════════════════════════════════════════════════════════════════
class _Cancelled(Exception):
    """Raised inside a worker when the job has been cancelled."""


MAX_AUTO_RETRIES = 4        # quick auto-retries on network/server errors before HOLD


def _retry_guard(fn, *, checkpoint, update, cancelled, take_retry,
                 label="", on_hold=None, cancel_exc=None):
    """
    Run fn(). On a transient network/server error (requests exceptions incl.
    5xx), auto-retry with backoff up to MAX_AUTO_RETRIES; if still failing, go
    on HOLD — all progress preserved — until a manual retry (take_retry() → True)
    or cancel. Auto-retry and manual retry both re-run fn from the SAME point.
    Non-network errors propagate to the caller (treated as permanent).
    `cancel_exc` is the exception raised on cancel (per-module).
    """
    cancel_exc = cancel_exc or _Cancelled
    attempt = 0
    while True:
        checkpoint()                                   # pause / cancel aware
        try:
            result = fn()
            if attempt:
                update(status="running", attempt=0, hold_reason="")
            return result
        except cancel_exc:
            raise
        except requests.exceptions.RequestException as e:
            attempt += 1
            reason = f"{label} — {type(e).__name__}".strip(" —")
            if attempt < MAX_AUTO_RETRIES:
                update(status="retrying", attempt=attempt,
                       max_attempts=MAX_AUTO_RETRIES, hold_reason=reason)
                deadline = time.time() + min(2 ** attempt, 20)   # interruptible backoff
                while time.time() < deadline:
                    if cancelled():
                        raise cancel_exc()
                    time.sleep(0.1)
                continue
            # auto-retries exhausted → HOLD (state preserved)
            update(status="hold", attempt=attempt,
                   max_attempts=MAX_AUTO_RETRIES, hold_reason=reason)
            if on_hold:
                try:
                    on_hold()
                except Exception as ex:
                    print(f"on_hold hook failed: {ex}")
            while True:
                if cancelled():
                    raise cancel_exc()
                if take_retry():
                    break                              # user pressed Retry
                time.sleep(0.3)
            update(status="running", attempt=0, hold_reason="")
            attempt = 0                                # try fn again from here


class JobControl:
    def __init__(self):
        self.lock       = threading.Lock()
        self.pause_evt  = threading.Event()
        self.cancel_evt = threading.Event()
        self.retry_evt  = threading.Event()
        self.thread     = None
        self.state      = self._fresh()

    @staticmethod
    def _fresh():
        return {"status": "idle", "phase": "", "collected": 0, "total": 0,
                "written": 0, "failed": 0, "paused": False, "done": False,
                "error": "", "attempt": 0, "max_attempts": MAX_AUTO_RETRIES,
                "hold_reason": ""}

    def reset(self):
        with self.lock:
            self.state = self._fresh()
        self.pause_evt.clear()
        self.cancel_evt.clear()
        self.retry_evt.clear()

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def get(self):
        with self.lock:
            return dict(self.state)

    def is_live(self):
        return self.thread is not None and self.thread.is_alive()

    def checkpoint(self, on_pause=None):
        """
        Called by a worker between units of work. Blocks while paused (keeping
        the thread — and all its in-memory position — alive) and raises when
        cancelled. `on_pause` runs once on entering the paused state (used by
        the comparison to write a partial result so it can be exported).
        """
        if self.cancel_evt.is_set():
            raise _Cancelled()
        entered = False
        while self.pause_evt.is_set() and not self.cancel_evt.is_set():
            if not entered:
                self.update(status="paused", paused=True)
                if on_pause:
                    try:
                        on_pause()
                    except Exception as e:
                        print(f"on_pause hook failed: {e}")
                entered = True
            time.sleep(0.3)
        if self.cancel_evt.is_set():
            raise _Cancelled()
        if entered:
            self.update(status="running", paused=False)

    def _take_retry(self):
        if self.retry_evt.is_set():
            self.retry_evt.clear()
            return True
        return False

    def guard(self, fn, label="", on_pause=None):
        """Wrap a network call with auto-retry → hold → manual-retry."""
        return _retry_guard(
            fn,
            checkpoint=lambda: self.checkpoint(on_pause),
            update=self.update,
            cancelled=self.cancel_evt.is_set,
            take_retry=self._take_retry,
            label=label,
        )

# ── Comparison job state (separate from the scraper job) ──────────────────────
def _fresh_compare_job():
    return {
        "status":       "idle",   # idle | running | done | error
        "phase":        "",
        "collected":    0,        # custom bids extracted so far (collect phase)
        "collect_total":0,        # custom bids to extract
        "failed":       0,        # custom bids that couldn't be extracted
        "total":        0,        # custom bids to match
        "processed":    0,        # custom bids matched so far
        "matched":      0,
        "encoded":      0,        # embeddings encoded so far (current encode phase)
        "encode_total": 0,
        "result_total": 0,
        "inline":       False,
        "error":        "",
        "done":         False,
        "paused":       False,
        "attempt":      0,
        "max_attempts": MAX_AUTO_RETRIES,
        "hold_reason":  "",
    }

compare_job     = _fresh_compare_job()
compare_lock    = threading.Lock()
compare_pause   = threading.Event()   # set = pause the comparison worker
compare_retry   = threading.Event()   # set = manual retry from hold
compare_results = []          # last run's rows, held in memory for /result


# ── On-demand custom-bid collection ───────────────────────────────────────────
CUSTOM_OUTPUT_CSV = "gemarpts_output.csv"   # Extractor's fresh output


def collect_custom_bids(session, needed, compare_all, progress,
                        checkpoint=None, out_rows=None, guard=None):
    """
    Collect custom bids from the live GeM listing, extracting each via its
    parent document (RA entries carry their GeMARPTS PDF on the parent bid) and
    keeping ONLY active bids (a real PDF with an Item Category). Over-fetches
    past expired ones until `needed` active bids are gathered (or all).

    `checkpoint()` — called each item for pause/cancel support.
    `out_rows` — live list to append working bids to (partial export).
    `guard(fn, label)` — wraps network calls with auto-retry → hold → retry.

    Returns the active custom-bid dicts ({"Bid No", "Item Category"}).
    """
    rows       = out_rows if out_rows is not None else []
    failed     = 0
    seen_bidno = {r["Bid No"] for r in rows}
    seen_ids   = set()
    unbounded  = compare_all or needed is None
    progress(phase="Collecting custom bids",
             collect_total=(0 if unbounded else needed), collected=len(rows), failed=0)

    tok = [prime_session(session)]

    def _fetch_page():
        try:
            return fetch_listing_targets(session, page, tok[0])
        except requests.exceptions.RequestException:
            tok[0] = prime_session(session)
            return fetch_listing_targets(session, page, tok[0])

    def _extract(pid, bno):
        try:
            return process_one(session, bno, pid)
        except requests.exceptions.RequestException:
            raise                            # transient → guard retries/holds
        except Exception:
            return None                      # permanent → skip

    page = 1
    while unbounded or len(rows) < needed:
        if checkpoint:
            checkpoint()
        if guard:
            targets = guard(_fetch_page, "Fetching bid listing")
        else:
            try:
                targets = _fetch_page()
            except Exception:
                time.sleep(1.5)
                continue
        if not targets:
            break                            # listing exhausted
        page += 1

        for pdf_id, bid_no, *_dept in targets:
            if checkpoint:
                checkpoint()
            if not unbounded and len(rows) >= needed:
                break
            if pdf_id in seen_ids:
                continue
            seen_ids.add(pdf_id)

            if guard:
                row = guard(lambda: _extract(pdf_id, bid_no), "Fetching bid document")
            else:
                row = _extract(pdf_id, bid_no)

            item = (row or {}).get("item_category", "").strip() if row else ""
            resolved_no = (row or {}).get("bid_no", "") or bid_no
            if item and resolved_no and resolved_no not in seen_bidno:
                seen_bidno.add(resolved_no)
                rows.append({"Bid No": resolved_no, "Item Category": item})
            else:
                failed += 1                  # expired/empty → ignore, don't store

            time.sleep(1.5)                  # respect GeM rate limits
            progress(collected=len(rows), failed=failed)

    return rows

# ══════════════════════════════════════════════════════════════════════════════
#  Two extractor modules — Custom Bid + Category Bid — on the shared framework.
# ══════════════════════════════════════════════════════════════════════════════
import json

CATEGORY_OUTPUT_CSV = "category_bids.csv"


def _load_done_custom(path):
    done = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in _csv.reader(f):
                if r and r[0] and r[0] != "Bid No":
                    done.add(r[0])
    return done


def _load_done_category(path):
    done = set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                b = (r.get("Category Bid No") or "").strip()
                if b and b != "—":
                    done.add(b)
    return done


# ── State filter (post-fetch: state is derived from each bid's PDF) ────────────
# GeM's listing carries no state, so a bid's state is only known after its PDF is
# fetched and parsed. The filter therefore runs during extraction: a valid bid
# whose derived state is not selected is returned as _FILTERED — skipped without
# being stored, and counted separately from genuine extraction failures.
_FILTERED = object()


def _norm_state(s):
    return " ".join((s or "").strip().lower().split())


_STATE_SET = {_norm_state(s) for s in STATE_NAMES}


def _clean_states(raw):
    """Normalise a list of state names to a set of known states, or None for
    'all states' (no filter). Unknown names are dropped."""
    if not raw or not isinstance(raw, list):
        return None
    picked = {_norm_state(s) for s in raw if _norm_state(s) in _STATE_SET}
    return picked or None


def _state_ok(state, states):
    """True when no filter is active, or the bid's derived state is selected."""
    return not states or _norm_state(state) in states


def _extract_custom(session, pdf_id, listing_bid_no, dept="", states=None):
    """
    Extract a custom bid via its (parent) PDF. Returns (bid_no, writer), None
    (permanent skip), or _FILTERED (valid bid, excluded by the state filter).
    Transient network/server errors are re-raised so the retry/hold guard can
    handle them. The buyer `dept` (from the listing) is stored alongside the
    extracted fields.
    """
    try:
        row = process_one(session, listing_bid_no, pdf_id)
    except requests.exceptions.RequestException:
        raise                                    # transient → guard retries/holds
    except Exception:
        row = None                               # permanent → skip
    item = (row or {}).get("item_category", "").strip() if row else ""
    no   = (row or {}).get("bid_no", "") or listing_bid_no
    if item and no:
        if not _state_ok(row.get("state", ""), states):
            return _FILTERED                     # not in a selected state → skip
        row["department"] = dept
        return no, (lambda: append_row(CUSTOM_OUTPUT_CSV, row))
    return None


def _append_category_row(path, name, bid_no, doc_id):
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["Category Name", "Category Bid No", "Doc ID"])
        if new:
            w.writeheader()
        w.writerow({"Category Name": name, "Category Bid No": bid_no, "Doc ID": doc_id})


def _extract_category(session, pdf_id, listing_bid_no, dept="", states=None):
    """
    Extract a true Category Bid (skips Custom Bids, which carry a GeMARPTS block).
    Returns (bid_no, writer), None, or _FILTERED (excluded by the state filter).
    `dept` is accepted for a uniform signature but not stored (category output has
    its own schema). State is only derived when a filter is active, to avoid the
    extra PDF parse otherwise.
    """
    try:
        pdf = cat_fetch_pdf(session, pdf_id)
    except requests.exceptions.RequestException:
        raise                                    # transient → guard retries/holds
    except Exception:
        return None                              # permanent (empty/non-PDF) → skip
    try:
        item, bno, is_custom = extract_from_pdf(pdf)
    except Exception:
        return None
    if is_custom or not item:
        return None                          # custom or empty → ignore, don't store
    if states:
        try:
            with _pdfplumber.open(_io.BytesIO(pdf)) as doc:
                st = extract_consignee_state(doc)
        except Exception:
            st = ""
        if not _state_ok(st, states):
            return _FILTERED                     # not in a selected state → skip
    no = bno or listing_bid_no or "—"
    return no, (lambda: _append_category_row(CATEGORY_OUTPUT_CSV, item, no, pdf_id))


# ── Module registry ───────────────────────────────────────────────────────────
EXTRACTORS = {
    "custom": {
        "ctrl":      JobControl(),
        "output":    CUSTOM_OUTPUT_CSV,
        "dl_name":   "gem_gemarpts_data.csv",
        "listing":   lambda s, p, t: fetch_listing_targets(s, p, t),   # parent-aware
        "extract":   _extract_custom,
        "load_done": _load_done_custom,
        "meta":      "custom_meta.json",
    },
    "category": {
        "ctrl":      JobControl(),
        "output":    CATEGORY_OUTPUT_CSV,
        "dl_name":   "category_bids.csv",
        "listing":   lambda s, p, t: [(i, "", "") for i in fetch_product_ids_page(s, p, t)],
        "extract":   _extract_category,
        "load_done": _load_done_category,
        "meta":      "category_meta.json",
    },
}


def _save_meta(cfg, target, is_all, states=None):
    try:
        with open(cfg["meta"], "w") as f:
            json.dump({"target": target, "is_all": is_all,
                       "states": sorted(states) if states else None}, f)
    except Exception:
        pass


def _load_meta(cfg):
    try:
        with open(cfg["meta"]) as f:
            return json.load(f)
    except Exception:
        return {}


def _run_extractor(cfg, target, is_all, resume, states=None):
    """
    Fresh (resume=False) wipes the output first; resume=True keeps it and skips
    bids already stored, so the final CSV is cumulative. Pages the live listing,
    extracts active bids only, and honours pause/cancel at every step.

    `states` (a set of normalised state names, or None) is the state filter: when
    set, only bids delivered to those states are kept, and the `target` count is
    the number of MATCHING bids to collect — scanning continues past bids in other
    states until the target is met or the listing is exhausted.
    """
    ctrl   = cfg["ctrl"]
    output = cfg["output"]
    try:
        if resume and os.path.exists(output):
            done = cfg["load_done"](output)          # keep prior session's data
        else:
            open(output, "w").close()                # fresh run
            done = set()

        written  = len(done)
        failed   = 0
        filtered = 0
        collected = 0
        ctrl.update(status="running", phase="collecting", written=written,
                    failed=0, filtered=0, collected=0, total=(target or 0),
                    done=False, error="", paused=False)

        session = build_session()
        token   = [prime_session(session)]           # boxed so nested fn can refresh it
        seen_ids = set()
        page     = 1
        first    = True

        def _fetch_page():
            # Refresh the CSRF token on a transient failure, then let the guard
            # decide whether to retry again or go on hold.
            try:
                return cfg["listing"](session, page, token[0])
            except requests.exceptions.RequestException:
                token[0] = prime_session(session)
                return cfg["listing"](session, page, token[0])

        while True:
            ctrl.checkpoint()
            if not is_all and written >= target:
                break
            targets = ctrl.guard(_fetch_page, "Fetching bid listing")
            if not targets:
                break                                # listing exhausted
            page += 1
            collected += len(targets)
            if first:
                ctrl.update(phase="extracting")
                first = False
            ctrl.update(collected=collected, total=(collected if is_all else target))

            for pdf_id, bid_no, dept in targets:
                ctrl.checkpoint()
                if not is_all and written >= target:
                    break
                if pdf_id in seen_ids:
                    continue
                seen_ids.add(pdf_id)

                res = ctrl.guard(lambda: cfg["extract"](session, pdf_id, bid_no, dept, states),
                                 "Fetching bid document")
                if res is _FILTERED:
                    filtered += 1                    # valid bid, other state → skip
                    ctrl.update(filtered=filtered)
                elif res and res[0] not in done:
                    res[1]()                         # write the row
                    done.add(res[0])
                    written += 1
                    ctrl.update(written=written)
                else:
                    failed += 1                      # expired/custom/empty → skip
                    ctrl.update(failed=failed)

                time.sleep(1.5)

        ctrl.update(status="done", phase="done", done=True)

    except _Cancelled:
        ctrl.reset()                                 # discard partial, back to idle
    except Exception as e:
        ctrl.update(status="error", error=str(e), done=True)


# ── Extractor endpoints (mod = custom | category) ─────────────────────────────
@app.route("/api/states", methods=["GET"])
def list_states():
    """Canonical India state/UT names for the extractor's state filter."""
    return jsonify({"states": STATE_NAMES})


def _extractor_or_404(mod):
    cfg = EXTRACTORS.get(mod)
    return cfg


@app.route("/api/extract/<mod>/start", methods=["POST"])
def ext_start(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    ctrl = cfg["ctrl"]
    if ctrl.is_live() and not ctrl.get()["done"]:
        return jsonify({"error": "A job is already running. Pause or cancel it first."}), 409

    data  = request.get_json(silent=True) or {}
    count = data.get("count", "all")
    is_all = (count == "all")
    try:
        target = None if is_all else int(count)
    except (TypeError, ValueError):
        target = None
    if not is_all and (target is None or target <= 0):
        return jsonify({"error": "Enter a valid number of bids, or choose ALL."}), 400

    states = _clean_states(data.get("states"))   # None = all states (no filter)

    ctrl.reset()
    ctrl.update(status="running", phase="collecting")
    _save_meta(cfg, target, is_all, states)
    t = threading.Thread(target=_run_extractor, args=(cfg, target, is_all, False),
                         kwargs={"states": states})
    t.daemon = True
    ctrl.thread = t
    t.start()
    return jsonify({"message": "started"})


@app.route("/api/extract/<mod>/pause", methods=["POST"])
def ext_pause(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    cfg["ctrl"].pause_evt.set()
    return jsonify({"message": "pausing"})


@app.route("/api/extract/<mod>/resume", methods=["POST"])
def ext_resume(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    ctrl = cfg["ctrl"]
    if ctrl.is_live():
        ctrl.pause_evt.clear()                       # same-session: just continue
        ctrl.update(status="running", paused=False)
        return jsonify({"message": "resumed"})

    # No live thread (e.g. server was restarted) → resume from the CSV on disk.
    meta   = _load_meta(cfg)
    target = meta.get("target")
    is_all = meta.get("is_all", target is None)
    states = _clean_states(meta.get("states"))
    ctrl.reset()
    ctrl.update(status="running", phase="collecting")
    t = threading.Thread(target=_run_extractor, args=(cfg, target, is_all, True),
                         kwargs={"states": states})
    t.daemon = True
    ctrl.thread = t
    t.start()
    return jsonify({"message": "resumed"})


@app.route("/api/extract/<mod>/retry", methods=["POST"])
def ext_retry(mod):
    """Manual retry from HOLD — release the held worker to re-attempt from the
    exact point it stopped. Only acts on a live worker sitting on hold."""
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    ctrl = cfg["ctrl"]
    if ctrl.is_live():
        ctrl.retry_evt.set()
        return jsonify({"message": "retrying"})
    return jsonify({"message": "no job on hold"}), 409


@app.route("/api/extract/<mod>/cancel", methods=["POST"])
def ext_cancel(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    ctrl = cfg["ctrl"]
    ctrl.cancel_evt.set()
    ctrl.pause_evt.clear()                           # unblock a paused worker so it can exit
    ctrl.retry_evt.set()                             # unblock a held worker so it can exit
    if not ctrl.is_live():
        ctrl.reset()
    return jsonify({"message": "cancelled"})


@app.route("/api/extract/<mod>/status", methods=["GET"])
def ext_status(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    return jsonify(cfg["ctrl"].get())


@app.route("/api/extract/<mod>/download", methods=["GET"])
def ext_download(mod):
    cfg = _extractor_or_404(mod)
    if not cfg:
        return jsonify({"error": "Unknown module"}), 404
    if os.path.exists(cfg["output"]):
        return send_file(cfg["output"], as_attachment=True, download_name=cfg["dl_name"])
    return jsonify({"error": "No data yet"}), 404


# ── Comparison background worker ──────────────────────────────────────────────
compare_gen = 0                 # bumped on every start/cancel; supersedes old runs


class _Aborted(Exception):
    """Raised inside a worker when a newer run/cancel has superseded it."""


def _make_progress(my_gen):
    """A progress callback that aborts the worker once it's been superseded."""
    def progress(**fields):
        if my_gen != compare_gen:
            raise _Aborted()
        with compare_lock:
            if my_gen == compare_gen:
                compare_job.update(fields)
    return progress


def run_compare(custom_limit, category_limit, compare_all, my_gen):
    """
    Fully LOCAL comparison — no scraping. Custom bids come from the Custom Bid
    Extractor's output (gemarpts_output.csv); categories come from the static
    reference CSV. Matches with the 3-layer pipeline and writes the full CSV.
    """
    global compare_results
    progress = _make_progress(my_gen)

    def checkpoint():
        # pause + cancel(abort). No network here, so no retry/hold state.
        if my_gen != compare_gen:
            raise _Aborted()
        entered = False
        while compare_pause.is_set() and my_gen == compare_gen:
            if not entered:
                with compare_lock:
                    compare_job.update(status="paused", paused=True)
                entered = True
            time.sleep(0.3)
        if my_gen != compare_gen:
            raise _Aborted()
        if entered:
            with compare_lock:
                compare_job.update(status="running", paused=False)

    try:
        results = run_matching_job(
            custom_limit=custom_limit,
            category_limit=category_limit,
            compare_all=compare_all,
            output_csv=COMPARE_CSV,
            progress=progress,
            checkpoint=checkpoint,
        )
        if not results:
            raise RuntimeError(
                "No extracted custom bids found — run the Custom Bid Extractor "
                "first, then start the comparison."
            )
        total = len(results)
        with compare_lock:
            if my_gen != compare_gen:
                return                       # superseded just before finishing
            compare_results = results
            compare_job.update({
                "status":       "done",
                "phase":        "Done",
                "done":         True,
                "result_total": total,
                "inline":       total <= INLINE_ROW_LIMIT,
            })
    except _Aborted:
        return                               # a newer run/cancel owns the state now
    except Exception as e:
        with compare_lock:
            if my_gen == compare_gen:
                compare_job.update({"status": "error", "error": str(e), "done": True})


# ── Endpoint 4: Compare — start background matching job ───────────────────────
@app.route("/api/compare", methods=["POST"])
def compare():
    """
    Starts the local three-layer matching pipeline (match.py) in the background.
    Poll /api/compare/status for progress, then GET /api/compare/result.

    Request JSON:
        customCount    — number of custom bids to compare (ignored if compareAll)
        categoryCount  — number of category bids to compare against (ignored if compareAll)
        compareAll     — bool; compare ALL custom bids against ALL category bids
    """
    global compare_results, compare_gen

    data        = request.get_json(silent=True) or {}
    compare_all = bool(data.get("compareAll", False))

    def parse_limit(value):
        try:
            n = int(value)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    custom_limit   = None if compare_all else parse_limit(data.get("customCount"))
    category_limit = None      # always compare against ALL categories in the loaded CSV

    if not compare_all and custom_limit is None:
        return jsonify({
            "error": "Enter a valid number of custom bids, or turn on Compare All."
        }), 400

    # Starting a run always supersedes any previous one (bumping the generation
    # aborts a stale/stuck worker), so the user can never get locked out.
    compare_pause.clear()
    compare_retry.clear()
    with compare_lock:
        compare_gen += 1
        my_gen = compare_gen
        compare_results = []
        compare_job.clear()
        compare_job.update(_fresh_compare_job())
        compare_job.update({"status": "running", "phase": "Starting"})

    thread = threading.Thread(
        target=run_compare, args=(custom_limit, category_limit, compare_all, my_gen)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"message": "Comparison started"})


# ── Pause / Resume a comparison ───────────────────────────────────────────────
@app.route("/api/compare/pause", methods=["POST"])
def compare_pause_ep():
    """Pause collection and match-so-far so it can be exported."""
    compare_pause.set()
    return jsonify({"message": "pausing"})


@app.route("/api/compare/resume", methods=["POST"])
def compare_resume_ep():
    compare_pause.clear()
    with compare_lock:
        if compare_job.get("status") == "paused":
            compare_job.update(status="running", paused=False)
    return jsonify({"message": "resumed"})


@app.route("/api/compare/retry", methods=["POST"])
def compare_retry_ep():
    """Manually retry a held comparison (network back)."""
    compare_retry.set()
    return jsonify({"message": "retrying"})


# ── Endpoint: Cancel / reset a comparison ─────────────────────────────────────
@app.route("/api/compare/cancel", methods=["POST"])
def compare_cancel():
    """Supersede any running comparison and reset state back to idle."""
    global compare_gen
    compare_pause.clear()                    # release a paused worker so it aborts
    compare_retry.clear()
    with compare_lock:
        compare_gen += 1                     # abort whatever worker is running
        compare_job.clear()
        compare_job.update(_fresh_compare_job())
    return jsonify({"message": "Comparison cancelled"})


# ── Endpoint 5: Compare status (progress polling) ─────────────────────────────
@app.route("/api/compare/status", methods=["GET"])
def compare_status():
    with compare_lock:
        return jsonify(dict(compare_job))


# ── How many custom bids are available to compare (extractor output size) ─────
@app.route("/api/compare/available", methods=["GET"])
def compare_available():
    n = 0
    if os.path.exists(CUSTOM_OUTPUT_CSV):
        with open(CUSTOM_OUTPUT_CSV, newline="", encoding="utf-8") as f:
            for r in _csv.reader(f):
                if r and r[0] and r[0] != "Bid No":
                    n += 1
    return jsonify({"available": n})


# ── Endpoint 6: Compare result (rows, once done) ──────────────────────────────
@app.route("/api/compare/result", methods=["GET"])
def compare_result():
    # Always return every row — the dashboard renders the full result set
    # (no 300-row cap). Empty while a run is still collecting with no results yet.
    with compare_lock:
        rows   = list(compare_results)
        total  = compare_job.get("result_total") or len(rows)
        status = compare_job.get("status")
    return jsonify({"total": total, "rows": rows, "status": status})


# ── Category Recommendation: closest existing category for an item description ─
@app.route("/api/recommend", methods=["POST"])
def recommend_ep():
    """
    Given a buyer's item category / requirement text, return the closest
    existing GeM categories (primary + alternatives). The frontend applies GeM
    policy to advise whether a standard Category Bid should be used.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if len(text) < 3:
        return jsonify({"error": "Enter the item category or requirement to get a recommendation."}), 400
    try:
        return jsonify(recommend(text))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Bid Lookup: classify a single bid & match it (on-demand) ──────────────────
@app.route("/api/lookup", methods=["POST"])
def lookup():
    """
    Look up one bid number: resolve it to its document, classify it
    (Custom vs Category by the GeMARPTS block), and — if Custom — match its
    item category against the reference list.

    Response:
      found=False                          → not in the active listing
      classification="category"           → "N/A — already a category bid"
      classification="custom" + match{...} → matched category, score, layer
    """
    data   = request.get_json(silent=True) or {}
    bid_no = (data.get("bidNo") or "").strip().upper()

    if not BID_NO_PATTERN.match(bid_no):
        return jsonify({"error": "Please enter a complete bid number, for example GEM/2026/B/1234567."}), 400

    try:
        session = build_session()
        token   = prime_session(session)
        pdf_id, resolved = search_bid_docid(session, token, bid_no)
        if not pdf_id:
            return jsonify({
                "bidNo": bid_no, "found": False,
                "message": "This bid was not found in the current active listing. "
                           "It may be closed or expired.",
            })

        classification, item = classify_and_extract(session, pdf_id)
        if classification == "category":
            return jsonify({
                "bidNo": bid_no, "found": True, "classification": "category",
                "message": "This is already an existing category bid.",
            })

        # Custom bid → run the match pipeline on its item category.
        match = match_single(item) if item else None
        return jsonify({
            "bidNo": bid_no, "found": True, "classification": "custom",
            "itemCategory": item, "match": match,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Endpoint 7: Download comparison result ────────────────────────────────────
@app.route("/api/compare/download", methods=["GET"])
def compare_download():
    if os.path.exists(COMPARE_CSV):
        return send_file(
            COMPARE_CSV,
            as_attachment=True,
            download_name="custom_bid_comparison.csv",
        )
    return jsonify({"error": "No comparison has been run yet"}), 404


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)