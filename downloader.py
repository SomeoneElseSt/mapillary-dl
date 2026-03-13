"""Mapillary API client and image downloader for street view imagery."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

import piexif
import mapillary.interface as mly
import requests
from tqdm import tqdm

from config import BoundingBox, MapillaryConfig, GridParams, DATA_DIR, GPS_COORD_PRECISION, GRANULARITY_DEFAULT, granularity_to_grid_params
from database import DiscoveryDB


MAX_RESOLUTION = 2048
API_IMAGE_LIMIT = 2000
DISCOVERY_WORKERS = 30

OPTIONAL_FIELDS = {
    'altitude': 'altitude',
    'camera_type': 'camera_type',
    'creator': 'creator',
    'height': 'image_height',
    'width': 'image_width',
}


def dms_to_deg(dms: tuple) -> float:
    d, m, s = dms
    return d[0] / d[1] + m[0] / m[1] / 60 + s[0] / s[1] / 3600


def read_gps_exif(path: Path) -> "tuple[float, float] | None":
    """Read GPS lat/lon from JPEG EXIF, or None if not present."""
    try:
        exif = piexif.load(str(path))
        gps = exif.get("GPS", {})
        lat_dms = gps.get(piexif.GPSIFD.GPSLatitude)
        lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
        lon_dms = gps.get(piexif.GPSIFD.GPSLongitude)
        lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)
        if not all([lat_dms, lat_ref, lon_dms, lon_ref]):
            return None
        lat = dms_to_deg(lat_dms) * (1 if lat_ref in (b"N", "N") else -1)
        lon = dms_to_deg(lon_dms) * (1 if lon_ref in (b"E", "E") else -1)
        return lat, lon
    except Exception:
        return None


def embed_gps_exif(path: Path, lat: float, lon: float) -> None:
    """Write GPS coordinates into JPEG EXIF in-place without re-encoding."""
    def to_rational(deg: float) -> tuple:
        # Store as decimal degrees rational: (round(deg * precision), precision)
        # This normalizes to GPS_COORD_PRECISION so DB and EXIF values are exactly equal on read
        return ((round(abs(deg) * GPS_COORD_PRECISION), GPS_COORD_PRECISION), (0, 1), (0, 1))

    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: to_rational(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: to_rational(lon),
    }
    try:
        exif_data = piexif.load(str(path))
    except Exception:
        exif_data = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
    exif_data["GPS"] = gps_ifd
    exif_bytes = piexif.dump(exif_data)
    piexif.insert(exif_bytes, str(path))


def extract_lat_lon(img: Dict) -> tuple[float, float] | None:
    """Extract (lat, lon) from either DB format {lat, lon} or API format {geometry.coordinates}."""
    if "lat" in img:
        return img["lat"], img["lon"]
    coords = img.get("geometry", {}).get("coordinates", [])
    if len(coords) >= 2:
        return coords[1], coords[0]
    return None


class MapillaryClient:
    """Client for interacting with Mapillary API."""

    BASE_URL = "https://graph.mapillary.com"

    def __init__(self, config: MapillaryConfig):
        self.config = config
        mly.set_access_token(config.client_token)
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {config.client_token}"})

    def get_images_in_bbox(
        self,
        bbox: BoundingBox,
        limit: int = 1000,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> List[Dict]:
        """Get images within a bounding box."""
        url = f"{self.BASE_URL}/images"
        params = {
            "bbox": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}",
            "limit": limit,
            "fields": "id,geometry,captured_at,compass_angle,sequence,is_pano,altitude,camera_type,creator,height,width"
        }

        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time

        response = self.session.get(url, params=params)
        if response.status_code != 200:
            return []

        return response.json().get("data", [])

    def get_image_metadata(self, image_id: str) -> Optional[Dict]:
        """Get detailed metadata for a specific image."""
        url = f"{self.BASE_URL}/{image_id}"
        params = {
            "fields": "id,geometry,captured_at,compass_angle,sequence,is_pano,altitude,camera_type,creator,height,width,thumb_256_url,thumb_1024_url,thumb_2048_url"
        }

        response = self.session.get(url, params=params)
        if response.status_code != 200:
            return None

        return response.json()

    def download_image(self, image_id: str, output_path: Path, resolution: int = MAX_RESOLUTION) -> bool:
        """Download an image at specified resolution (256, 1024, or 2048)."""
        if resolution not in [256, 1024, 2048]:
            return False

        metadata = self.get_image_metadata(image_id)
        if not metadata:
            return False

        thumb_url = metadata.get(f"thumb_{resolution}_url")
        if not thumb_url:
            return False

        response = self.session.get(thumb_url, stream=True)
        if response.status_code != 200:
            return False

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return True

    def get_coverage_stats(self, bbox: BoundingBox) -> Dict:
        """Get statistics about image coverage in a bounding box."""
        images = self.get_images_in_bbox(bbox, limit=10000)
        total_images = len(images)
        pano_count = sum(1 for img in images if img.get("is_pano"))
        sequences = set(img.get("sequence") for img in images if img.get("sequence"))

        return {
            "total_images": total_images,
            "panoramic_images": pano_count,
            "regular_images": total_images - pano_count,
            "unique_sequences": len(sequences),
            "bbox": bbox.to_tuple()
        }


class ImageDownloader:
    """Downloads Mapillary images with progress tracking."""

    def __init__(self, client: MapillaryClient, output_dir: Path = DATA_DIR, grid_params: GridParams = None):
        self.client = client
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.grid = grid_params or granularity_to_grid_params(GRANULARITY_DEFAULT)

    def _split_cell(self, cell: BoundingBox) -> List[BoundingBox]:
        """Split a cell into 4 equal quadrants."""
        mid_lon = (cell.west + cell.east) / 2
        mid_lat = (cell.south + cell.north) / 2
        return [
            BoundingBox(cell.west, cell.south, mid_lon, mid_lat),
            BoundingBox(mid_lon, cell.south, cell.east, mid_lat),
            BoundingBox(cell.west, mid_lat, mid_lon, cell.north),
            BoundingBox(mid_lon, mid_lat, cell.east, cell.north),
        ]

    def _fetch_cell_images(self, cell: BoundingBox) -> List[Dict]:
        """Fetch images for a cell, recursively splitting if the API limit is hit.

        Stops recursing at MIN_CELL_SIZE
        """
        images = self.client.get_images_in_bbox(cell, limit=API_IMAGE_LIMIT)
        cell_size = min(cell.east - cell.west, cell.north - cell.south)
        if len(images) < API_IMAGE_LIMIT or cell_size <= self.grid.min_cell_size:
            return images
        all_images = []
        for sub_cell in self._split_cell(cell):
            all_images.extend(self._fetch_cell_images(sub_cell))
        return all_images

    def split_bbox_into_grid(self, bbox: BoundingBox) -> List[BoundingBox]:
        """Split large bounding box into smaller grid cells."""
        cells = []
        cell_size = self.grid.grid_cell_size
        lon_cells = int((bbox.east - bbox.west) / cell_size) + 1
        lat_cells = int((bbox.north - bbox.south) / cell_size) + 1

        for i in range(lon_cells):
            for j in range(lat_cells):
                cell_west = bbox.west + (i * cell_size)
                cell_east = min(cell_west + cell_size, bbox.east)
                cell_south = bbox.south + (j * cell_size)
                cell_north = min(cell_south + cell_size, bbox.north)

                cells.append(BoundingBox(
                    west=cell_west,
                    south=cell_south,
                    east=cell_east,
                    north=cell_north
                ))

        return cells

    def discover_images(self, bbox: BoundingBox, db: Optional["DiscoveryDB"] = None) -> List[Dict]:
        """Discover all available images in bounding box.

        If db is provided, inserts images into the DB as each cell completes so
        progress is preserved on Ctrl+C.
        """
        print(f"\n🔍 Discovering images in area...")
        print(f"   Bbox: {bbox.to_tuple()}")

        cells = self.split_bbox_into_grid(bbox)
        update_interval = max(1, len(cells) // 100)
        print(f"   Searching {len(cells)} grid cells...")
        print(f"   Time estimates refresh every {update_interval} cells")

        all_images = []
        seen_ids = set()
        completed = 0

        with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as executor:
            futures = {executor.submit(self._fetch_cell_images, cell): cell for cell in cells}
            with tqdm(total=len(cells), desc="Discovering", unit="cell") as pbar:
                for future in as_completed(futures):
                    cell_images = future.result() or []
                    new_images = []
                    for img in cell_images:
                        img_id = img.get('id')
                        if img_id and img_id not in seen_ids:
                            all_images.append(img)
                            new_images.append(img)
                            seen_ids.add(img_id)
                    if db and new_images:
                        db.insert_images(new_images)
                    completed += 1
                    pbar.set_postfix({"found": f"{len(all_images):,}"})
                    if completed % update_interval == 0:
                        pbar.update(update_interval)
                pbar.update(pbar.total - pbar.n)

        print(f"\n✓ Found {len(all_images)} unique images")
        return all_images

    def reconcile_disk_images(self, images: List[Dict], db: DiscoveryDB) -> List[Dict]:
        """Mark images already on disk as downloaded in DB. Returns images not yet on disk.

        First reconciles the pending list against disk, then scans for any
        orphaned files on disk that exist in the DB but weren't in the pending list.
        """
        pending_ids = set()
        remaining = []
        for img in images:
            img_id = img.get('id')
            if not img_id:
                continue
            pending_ids.add(img_id)
            output_path = self.output_dir / f"{img_id}.jpg"
            if not output_path.exists():
                remaining.append(img)
                continue
            lat_lon = extract_lat_lon(img)
            if lat_lon:
                if read_gps_exif(output_path) is None:
                    embed_gps_exif(output_path, *lat_lon)
                db.upsert_downloaded(img_id, *lat_lon)
            else:
                output_path.unlink()
                remaining.append(img)

        # Reconcile orphaned disk files that are in the DB but weren't in the pending list
        for jpg in self.output_dir.glob("*.jpg"):
            img_id = jpg.stem
            if img_id in pending_ids:
                continue
            gps = read_gps_exif(jpg)
            if gps:
                db.upsert_downloaded(img_id, *gps)

        return remaining

    def download_images(
        self,
        bbox: BoundingBox,
        db: DiscoveryDB,
        max_images: int = None,
        images: List[Dict] = None,
    ) -> Dict[str, int]:
        """Download images. Pass `images` to skip rediscovery. Uses db for tracking."""
        downloaded_ids = db.get_downloaded_ids()
        all_images = images if images is not None else self.discover_images(bbox)
        total_images_in_db = db.get_image_count()
        already_had_at_start = total_images_in_db - db.get_pending_count()

        if not all_images:
            print("\n❌ No images found in this area")
            return {'total_found': 0, 'downloaded': 0, 'skipped': 0, 'failed': 0}

        images_to_download = [img for img in all_images if img.get('id') not in downloaded_ids]
        skipped_count = len(all_images) - len(images_to_download)

        if max_images and len(images_to_download) > max_images:
            images_to_download = images_to_download[:max_images]
            print(f"\n⚠️  Limiting download to {max_images} images")

        if not images_to_download:
            print(f"\n✓ All {len(all_images)} images already downloaded!")
            return {
                'total_found': len(all_images),
                'downloaded': 0,
                'skipped': skipped_count,
                'failed': 0
            }

        print(f"\n📥 Downloading {len(images_to_download)} images...")
        print(f"   Resolution: {MAX_RESOLUTION}px")
        print(f"   Output: {self.output_dir}")

        failed_count = 0
        success_count = 0
        completed = 0
        update_interval = max(1, len(images_to_download) // 100)
        print(f"   Time estimates refresh every {update_interval} downloaded images")

        with tqdm(total=len(images_to_download), desc="Downloading", unit="img") as pbar:
            for img in images_to_download:
                img_id = img.get('id')
                if not img_id:
                    continue

                output_path = self.output_dir / f"{img_id}.jpg"
                if output_path.exists():
                    lat_lon = extract_lat_lon(img)
                    if lat_lon:
                        # Embed GPS if missing — use img coords (full precision, not EXIF round-trip)
                        if read_gps_exif(output_path) is None:
                            embed_gps_exif(output_path, *lat_lon)
                        db.upsert_downloaded(img_id, *lat_lon)
                        skipped_count += 1
                        completed += 1
                        continue
                    else:
                        # No coords anywhere — delete so it gets re-downloaded fresh
                        output_path.unlink()

                success = self.client.download_image(
                    image_id=img_id,
                    output_path=output_path,
                    resolution=MAX_RESOLUTION
                )

                if success:
                    lat_lon = extract_lat_lon(img)
                    if lat_lon:
                        embed_gps_exif(output_path, *lat_lon)
                    db.mark_downloaded(img_id)
                    success_count += 1
                else:
                    failed_count += 1

                completed += 1
                if completed % update_interval == 0:
                    pbar.update(update_interval)
            pbar.update(pbar.total - pbar.n)

        total_downloaded = len(db.get_downloaded_ids())
        
        print("\n📋 Download Summary:")
        print(f"  {'Discovered Images:':<22} {total_images_in_db:,}")
        print(f"  {'Existing Downloads:':<22} {already_had_at_start:,}")
        print(f"  {'New Downloads:':<22} {success_count:,}")
        print(f"  {'Failed Downloads:':<22} {failed_count:,}")
        print(f"  {'Total Downloads:':<22} {total_downloaded:,}\n")

        return {
            'total_found': len(all_images),
            'downloaded': success_count,
            'skipped': skipped_count,
            'failed': failed_count
        }
