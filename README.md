# iCloud Media Harvester

> Download every photo and video from any iCloud shared album as a single ZIP file — served through a local web dashboard with a futuristic UI.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=flat-square&logo=flask&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey?style=flat-square)

---

## Screenshots

### Dashboard — Idle
![Dashboard idle state showing the URL input and stats cards](https://placehold.co/900x500/03060f/00e5ff?text=Dashboard+%E2%80%94+Idle+State&font=mono)

### Dashboard — Fetching Album
![Amber slow-connection warning banner while contacting Apple's endpoint](https://placehold.co/900x500/03060f/ffab40?text=Dashboard+%E2%80%94+Fetching+%2F+Slow-Connection+Warning&font=mono)

### Dashboard — Downloading (8 parallel workers)
![Live progress bar and terminal log showing concurrent file downloads](https://placehold.co/900x500/03060f/2979ff?text=Dashboard+%E2%80%94+Downloading+%288+Parallel+Workers%29&font=mono)

### Dashboard — Complete with Failed Files
![Green download button alongside the red failed-files panel with retry option](https://placehold.co/900x500/03060f/00e676?text=Dashboard+%E2%80%94+Complete+%2B+Failed+Files+Panel&font=mono)

### Dashboard — Retry Failed
![Retry job in progress after clicking the Retry Failed Files button](https://placehold.co/900x500/03060f/ffab40?text=Dashboard+%E2%80%94+Retrying+Failed+Files&font=mono)

---

## Features

- **One-click download** — paste a shared album URL, hit INITIATE, get a ZIP
- **8 parallel workers** — files download concurrently via `ThreadPoolExecutor`, ~6–8× faster than sequential
- **Auto-retry with backoff** — each file retried up to 4× (1 s → 2 s → 4 s) on timeout or CDN connection drop
- **Failed files panel** — live red badge counts failures as they happen; full list with error reasons shown on completion
- **One-click retry** — re-downloads only the failed files without re-fetching the full album metadata
- **Best-quality selection** — picks the highest-resolution derivative per photo; prefers `1080p → 720p` for videos, skips poster frames
- **Live progress** — real-time stats (photo count, video count, ZIP size, status) and scrolling terminal log via SSE
- **Slow-connection warning** — amber banner appears while waiting for Apple's endpoint (10-minute timeout)
- **Partition auto-discovery** — follows Apple's `330` redirect to the correct shard (`p01 → p117`, etc.) automatically
- **Duplicate filename handling** — deduplicates filenames inside the ZIP so nothing is silently overwritten
- **No cloud dependency** — runs entirely on your local machine; your iCloud token never leaves your network beyond the Apple API calls

---

## How It Works

The iCloud shared-stream API uses a two-step flow before any downloading begins:

```
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 1 — webstream                                                  │
│                                                                      │
│  POST /sharedstreams/webstream                                       │
│  Body: {"streamCtag": null}                                          │
│                                                                      │
│  ← Returns: album name, list of photos with photoGuid + derivatives  │
│             (each derivative has a checksum, dimensions, fileSize)   │
│                                                                      │
│  Note: if Apple returns HTTP 330, the JSON body contains             │
│  X-Apple-MMe-Host with the correct partition hostname (e.g. p117).  │
│  The request is automatically retried against the correct host.      │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 2 — webasseturls  (batched, 25 GUIDs per request)             │
│                                                                      │
│  POST /sharedstreams/webasseturls                                    │
│  Body: {"photoGuids": ["GUID1", "GUID2", ...]}                       │
│                                                                      │
│  ← Returns: map of checksum → {url_location, url_path, url_expiry}  │
│                                                                      │
│  Full URL = scheme + "://" + url_location + url_path                 │
│  e.g. https://cvws.icloud-content.com/S/ABC.../IMG_2912.JPG?...     │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 3 — Concurrent download + ZIP                                  │
│                                                                      │
│  8 files download simultaneously via ThreadPoolExecutor.             │
│  Each file: pick best derivative → fetch URL (4× retry on failure)  │
│  → write into a shared in-memory ZipFile under a lock.              │
│  Failed files are tracked and surfaced in the UI for one-click retry.│
└──────────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
icloud-downloader/
├── app.py               # Flask backend — iCloud API, concurrent downloader, SSE, ZIP
├── requirements.txt     # flask, requests
├── .gitignore
└── templates/
    └── index.html       # Self-contained futuristic dashboard (no build step)
```

---

## Setup

### Prerequisites

- Python 3.10 or newer
- pip

### 1 — Clone

```bash
git clone https://github.com/your-username/icloud-downloader.git
cd icloud-downloader
```

### 2 — Install dependencies

```bash
pip install -r requirements.txt
```

### 3 — Run

```bash
python app.py
```

Open **[http://localhost:5000](http://localhost:5000)** in your browser.

---

## Usage

1. Open a shared iCloud album and copy the URL from the address bar.  
   It looks like: `https://www.icloud.com/photos/#B1tJtdOXmNT4XNt`

2. Paste it into the dashboard and click **INITIATE**.

3. An amber banner appears while the app contacts Apple's endpoint — this can take **1–3 minutes** on large albums; do not close the tab.

4. Downloading begins automatically with 8 parallel workers. Watch the live terminal log and progress bar.

5. When complete, a glowing **DOWNLOAD ZIP ARCHIVE** button appears. If any files failed, a red **FAILED FILES** panel lists them with error reasons. Click **↺ RETRY FAILED FILES** to re-download only those files without restarting from scratch.

> **Tip:** You can paste just the bare token (`B1tJtdOXmNT4XNt`) instead of the full URL.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, Flask 3.x |
| Concurrent downloads | `concurrent.futures.ThreadPoolExecutor` |
| HTTP client | requests (with exponential-backoff retry) |
| Real-time updates | Server-Sent Events (SSE) |
| Packaging | `zipfile` (stdlib, in-memory) |
| Frontend | Vanilla HTML / CSS / JS — no framework or build step |
| Fonts | Rajdhani + Share Tech Mono (Google Fonts) |

---

## Configuration

All tunables are constants at the top of `app.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `DOWNLOAD_WORKERS` | `8` | Concurrent download threads |
| `timeout` in `_icloud_post` | `600` s | Max wait for Apple's webstream endpoint |
| `timeout` in `_download_file` | `300` s | Per-file download timeout |
| `max_retries` in `_download_file` | `4` | Retry attempts per file before marking as failed |
| batch size in `_fetch_asset_urls` | `25` | GUIDs per `webasseturls` request |
| Flask port | `5000` | Change in the `app.run()` call at the bottom |

---

## Limitations

- **In-memory ZIP** — the full album is buffered in RAM. Very large albums (several GB) may exhaust memory on low-RAM machines.
- **URL expiry** — Apple's asset URLs expire after ~1 hour. If a full download + retry cycle takes longer, re-run from the start to get fresh URLs.
- **Single-process** — the in-memory job store is not shared across workers. Run with `python app.py`, not a multi-process WSGI server.
- **Public albums only** — works only with publicly shared iCloud albums (the `#token` share links). Does not authenticate with Apple ID credentials.

---

## License

MIT — see [LICENSE](LICENSE) for details.
