import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests
from flask import Flask, Response, jsonify, render_template, stream_with_context, request

app = Flask(__name__)

# In-memory job store (single-process / demo use)
_jobs = {}

DOWNLOAD_WORKERS = 8   # concurrent file downloads

_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
    "Connection": "keep-alive",
    "Content-Type": "text/plain",
    "Origin": "https://www.icloud.com",
    "Referer": "https://www.icloud.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
_COOKIES = {"x-apple-group": "false"}


# ──────────────────────────────────────────────
# iCloud API helpers
# ──────────────────────────────────────────────

def _extract_token(raw):
    raw = raw.strip()
    m = re.search(r"#([A-Za-z0-9_\-]+)$", raw)
    if m:
        return m.group(1)
    m = re.search(r"/photos/([A-Za-z0-9_\-]{10,})/?$", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", raw):
        return raw
    return None


def _icloud_post(url, payload):
    return requests.post(
        url, headers=_HEADERS, cookies=_COOKIES, data=payload, timeout=600,
    )


def _fetch_photos(token):
    base = f"https://p01-sharedstreams.icloud.com/{token}/sharedstreams"
    payload = json.dumps({"streamCtag": None})

    resp = _icloud_post(f"{base}/webstream", payload)

    if resp.status_code == 330:
        redir_host = resp.json().get("X-Apple-MMe-Host", "")
        if not redir_host:
            raise RuntimeError("330 redirect but no host in response body")
        base = f"https://{redir_host}/{token}/sharedstreams"
        resp = _icloud_post(f"{base}/webstream", payload)

    if resp.status_code != 200:
        raise RuntimeError(f"webstream HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    return base, body.get("photos", []), body.get("streamName", "icloud_album")


def _fetch_asset_urls(base, guids):
    url_map = {}
    for i in range(0, len(guids), 25):
        chunk = guids[i: i + 25]
        resp = _icloud_post(f"{base}/webasseturls", json.dumps({"photoGuids": chunk}))
        if resp.status_code != 200:
            continue
        body = resp.json()
        locs = body.get("locations", {})
        for checksum, item in body.get("items", {}).items():
            loc = item.get("url_location", "")
            scheme = locs.get(loc, {}).get("scheme", "https")
            url_map[checksum] = f"{scheme}://{loc}{item.get('url_path', '')}"
    return url_map


def _best_derivative(photo):
    derivs = photo.get("derivatives", {})
    mtype = photo.get("mediaAssetType", "photo")
    if not derivs:
        return None, None
    if mtype == "video":
        for q in ("1080p", "720p", "480p", "360p", "240p"):
            if q in derivs:
                return derivs[q]["checksum"], ".mp4"
        best_cs, best_sz = None, 0
        for k, d in derivs.items():
            if k != "PosterFrame":
                sz = int(d.get("fileSize", 0))
                if sz > best_sz:
                    best_sz, best_cs = sz, d["checksum"]
        return best_cs, ".mp4"
    best_cs, best_sz = None, 0
    for d in derivs.values():
        sz = int(d.get("fileSize", 0))
        if sz > best_sz:
            best_sz, best_cs = sz, d["checksum"]
    return best_cs, ".jpg"


def _filename_from_url(url, fallback_ext):
    path = url.split("?")[0].split("/")[-1]
    name = unquote(path) if path else ""
    return name if name else f"file{fallback_ext}"


# ──────────────────────────────────────────────
# Download engine
# ──────────────────────────────────────────────

def _download_file(url, max_retries=4, timeout=300):
    """Fetch one file with exponential-backoff retry on timeout / connection drop."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, timeout=timeout, stream=True)
            if r.status_code == 200:
                return r.content
            raise requests.HTTPError(f"HTTP {r.status_code}")
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)   # 1 s, 2 s, 4 s
    raise last_exc


def _download_to_zip(downloads, job, log_fn):
    """
    Download all items with DOWNLOAD_WORKERS concurrent threads and pack into a ZIP.
    Updates job["failed"] in real time. Returns (zip_bytes, total_bytes).
    """
    buf = io.BytesIO()
    seen = {}
    state = {"done": 0, "bytes": 0}
    lock = threading.Lock()
    zip_lock = threading.Lock()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:

        def fetch_one(item):
            content = _download_file(item["url"])   # raises on failure
            with zip_lock:
                fname = item["filename"]
                if fname in seen:
                    seen[fname] += 1
                    base_n, ext_n = os.path.splitext(fname)
                    fname = f"{base_n}_{seen[fname]}{ext_n}"
                else:
                    seen[fname] = 0
                zf.writestr(fname, content)
            return len(content)

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            future_map = {pool.submit(fetch_one, item): item for item in downloads}
            for fut in as_completed(future_map):
                item = future_map[fut]
                try:
                    nbytes = fut.result()
                    with lock:
                        state["done"] += 1
                        state["bytes"] += nbytes
                        log_fn(
                            f"[{state['done']}/{len(downloads)}] ✓ {item['filename']}",
                            progress=state["done"],
                        )
                except Exception as exc:
                    err = str(exc)
                    if len(err) > 100:
                        err = err[:100] + "…"
                    with lock:
                        state["done"] += 1
                        job["failed"].append({
                            "filename": item["filename"],
                            "url": item["url"],
                            "error": err,
                        })
                        log_fn(f"  ⚠  Skipped {item['filename']}: {err}")

    buf.seek(0)
    return buf.getvalue(), state["bytes"]


# ──────────────────────────────────────────────
# Job workers
# ──────────────────────────────────────────────

def _blank_job(stream_name="", total=0, photo_count=0, video_count=0):
    return {
        "status": "starting",
        "progress": 0,
        "total": total,
        "log": [],
        "zip_data": None,
        "zip_size": 0,
        "total_bytes": 0,
        "error": None,
        "stream_name": stream_name,
        "photo_count": photo_count,
        "video_count": video_count,
        "failed": [],
    }


def _finish_job(job, zip_bytes, total_bytes, n_total, log_fn):
    job["zip_data"] = zip_bytes
    job["zip_size"] = len(zip_bytes)
    job["total_bytes"] = total_bytes
    job["status"] = "done"
    n_fail = len(job["failed"])
    fail_note = f" — {n_fail} file(s) failed" if n_fail else ""
    log_fn(
        f"Complete — ZIP is {len(zip_bytes) // 1024} KB{fail_note}",
        progress=n_total,
    )


def _run_job(job_id, token):
    job = _jobs[job_id]

    def log(msg, progress=None):
        job["log"].append(msg)
        if progress is not None:
            job["progress"] = progress

    try:
        job["status"] = "fetching"
        log("Connecting to iCloud shared stream …")

        base, photos, stream_name = _fetch_photos(token)
        job["stream_name"] = stream_name

        n_photo = sum(1 for p in photos if p.get("mediaAssetType") != "video")
        n_video = sum(1 for p in photos if p.get("mediaAssetType") == "video")
        job.update({"photo_count": n_photo, "video_count": n_video})
        log(f'Album: "{stream_name}" — {len(photos)} items ({n_photo} photos, {n_video} videos)')

        log("Resolving asset download URLs …")
        guids = [p["photoGuid"] for p in photos if "photoGuid" in p]
        url_map = _fetch_asset_urls(base, guids)
        log(f"Resolved {len(url_map)} asset URLs")

        downloads = []
        for photo in photos:
            cs, ext = _best_derivative(photo)
            if cs and cs in url_map:
                downloads.append({
                    "url": url_map[cs],
                    "filename": _filename_from_url(url_map[cs], ext or ".bin"),
                })

        skipped = len(photos) - len(downloads)
        if skipped:
            log(f"⚠  {skipped} items had no resolvable URL")

        job["status"] = "downloading"
        job["total"] = len(downloads)
        log(f"Downloading {len(downloads)} files with {DOWNLOAD_WORKERS} parallel workers …")

        zip_bytes, total_bytes = _download_to_zip(downloads, job, log)
        _finish_job(job, zip_bytes, total_bytes, len(downloads), log)

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        job["log"].append(f"FATAL: {exc}")


def _run_retry_job(job_id, failed_items):
    job = _jobs[job_id]

    def log(msg, progress=None):
        job["log"].append(msg)
        if progress is not None:
            job["progress"] = progress

    try:
        job["status"] = "downloading"
        log(f"Retrying {len(failed_items)} failed file(s) with {DOWNLOAD_WORKERS} workers …")

        zip_bytes, total_bytes = _download_to_zip(failed_items, job, log)
        _finish_job(job, zip_bytes, total_bytes, len(failed_items), log)

    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        job["log"].append(f"FATAL: {exc}")


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(force=True, silent=True) or {}
    token = _extract_token(body.get("url", ""))
    if not token:
        return jsonify({"error": "Invalid URL. Expected: https://www.icloud.com/photos/#TOKEN"}), 400

    job_id = str(uuid.uuid4())
    _jobs[job_id] = _blank_job()
    threading.Thread(target=_run_job, args=(job_id, token), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/retry/<job_id>", methods=["POST"])
def api_retry(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    failed = job.get("failed", [])
    if not failed:
        return jsonify({"error": "No failed items to retry"}), 400

    n_vid = sum(1 for f in failed if f["filename"].lower().endswith((".mp4", ".mov")))
    new_id = str(uuid.uuid4())
    _jobs[new_id] = _blank_job(
        stream_name=job.get("stream_name", "") + " (retry)",
        total=len(failed),
        photo_count=len(failed) - n_vid,
        video_count=n_vid,
    )
    threading.Thread(target=_run_retry_job, args=(new_id, failed), daemon=True).start()
    return jsonify({"job_id": new_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    if job_id not in _jobs:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        cursor = 0
        while True:
            job = _jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'gone'})}\n\n"
                return
            new_logs = job["log"][cursor:]
            cursor = len(job["log"])
            yield "data: " + json.dumps({
                "status":      job["status"],
                "progress":    job["progress"],
                "total":       job["total"],
                "logs":        new_logs,
                "stream_name": job["stream_name"],
                "photo_count": job["photo_count"],
                "video_count": job["video_count"],
                "zip_size":    job["zip_size"],
                "failed_count": len(job["failed"]),
                "error":       job.get("error"),
            }) + "\n\n"
            if job["status"] in ("done", "error"):
                return
            time.sleep(0.4)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/failed/<job_id>")
def api_failed(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"failed": job.get("failed", [])})


@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done" or not job["zip_data"]:
        return jsonify({"error": "ZIP not ready"}), 400
    safe_name = re.sub(r"[^\w\-]", "_", job["stream_name"]) or "icloud_album"
    return Response(
        job["zip_data"],
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.zip"',
            "Content-Length": str(len(job["zip_data"])),
        },
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
