#!/usr/bin/env python3
"""Convert ReefScan session BMPs to EXIF-tagged JPEGs in place.

Standalone version of the storage API JPEG export
(``reefscan/services/storage_api/export/jpeg_exporter.py``). Requires Pillow and Rich.

Each session folder must contain ``photo_log.csv`` and the ``*.bmp`` frames it
references. Each JPEG is written next to its BMP with the same stem. EXIF
metadata (capture time, GPS, depth, temperature, frame number, exposure and the
full CSV row as JSON in UserComment) is taken from the matching CSV row of that
same folder's photo log. Multiple folders are processed one at a time and never
share photo log rows, outputs or raw-data folders. BMP vs JPEG disk usage is
reported per folder and in total.

Examples:
    python convert_bmp_to_jpeg.py D:/reefscan/session_folder
    python convert_bmp_to_jpeg.py D:/reefscan/session_a D:/reefscan/session_b --move-raw --workers 4
    python convert_bmp_to_jpeg.py D:/reefscan/session_folder --delete-raw
    python convert_bmp_to_jpeg.py D:/reefscan --recursive --dry-run
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
import csv
from datetime import datetime
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import time

from PIL import ExifTags, Image
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column

PHOTO_LOG_NAME = "photo_log.csv"
PHOTO_LOG_JPG_NAME = "photo_log_jpg.csv"
RAW_SUBFOLDER = "_raw_data_to_delete"
MAX_EXIF_INTEGER = 2**32 - 1
MISSING = (None, "", "N/A")


def progress_display() -> Progress:
    """Keep long-running stages visibly active, including before totals are known."""
    return Progress(
        SpinnerColumn(),
        TextColumn(
            "{task.description}\n{task.fields[detail]}",
            markup=False,
            table_column=Column(ratio=1, no_wrap=False, overflow="fold"),
        ),
        BarColumn(bar_width=16),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=Console(stderr=True),
        expand=True,
    )


# --------------------------------------------------------------------------- EXIF helpers


def parse_timestamp(value: str | None) -> tuple[str, str, str] | None:
    """Return (``YYYY:MM:DD HH:MM:SS``, subseconds, UTC offset) or None.

    Accepts ISO-8601 (``2026-10-07T23:20:44.513+00:00``) and the ReefScan
    compact form (``20261007_232044_513``).
    """
    if value in MISSING:
        return None
    try:
        if "T" in value or "-" in value[:8]:
            parsed = datetime.fromisoformat(value)
            subseconds = f"{parsed.microsecond // 1000:03d}"
            offset = ""
            utc_offset = parsed.utcoffset()
            if utc_offset is not None:
                total_minutes = int(utc_offset.total_seconds() // 60)
                sign = "+" if total_minutes >= 0 else "-"
                hours, minutes = divmod(abs(total_minutes), 60)
                offset = f"{sign}{hours:02d}:{minutes:02d}"
            return parsed.strftime("%Y:%m:%d %H:%M:%S"), subseconds, offset
        parts = value.split("_")
        if len(parts) < 2:
            return None
        parsed = datetime.strptime(f"{parts[0]}{parts[1]}", "%Y%m%d%H%M%S")
        subseconds = parts[2] if len(parts) > 2 else "000"
        return parsed.strftime("%Y:%m:%d %H:%M:%S"), subseconds, ""
    except ValueError:
        return None


def decimal_to_gps(decimal_value: float, axis: str) -> tuple[str, list[Fraction]]:
    limit = 90.0 if axis == "latitude" else 180.0
    if not math.isfinite(decimal_value) or abs(decimal_value) > limit:
        raise ValueError(f"Invalid {axis} {decimal_value!r}")
    if axis == "latitude":
        ref = "N" if decimal_value >= 0 else "S"
    else:
        ref = "E" if decimal_value >= 0 else "W"
    absolute = abs(decimal_value)
    degrees = int(absolute)
    minutes_float = (absolute - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return ref, [Fraction(degrees), Fraction(minutes), Fraction(seconds).limit_denominator(1_000_000)]


def to_fraction(value: object) -> Fraction | None:
    if value in MISSING:
        return None
    try:
        return Fraction(str(value)).limit_denominator(1_000_000)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def to_rational(value: object) -> tuple[int, int] | None:
    fraction = to_fraction(value)
    if fraction is None or abs(fraction.numerator) > MAX_EXIF_INTEGER or fraction.denominator > MAX_EXIF_INTEGER:
        return None
    return fraction.numerator, fraction.denominator


def to_uint(value: object) -> int | None:
    fraction = to_fraction(value)
    if fraction is None or fraction.denominator != 1 or not 0 <= fraction <= MAX_EXIF_INTEGER:
        return None
    return int(fraction)


def build_exif(row: dict[str, str], source_filename: str) -> Image.Exif:
    exif = Image.Exif()
    # Capture-related tags belong in the Exif sub-IFD; IFD0 holds only image-level tags.
    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)

    # Prefer the CSV timestamp; fall back to the image's own capture time.
    stamp = parse_timestamp(row.get("timestamp")) or parse_timestamp(row.get("image_time"))
    if stamp:
        date_time, subseconds, offset = stamp
        exif[ExifTags.Base.DateTime] = date_time
        exif_ifd[ExifTags.Base.DateTimeOriginal] = date_time
        exif_ifd[ExifTags.Base.DateTimeDigitized] = date_time
        exif_ifd[ExifTags.Base.SubsecTimeOriginal] = subseconds
        exif_ifd[ExifTags.Base.SubsecTimeDigitized] = subseconds
        if offset:
            exif_ifd[ExifTags.Base.OffsetTimeOriginal] = offset
            exif_ifd[ExifTags.Base.OffsetTimeDigitized] = offset

    exif[ExifTags.Base.ImageDescription] = source_filename

    frame_number = to_uint(row.get("frame_id"))
    if frame_number is not None:
        exif[ExifTags.Base.ImageNumber] = frame_number

    depth = to_rational(row.get("pressure_depth"))
    if depth is not None:
        exif_ifd[ExifTags.Base.WaterDepth] = depth

    temperature = to_rational(row.get("water_temperature"))
    if temperature is not None:
        exif_ifd[ExifTags.Base.AmbientTemperature] = temperature

    # Direct-write (VimbaX) frames log exposure in microseconds.
    if "_Frame_" in source_filename:
        exposure = to_fraction(row.get("exposure"))
        if exposure is not None and exposure > 0:
            seconds = exposure / 1_000_000
            exif_ifd[ExifTags.Base.ExposureTime] = (seconds.numerator, seconds.denominator)

    latitude, longitude = row.get("latitude"), row.get("longitude")
    if latitude not in MISSING and longitude not in MISSING:
        try:
            lat_ref, lat = decimal_to_gps(float(latitude), "latitude")
            lon_ref, lon = decimal_to_gps(float(longitude), "longitude")
        except (OverflowError, TypeError, ValueError):
            pass
        else:
            gps_ifd: dict[int, object] = {
                ExifTags.GPS.GPSLatitudeRef: lat_ref,
                ExifTags.GPS.GPSLatitude: lat,
                ExifTags.GPS.GPSLongitudeRef: lon_ref,
                ExifTags.GPS.GPSLongitude: lon,
            }
            if stamp:
                gps_ifd[ExifTags.GPS.GPSDateStamp] = stamp[0][:10]
            exif[ExifTags.IFD.GPSInfo] = gps_ifd

    payload = {key: value for key, value in row.items() if key != "filename_string"}
    payload["source_filename_string"] = source_filename
    # UserComment requires an 8-byte character-code prefix per the EXIF spec.
    exif_ifd[ExifTags.Base.UserComment] = b"ASCII\x00\x00\x00" + json.dumps(payload, sort_keys=True).encode("ascii")
    return exif


# --------------------------------------------------------------------------- conversion


def convert_one(
    bmp_path: str, row: dict[str, str] | None, quality: int, overwrite: bool
) -> tuple[str, str, str, int, int]:
    """Convert one BMP.

    Returns (bmp_path, status, message, bmp_bytes, jpeg_bytes); status is converted|skipped|error.
    Byte counts are 0 on error.
    """
    source = Path(bmp_path)
    target = source.with_suffix(".jpg")
    if target.exists() and not overwrite:
        try:
            return bmp_path, "skipped", "JPEG already exists", source.stat().st_size, target.stat().st_size
        except OSError:
            return bmp_path, "skipped", "JPEG already exists", 0, 0

    # Write to a temp file then rename, so an interrupted run never leaves a truncated .jpg.
    temp_target = target.with_name(target.name + ".part")
    try:
        with Image.open(source) as image:
            rgb = image.convert("RGB")
            exif = build_exif(row or {}, source.name)
            rgb.save(temp_target, format="JPEG", quality=quality, exif=exif)
        os.replace(temp_target, target)
    except Exception as exc:  # noqa: BLE001 - report per-file failure and continue with the batch
        temp_target.unlink(missing_ok=True)
        return bmp_path, "error", f"{type(exc).__name__}: {exc}", 0, 0
    except BaseException:
        temp_target.unlink(missing_ok=True)
        raise
    note = "" if row else "no photo_log row; EXIF limited to filename"
    return bmp_path, "converted", note, source.stat().st_size, target.stat().st_size


def ignore_sigint() -> None:
    """Worker initializer: Ctrl+C is handled by the main process, which lets in-flight images finish cleanly."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def remove_partial_files(folder: Path) -> int:
    """Delete leftover ``.part`` temp files from an earlier interrupted run in this folder only."""
    removed = 0
    for entry in folder.iterdir():
        if entry.is_file() and (entry.name.lower().endswith(".jpg.part") or entry.name == PHOTO_LOG_JPG_NAME + ".part"):
            entry.unlink(missing_ok=True)
            removed += 1
    return removed


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{size:,.1f} {unit}" if unit != "B" else f"{int(size):,} B"
        size /= 1024
    return f"{size:,.2f} TB"


def describe_savings(bmp_bytes: int, jpeg_bytes: int) -> str:
    if not bmp_bytes:
        return "n/a (no converted or existing JPEGs)"
    saved = bmp_bytes - jpeg_bytes
    ratio = f", ratio {bmp_bytes / jpeg_bytes:.1f}:1" if jpeg_bytes else ""
    return (
        f"BMP {format_bytes(bmp_bytes)} -> JPEG {format_bytes(jpeg_bytes)}, "
        f"{'saves' if saved >= 0 else 'adds'} {format_bytes(abs(saved))} ({abs(saved) / bmp_bytes:.1%}{ratio})"
    )


def load_photo_log(folder: Path) -> dict[str, dict[str, str]]:
    photo_log = folder / PHOTO_LOG_NAME
    if not photo_log.is_file():
        raise FileNotFoundError(f"{PHOTO_LOG_NAME} not found in {folder}")
    rows: dict[str, dict[str, str]] = {}
    with progress_display() as progress, photo_log.open(newline="", encoding="utf-8-sig") as handle:
        progress.console.print(f"Loading {photo_log}...", markup=False)
        task = progress.add_task("Loading photo log", total=None, detail="Reading CSV rows")
        for row in csv.DictReader(handle):
            filename = Path(row.get("filename_string") or "").name
            if filename:
                rows[filename] = row
            progress.update(task, advance=1, detail=f"{len(rows):,} filenames loaded")
        progress.update(task, total=progress.tasks[task].completed, detail=f"{len(rows):,} filenames loaded")
    return rows


def write_jpeg_photo_log(folder: Path) -> tuple[Path, int, int]:
    """Atomically write photo_log_jpg.csv; returns (path, rows pointing at .jpg, rows left unchanged).

    A row's filename_string is switched to .jpg only if that JPEG exists (non-empty) in this folder.
    The file is written to a ``.part`` temp file, flushed to disk, then renamed over the target, so
    readers only ever see the previous complete file or the new complete file.
    """
    source = folder / PHOTO_LOG_NAME
    target = folder / PHOTO_LOG_JPG_NAME
    temp_target = folder / (PHOTO_LOG_JPG_NAME + ".part")
    jpeg_sizes = {
        entry.name: entry.stat().st_size
        for entry in os.scandir(folder)
        if entry.is_file() and entry.name.lower().endswith(".jpg")
    }
    pointed = unchanged = 0
    with progress_display() as progress, source.open(newline="", encoding="utf-8-sig") as handle:
        progress.console.print(f"Preparing {target.name}...", markup=False)
        task = progress.add_task("Reading export rows", total=None, detail="Reading CSV rows")
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            rows.append(row)
            progress.update(task, advance=1, detail=f"{len(rows):,} rows read")
        if "source_filename_string" not in fieldnames:
            fieldnames.insert(fieldnames.index("filename_string") + 1, "source_filename_string")
        progress.reset(task, description="Writing JPEG photo log", total=len(rows), detail="Writing CSV rows")
        try:
            with temp_target.open("w", newline="", encoding="utf-8") as output:
                writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                for index, row in enumerate(rows, start=1):
                    original = row.get("filename_string") or ""
                    row["source_filename_string"] = original
                    jpeg_name = Path(original).stem + ".jpg"
                    if original.lower().endswith(".bmp") and jpeg_sizes.get(jpeg_name, 0) > 0:
                        row["filename_string"] = original[:-4] + ".jpg"
                        pointed += 1
                    else:
                        unchanged += 1
                    writer.writerow(row)
                    progress.update(task, advance=1, detail=f"{index:,}/{len(rows):,} rows written")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_target, target)
        except BaseException:
            temp_target.unlink(missing_ok=True)
            raise
    return target, pointed, unchanged


def move_to_raw(bmp_path: Path, raw_dir: Path) -> None:
    jpeg = bmp_path.with_suffix(".jpg")
    if not jpeg.is_file() or jpeg.stat().st_size == 0:
        raise RuntimeError(f"refusing to move {bmp_path.name}: JPEG missing or empty")
    raw_dir.mkdir(exist_ok=True)
    shutil.move(str(bmp_path), str(raw_dir / bmp_path.name))


def delete_raw(bmp_path: Path) -> None:
    """Permanently delete a BMP once its nonempty JPEG exists, to reclaim disk space during the run."""
    jpeg = bmp_path.with_suffix(".jpg")
    if not jpeg.is_file() or jpeg.stat().st_size == 0:
        raise RuntimeError(f"refusing to delete {bmp_path.name}: JPEG missing or empty")
    bmp_path.unlink()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert ReefScan session BMPs to JPEGs with EXIF metadata from photo_log.csv.",
    )
    parser.add_argument(
        "folders",
        type=Path,
        nargs="+",
        metavar="folder",
        help="Session folder(s) containing photo_log.csv and *.bmp files (or parent folders with --recursive)",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Search each folder recursively for session folders containing photo_log.csv",
    )
    parser.add_argument("-q", "--quality", type=int, default=95, help="JPEG quality 1-100 (default: 95)")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="Parallel worker processes (default: CPU count - 1)",
    )
    raw_handling = parser.add_mutually_exclusive_group()
    raw_handling.add_argument(
        "--move-raw",
        action="store_true",
        help=f"Once a BMP has a nonempty JPEG, move the BMP into '{RAW_SUBFOLDER}/' within its own session folder",
    )
    raw_handling.add_argument(
        "--delete-raw",
        action="store_true",
        help=(
            "Once a BMP has a nonempty JPEG, delete the BMP immediately to free disk space as the run proceeds "
            "(cannot be combined with --move-raw)"
        ),
    )
    parser.add_argument("--overwrite", action="store_true", help="Re-convert even if the JPEG already exists")
    parser.add_argument(
        "--write-photo-log",
        action="store_true",
        help="Also write photo_log_jpg.csv with filename_string pointing at the .jpg files",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report what would be done without writing anything")
    args = parser.parse_intermixed_args(argv)
    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    for folder in args.folders:
        if not folder.is_dir():
            parser.error(f"folder does not exist: {folder}")
    return args


def discover_session_folders(roots: list[Path], recursive: bool) -> tuple[list[Path], list[str]]:
    """Return (unique session folders, problems). Without recursion each root is a session folder."""
    found: dict[Path, None] = {}
    problems: list[str] = []
    with progress_display() as progress:
        task = progress.add_task("Finding session folders", total=None, detail="")
        for root in roots:
            root = root.resolve()
            if not recursive:
                found.setdefault(root)
                progress.update(task, advance=1, detail=str(root))
                continue
            matches = 0
            for dirpath, dirnames, filenames in os.walk(root):
                # Never treat archived raw BMPs as a session of their own.
                dirnames[:] = sorted(d for d in dirnames if d != RAW_SUBFOLDER)
                if PHOTO_LOG_NAME in filenames:
                    matches += 1
                    found.setdefault(Path(dirpath))
                progress.update(task, advance=1, detail=f"{len(found):,} found | {dirpath}")
            if not matches:
                problems.append(f"{root}: no {PHOTO_LOG_NAME} found in any subfolder")
        progress.update(task, total=progress.tasks[task].completed, detail=f"{len(found):,} session folder(s)")
    return list(found), problems


def process_folder(folder: Path, args: argparse.Namespace, pool: ProcessPoolExecutor) -> dict[str, object]:
    """Convert one session folder. All reads and writes stay inside ``folder``."""
    result: dict[str, object] = {
        "folder": folder,
        "converted": 0,
        "skipped": 0,
        "error": 0,
        "moved": 0,
        "deleted": 0,
        "bmp_bytes": 0,
        "jpeg_bytes": 0,
        "pending_bytes": 0,
        "errors": [],
        "fatal": False,
        "interrupted": False,
    }
    errors: list[str] = result["errors"]  # type: ignore[assignment]
    raw_dir = folder / RAW_SUBFOLDER

    try:
        rows = load_photo_log(folder)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        errors.append(f"could not read {PHOTO_LOG_NAME}: {exc}")
        result["fatal"] = True
        return result

    if not args.dry_run:
        removed = remove_partial_files(folder)
        if removed:
            print(f"Removed {removed} leftover .part file(s) from an earlier interrupted run.")

    with progress_display() as progress:
        progress.console.print(f"Scanning {folder}...", markup=False)
        task = progress.add_task("Scanning session folder", total=None, detail="Looking for BMP files")
        bmps = []
        for entry in folder.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".bmp":
                bmps.append(entry)
            progress.update(task, advance=1, detail=f"{len(bmps):,} BMPs found")
        progress.update(task, description="Checking archived BMPs", detail="Scanning raw subfolder")
        already_moved = set()
        if raw_dir.is_dir():
            for entry in raw_dir.iterdir():
                already_moved.add(entry.name)
                progress.update(task, advance=1, detail=f"{len(already_moved):,} archived entries")
        progress.update(task, description="Matching photo log", detail="Sorting and matching filenames")
        bmps.sort()
        unmatched = [p.name for p in bmps if p.name not in rows]
        missing = set(rows) - {p.name for p in bmps} - already_moved
        progress.update(task, total=progress.tasks[task].completed, detail=f"{len(bmps):,} BMPs found")

    print(f"Folder:            {folder}")
    print(f"photo_log rows:    {len(rows)}")
    print(f"BMP files found:   {len(bmps)}")
    print(f"BMPs without row:  {len(unmatched)}")
    print(f"Rows without BMP:  {len(missing)}")

    if args.dry_run:
        bmp_bytes = jpeg_bytes = pending_bytes = to_convert = 0
        with progress_display() as progress:
            progress.console.print("Checking existing JPEGs (dry run)...")
            task = progress.add_task("Checking existing JPEGs", total=len(bmps), detail="")
            for p in bmps:
                jpeg = p.with_suffix(".jpg")
                if args.overwrite or not jpeg.exists():
                    to_convert += 1
                    pending_bytes += p.stat().st_size
                else:
                    bmp_bytes += p.stat().st_size
                    jpeg_bytes += jpeg.stat().st_size
                progress.update(task, advance=1, detail=p.name)
        result.update(converted=to_convert, pending_bytes=pending_bytes, bmp_bytes=bmp_bytes, jpeg_bytes=jpeg_bytes)
        print(f"Dry run: would convert {to_convert} file(s) ({format_bytes(pending_bytes)} of BMPs).")
        if bmp_bytes:
            print(f"Existing JPEGs:    {describe_savings(bmp_bytes, jpeg_bytes)}")
        return result
    counts = {"converted": 0, "skipped": 0, "error": 0, "moved": 0, "deleted": 0}
    bmp_bytes = jpeg_bytes = 0
    start = time.monotonic()
    if not bmps:
        print("Nothing to convert.")
    else:
        with progress_display() as progress:
            progress.console.print(f"Converting {folder} with {args.workers} worker(s)...", markup=False)
            queue_task = progress.add_task("Queuing conversions", total=len(bmps), detail="Starting workers")
            task = progress.add_task("Converting BMPs", total=len(bmps), detail="Waiting for workers")
            futures: list[Future] = []
            handled: set[Future] = set()

            def handle(future: Future) -> None:
                nonlocal bmp_bytes, jpeg_bytes
                handled.add(future)
                bmp_path, status, message, file_bmp_bytes, file_jpeg_bytes = future.result()
                counts[status] += 1
                bmp_bytes += file_bmp_bytes
                jpeg_bytes += file_jpeg_bytes
                if status == "error":
                    errors.append(f"{Path(bmp_path).name}: {message}")
                elif args.move_raw:
                    try:
                        move_to_raw(Path(bmp_path), raw_dir)
                        counts["moved"] += 1
                    except (OSError, RuntimeError) as exc:
                        errors.append(f"{Path(bmp_path).name}: move failed: {exc}")
                elif args.delete_raw:
                    try:
                        delete_raw(Path(bmp_path))
                        counts["deleted"] += 1
                    except (OSError, RuntimeError) as exc:
                        errors.append(f"{Path(bmp_path).name}: delete failed: {exc}")
                elapsed = time.monotonic() - start
                rate = len(handled) / elapsed if elapsed else 0.0
                progress.update(
                    task,
                    advance=1,
                    detail=(
                        f"{len(handled)}/{len(bmps)} | {rate:.1f} img/s\n"
                        f"{counts['converted']} converted, {counts['skipped']} skipped\n"
                        f"{len(errors)} errors, {counts['moved']} moved, {counts['deleted']} deleted\n"
                        f"{describe_savings(bmp_bytes, jpeg_bytes)}"
                    ),
                )

            try:
                for p in bmps:
                    # Each worker receives only this folder's photo_log row for this exact file.
                    futures.append(pool.submit(convert_one, str(p), rows.get(p.name), args.quality, args.overwrite))
                    progress.update(queue_task, advance=1, detail=p.name)
                for future in as_completed(futures):
                    handle(future)
            except KeyboardInterrupt:
                result["interrupted"] = True
                cancelled = sum(f.cancel() for f in futures)
                in_flight = [f for f in futures if f not in handled and not f.cancelled()]
                progress.console.print(
                    f"\nCtrl+C: cancelled {cancelled} queued image(s); finishing {len(in_flight)} already started "
                    "(press Ctrl+C again to quit without waiting)...",
                    markup=False,
                )
                progress.update(task, total=len(handled) + len(in_flight))
                for future in as_completed(in_flight):
                    handle(future)
        result.update(counts, bmp_bytes=bmp_bytes, jpeg_bytes=jpeg_bytes)

    if result["interrupted"]:
        remove_partial_files(folder)
        if args.write_photo_log:
            print(f"{PHOTO_LOG_JPG_NAME} not written because the run was interrupted.")
    elif args.write_photo_log:
        try:
            path, pointed, unchanged = write_jpeg_photo_log(folder)
        except (OSError, csv.Error, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"could not write {PHOTO_LOG_JPG_NAME}: {type(exc).__name__}: {exc}")
        else:
            note = f"; {unchanged:,} row(s) left unchanged (no matching JPEG)" if unchanged else ""
            print(f"Wrote {path.name}: {pointed:,} row(s) -> .jpg{note}")

    print(
        f"Converted: {counts['converted']}  Skipped: {counts['skipped']}  "
        f"Errors: {counts['error']}  Moved: {counts['moved']}  Deleted: {counts['deleted']}  "
        f"({time.monotonic() - start:.0f}s)"
        + ("  [INTERRUPTED]" if result["interrupted"] else "")
    )
    print(f"Disk usage:        {describe_savings(bmp_bytes, jpeg_bytes)}")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        folders, problems = discover_session_folders(args.folders, args.recursive)
    except KeyboardInterrupt:
        print("\nInterrupted while searching for session folders; nothing was changed.", file=sys.stderr)
        return 130

    print(f"Session folders:   {len(folders)}")
    print(f"Quality / workers: {args.quality} / {args.workers}")
    if args.move_raw:
        raw_mode = "move -> <session>/" + RAW_SUBFOLDER
    elif args.delete_raw:
        raw_mode = "delete BMP as soon as its JPEG is written"
    else:
        raw_mode = "no (BMPs left in place)"
    print(f"Raw BMP handling:  {raw_mode}")
    for problem in problems:
        print(f"WARNING: {problem}", file=sys.stderr)

    results: list[dict] = []
    interrupted = forced = False
    pool = ProcessPoolExecutor(max_workers=args.workers, initializer=ignore_sigint)
    try:
        for index, folder in enumerate(folders, start=1):
            print(f"\n=== [{index}/{len(folders)}] {folder} ===")
            results.append(process_folder(folder, args, pool))
            if results[-1]["interrupted"]:
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = forced = True
        print("\nInterrupted; exiting without waiting for in-progress images.", file=sys.stderr)
    finally:
        # Workers ignore Ctrl+C, so each finishes (at most) its current image before exiting.
        pool.shutdown(wait=not forced, cancel_futures=True)

    totals = {
        key: sum(r[key] for r in results)
        for key in ("converted", "skipped", "error", "moved", "deleted", "bmp_bytes", "jpeg_bytes", "pending_bytes")
    }
    print(f"\n=== Summary: {len(folders)} session folder(s) ===")
    if args.dry_run:
        print(f"Dry run: would convert {totals['converted']} file(s) ({format_bytes(totals['pending_bytes'])} of BMPs).")
    else:
        print(
            f"Converted: {totals['converted']}  Skipped: {totals['skipped']}  "
            f"Errors: {totals['error']}  Moved: {totals['moved']}  Deleted: {totals['deleted']}"
        )
    print(f"Disk usage:        {describe_savings(totals['bmp_bytes'], totals['jpeg_bytes'])}")
    if totals["bmp_bytes"] and not args.dry_run:
        if args.delete_raw:
            print("Note: converted/existing BMPs were deleted as their JPEGs were written; that space is already reclaimed.")
        else:
            where = f"in each session's {RAW_SUBFOLDER}/" if args.move_raw else "next to the JPEGs"
            print(f"Note: BMPs are still on disk ({where}); space is reclaimed only after they are deleted.")

    for r in results:
        for line in r["errors"]:
            print(f"  ERROR {r['folder']}: {line}", file=sys.stderr)
    if interrupted:
        print(
            f"\nINTERRUPTED after {len(results)} of {len(folders)} session folder(s). "
            "Rerun the same command to resume: finished JPEGs are skipped"
            + (f" and {PHOTO_LOG_JPG_NAME} is written once a folder completes." if args.write_photo_log else "."),
            file=sys.stderr,
        )
        return 130
    if problems or any(r["fatal"] for r in results):
        return 2
    return 1 if any(r["errors"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
