# Mapillary Bulk Downloader

A CLI tool for downloading street-level imagery from [Mapillary](https://www.mapillary.com/) at city scale. Define a bounding box, discover every available image inside it, and download them all — with GPS coordinates embedded in EXIF, resumable downloads, and a SQLite-backed discovery cache that makes re-runs instant.

Built to collect training data for 3D city reconstruction (COLMAP + Gaussian Splatting), where you need tens of thousands of geo-tagged street photos covering contiguous areas.

## What it does

1. **Discover** — Splits a bounding box into a grid, queries every cell in parallel (30 workers), and recursively subdivides cells that hit the API limit. Finds every image Mapillary has in the area.
2. **Cache** — Stores all discovered image IDs and coordinates in a local SQLite database (`images.db`). Subsequent runs skip the API entirely unless you ask to re-discover.
3. **Download** — Pulls images at 2048px resolution with progress bars. Embeds GPS lat/lon into JPEG EXIF so each file is self-contained. Tracks what's been downloaded with atomic SQLite writes, so you can interrupt and resume at any time.

## Quick start

```bash
# Install dependencies
uv sync

# Set your Mapillary API token
echo 'MAPILLARY_CLIENT_TOKEN=MLY|...' > .env

# Interactive mode — pick a city, preview the area on a map, then download
uv run python3 cli.py

# Or go headless
uv run python3 cli.py --city "San Francisco"
uv run python3 cli.py --bbox "-122.52,37.70,-122.35,37.83" --limit 500
```

## Usage

```
uv run python3 cli.py [OPTIONS]

Options:
  --city NAME           Download from a predefined city
  --bbox W,S,E,N        Custom bounding box (overrides --city)
  --limit N             Cap the number of images to download
  --output-dir PATH     Output directory (default: data/<city>)
  --preview             Open an interactive map in the browser before downloading
  --state STATE         Discovery state when resuming: maintain | merge | rediscover
  --no-save-discovery   Don't persist discovered IDs to the database
  --list-cities         Show predefined cities and exit
```

**No arguments** launches interactive mode: arrow-key city selection, optional map preview via [Folium](https://python-visualization.github.io/folium/), discovery summary, and a confirmation prompt before downloading.

### Discovery states

When an `images.db` already exists for a city:

| State | Behavior |
|-------|----------|
| `maintain` | Load from DB, skip API calls (default) |
| `merge` | Re-discover and add any new images to the existing DB |
| `rediscover` | Wipe the DB and run a full fresh discovery |

## Architecture

```
cli.py          — CLI entry point: argparse, interactive prompts, map preview
downloader.py   — MapillaryClient (API) + ImageDownloader (grid split, parallel discovery, download loop)
database.py     — DiscoveryDB: SQLite cache with singleton pattern, tracks discovered/downloaded state
config.py       — Dataclasses (MapillaryConfig, BoundingBox), env loading, predefined city bounding boxes
scripts/        — Standalone utilities (GPS coordinate enrichment)
```

## Key design decisions

- **Adaptive grid splitting**: The API caps results at 2,000 per query. Dense urban areas easily exceed that. The downloader starts with coarse grid cells and recursively subdivides any cell that saturates the limit, down to a minimum cell size. This guarantees complete coverage without manual tuning.
- **SQLite over JSON**: Early versions used `download_metadata.json`. Switched to SQLite for atomic writes (no corruption on Ctrl+C), fast set-membership lookups on 100k+ image IDs, and clean separation of discovery vs. download state.
- **GPS in EXIF**: Coordinates are embedded directly into each JPEG at download time. This means images work standalone — no sidecar files, no separate metadata lookup. Precision is normalized to 7 decimal places (~1 cm) so DB and EXIF values match exactly.
- **Disk reconciliation**: On resume, the downloader checks what's actually on disk (not just what the DB says) and reconciles the two. Images on disk missing GPS get coordinates embedded; images in the DB but missing from disk get re-queued.
