from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import threading
import os
import time

from gem_gemarpts_scraper import (
    process_one, build_session, append_row,
    load_processed_bids, prime_session, fetch_listing_page
)

app = Flask(__name__)
CORS(app)

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
        session  = build_session()
        is_all   = (count == "all")
        target   = None if is_all else int(count)

        # ── Phase 1: Collect fresh active IDs from GeM listing ────────────────
        job["phase"] = "collecting"

        token         = prime_session(session)
        collected_ids = []
        page          = 1

        while True:
            if not is_all and len(collected_ids) >= target:
                break

            ids = fetch_listing_page(session, page, token)

            if not ids:
                break                       # no more pages

            collected_ids.extend(ids)
            job["collected"] = len(collected_ids)
            page += 1
            time.sleep(1.5)

        # Trim to exact count
        if not is_all:
            collected_ids = collected_ids[:target]

        # ── Phase 2: Extract GeMARPTS data from each ID ───────────────────────
        job["phase"] = "extracting"
        job["total"] = len(collected_ids)

        done_set = load_processed_bids("gemarpts_output.csv")

        for doc_id in collected_ids:
            try:
                row = process_one(session, "", doc_id)
                if row and row["bid_no"] not in done_set:
                    append_row("gemarpts_output.csv", row)
                    done_set.add(row["bid_no"])
                    job["written"] += 1
                else:
                    job["failed"] += 1
            except Exception as e:
                job["failed"] += 1

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


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)