from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import threading
import os
import time

from gem_gemarpts_scraper import (
    process_one, build_session, append_row,
    load_processed_bids, prime_session, fetch_listing_page,
    fetch_listing_targets
)
from match import run_matching_job

app = Flask(__name__)
CORS(app)

COMPARE_CSV       = "compare_output.csv"
INLINE_ROW_LIMIT  = 300     # results at/below this are shown in the dashboard

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
    }

compare_job     = _fresh_compare_job()
compare_lock    = threading.Lock()
compare_results = []          # last run's rows, held in memory for /result


# ── On-demand custom-bid collection ───────────────────────────────────────────
CUSTOM_OUTPUT_CSV = "gemarpts_output.csv"   # Extractor's fresh output


def collect_custom_bids(session, needed, compare_all, progress):
    """
    Collect custom bids FRESH from the live GeM listing every run — no cache,
    no resume. Pages the ongoing Product Custom Bid/RA listing, extracts each
    via its parent document (RA entries carry their GeMARPTS PDF on the parent
    bid), and keeps ONLY active bids (a real PDF with an Item Category).
    Expired/empty bids are skipped and never stored. Over-fetches past expired
    ones until `needed` active bids are gathered (or all, for Compare All).

    Returns the active custom-bid dicts ({"Bid No", "Item Category"}).
    """
    rows       = []
    failed     = 0
    seen_bidno = set()
    seen_ids   = set()
    unbounded  = compare_all or needed is None
    progress(phase="Collecting custom bids",
             collect_total=(0 if unbounded else needed), collected=0, failed=0)

    token = prime_session(session)
    page  = 1
    while unbounded or len(rows) < needed:
        try:
            targets = fetch_listing_targets(session, page, token)
        except Exception:
            token = prime_session(session)   # re-prime and retry the same page
            time.sleep(1.5)
            continue
        if not targets:
            break                            # listing exhausted
        page += 1

        for pdf_id, bid_no in targets:
            if not unbounded and len(rows) >= needed:
                break
            if pdf_id in seen_ids:
                continue
            seen_ids.add(pdf_id)

            try:
                row = process_one(session, bid_no, pdf_id)
            except Exception:
                row = None

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

# ── Shared job state ──────────────────────────────────────────────────────────
job = {
    "status":    "idle",
    "phase":     "",        # collecting | extracting
    "collected": 0,         # IDs found so far (phase 1)
    "total":     0,         # total to extract (phase 2)
    "written":   0,
    "failed":    0,
    "done":      False,
    "error":     ""
}


# ── Background function ───────────────────────────────────────────────────────
def run_scraper(count):
    global job
    try:
        session = build_session()
        is_all  = (count == "all")
        target  = None if is_all else int(count)

        # ── Fresh start: wipe previous output so each run reflects what is
        #    live on GeM right now (no resume, no stale/expired bids kept). ──
        open(CUSTOM_OUTPUT_CSV, "w").close()

        job["phase"] = "collecting"
        token        = prime_session(session)

        # ── Page the live listing and extract as we go, keeping ONLY active
        #    bids (real PDF via the parent doc). Expired/empty are skipped and
        #    never stored. Over-fetch past expired until `target` active bids. ─
        job["total"] = target or 0
        seen_ids   = set()
        seen_bidno = set()
        page       = 1
        first      = True

        while is_all or job["written"] < target:
            targets = fetch_listing_targets(session, page, token)
            if not targets:
                break                       # listing exhausted
            page += 1
            job["collected"] += len(targets)
            if first:
                job["phase"] = "extracting"
                first = False
            if is_all:
                job["total"] = job["collected"]

            for pdf_id, bid_no in targets:
                if not is_all and job["written"] >= target:
                    break
                if pdf_id in seen_ids:
                    continue
                seen_ids.add(pdf_id)

                try:
                    row = process_one(session, bid_no, pdf_id)
                except Exception:
                    row = None

                if row and row["bid_no"] not in seen_bidno:
                    append_row(CUSTOM_OUTPUT_CSV, row)   # active bid → store
                    seen_bidno.add(row["bid_no"])
                    job["written"] += 1
                else:
                    job["failed"] += 1                   # expired/empty → ignore

                time.sleep(1.5)

        job["done"]   = True
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"]  = str(e)
        job["done"]   = True


# ── Endpoint 1: Start ─────────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def start():
    global job

    data  = request.get_json()
    count = data.get("count", "all")

    job = {
        "status":    "running",
        "phase":     "collecting",
        "collected": 0,
        "total":     0,
        "written":   0,
        "failed":    0,
        "done":      False,
        "error":     ""
    }

    thread = threading.Thread(target=run_scraper, args=(count,))
    thread.daemon = True
    thread.start()

    return jsonify({"message": "Job started", "count": count})


# ── Endpoint 2: Status ────────────────────────────────────────────────────────
@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify(job)


# ── Endpoint 3: Download ──────────────────────────────────────────────────────
@app.route("/api/download", methods=["GET"])
def download():
    csv_path = "gemarpts_output.csv"
    if os.path.exists(csv_path):
        return send_file(
            csv_path,
            as_attachment=True,
            download_name="gem_gemarpts_data.csv"
        )
    return jsonify({"error": "CSV not ready yet"}), 404


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
    global compare_results
    progress = _make_progress(my_gen)
    try:
        # ── Phase 1: collect the custom bids FRESH from GeM every run ────────
        #    (no cache/resume — always reflects what is live right now).
        session     = build_session()
        needed      = None if (compare_all or custom_limit is None) else custom_limit
        custom_rows = collect_custom_bids(session, needed, compare_all, progress)
        if not custom_rows:
            raise RuntimeError(
                "No active custom bids could be collected from GeM right now — "
                "nothing to compare. Please try again."
            )

        # ── Phase 2: match the collected custom bids against the categories ──
        results = run_matching_job(
            custom_rows=custom_rows,
            category_limit=category_limit,
            compare_all=compare_all,
            output_csv=COMPARE_CSV,
            progress=progress,
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
    category_limit = None if compare_all else parse_limit(data.get("categoryCount"))

    if not compare_all and (custom_limit is None or category_limit is None):
        return jsonify({
            "error": "Enter a valid number of custom bids and category bids, "
                     "or turn on Compare All."
        }), 400

    # Starting a run always supersedes any previous one (bumping the generation
    # aborts a stale/stuck worker), so the user can never get locked out.
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


# ── Endpoint: Cancel / reset a comparison ─────────────────────────────────────
@app.route("/api/compare/cancel", methods=["POST"])
def compare_cancel():
    """Supersede any running comparison and reset state back to idle."""
    global compare_gen
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


# ── Endpoint 6: Compare result (rows, once done) ──────────────────────────────
@app.route("/api/compare/result", methods=["GET"])
def compare_result():
    with compare_lock:
        if compare_job["status"] != "done":
            return jsonify({"error": "Comparison not finished yet"}), 409
        total  = compare_job["result_total"]
        inline = compare_job["inline"]
        rows   = list(compare_results) if inline else None
    return jsonify({"total": total, "inline": inline, "rows": rows})


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