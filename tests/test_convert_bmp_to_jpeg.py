import csv
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import ExifTags, Image
from rich.console import Console

import convert_bmp_to_jpeg as converter


def make_session(folder, stamp):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "photo_log.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["filename_string", "timestamp"])
        writer.writeheader()
        writer.writerow({"filename_string": "frame.bmp", "timestamp": stamp})
    with Image.new("RGB", (8, 8), "blue") as image:
        image.save(folder / "frame.bmp")


class ConverterProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        with (self.folder / "photo_log.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["filename_string", "timestamp"])
            writer.writeheader()
            writer.writerow({"filename_string": "frame.bmp", "timestamp": "20261007_232044_513"})
        with Image.new("RGB", (8, 8), "blue") as image:
            image.save(self.folder / "frame.bmp")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(Path(converter.__file__)), str(self.folder), "--workers", "2", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_dry_run_shows_loading_and_scan_without_writing(self):
        result = self.run_cli("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Loading ", result.stderr)
        self.assertIn("Scanning ", result.stderr)
        self.assertIn("Checking existing JPEGs", result.stderr)
        self.assertIn("would convert 1 file(s)", result.stdout)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["frame.bmp", "photo_log.csv"])

    def test_conversion_skip_overwrite_export_and_move(self):
        result = self.run_cli("--write-photo-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Queuing conversions", result.stderr)
        self.assertIn("Converting BMPs", result.stderr)
        self.assertIn("1 converted, 0 skipped", result.stderr)
        self.assertIn("0 errors, 0 moved", result.stderr)
        self.assertIn("Writing JPEG photo log", result.stderr)
        with Image.open(self.folder / "frame.jpg") as image:
            self.assertEqual(image.size, (8, 8))
            exif = image.getexif()
            self.assertEqual(exif[ExifTags.Base.ImageDescription], "frame.bmp")
            self.assertEqual(
                exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal],
                "2026:10:07 23:20:44",
            )
        with (self.folder / "photo_log_jpg.csv").open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["filename_string"], "frame.jpg")
        self.assertEqual(row["source_filename_string"], "frame.bmp")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Converted: 0  Skipped: 1", result.stdout)
        result = self.run_cli("--overwrite", "--move-raw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Converted: 1  Skipped: 0  Errors: 0  Moved: 1", result.stdout)
        self.assertFalse((self.folder / "frame.bmp").exists())
        self.assertTrue((self.folder / converter.RAW_SUBFOLDER / "frame.bmp").is_file())
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to convert.", result.stdout)
        self.assertIn("Rows without BMP:  0", result.stdout)

    def test_delete_raw_removes_bmp_after_conversion(self):
        result = self.run_cli("--delete-raw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Converted: 1  Skipped: 0  Errors: 0  Moved: 0  Deleted: 1", result.stdout)
        self.assertIn("delete BMP as soon as its JPEG is written", result.stdout)
        self.assertTrue((self.folder / "frame.jpg").is_file())
        self.assertFalse((self.folder / "frame.bmp").exists())
        self.assertFalse((self.folder / converter.RAW_SUBFOLDER).exists())

    def test_delete_raw_removes_already_converted_bmp(self):
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertTrue((self.folder / "frame.bmp").exists())
        result = self.run_cli("--delete-raw")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipped: 1", result.stdout)
        self.assertIn("Deleted: 1", result.stdout)
        self.assertFalse((self.folder / "frame.bmp").exists())

    def test_move_raw_and_delete_raw_are_mutually_exclusive(self):
        result = self.run_cli("--move-raw", "--delete-raw")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with", result.stderr)

    def test_errors_remain_visible_and_return_failure(self):
        (self.folder / "broken.bmp").write_bytes(b"not an image")
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("1 converted, 0 skipped", result.stderr)
        self.assertIn("1 errors, 0 moved", result.stderr)
        self.assertIn("broken.bmp: UnidentifiedImageError", result.stderr)
        self.assertFalse((self.folder / "broken.jpg").exists())
        self.assertFalse((self.folder / "broken.jpg.part").exists())

    def test_missing_photo_log_reports_error(self):
        (self.folder / "photo_log.csv").unlink()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("could not read photo_log.csv:", result.stderr)

    def test_reports_disk_usage(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        bmp = (self.folder / "frame.bmp").stat().st_size
        jpg = (self.folder / "frame.jpg").stat().st_size
        expected = converter.describe_savings(bmp, jpg)
        self.assertIn(f"Disk usage:        {expected}", result.stdout)
        self.assertIn("BMPs are still on disk", result.stdout)
        self.assertIn(f"{bmp:,} B", expected)

    def test_recursive_multi_folder_isolation(self):
        parent = self.folder / "voyage"
        stamps = {"a": "20261001_010101_000", "b": "20261002_020202_000"}
        for name, stamp in stamps.items():
            make_session(parent / name / "deep", stamp)
        # An archived photo log must never be picked up as a session.
        make_session(parent / "a" / "deep" / converter.RAW_SUBFOLDER, "20261003_030303_000")
        result = subprocess.run(
            [sys.executable, str(Path(converter.__file__)), str(parent), str(parent / "a" / "deep"),
             "-r", "--workers", "2", "--move-raw", "--write-photo-log"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Session folders:   2", result.stdout)
        self.assertIn("=== Summary: 2 session folder(s) ===", result.stdout)
        self.assertIn("Converted: 2  Skipped: 0  Errors: 0  Moved: 2", result.stdout)
        for name, stamp in stamps.items():
            session = parent / name / "deep"
            with Image.open(session / "frame.jpg") as image:
                taken = image.getexif().get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal]
            self.assertEqual(taken, converter.parse_timestamp(stamp)[0])
            self.assertTrue((session / converter.RAW_SUBFOLDER / "frame.bmp").is_file())
            self.assertTrue((session / "photo_log_jpg.csv").is_file())
        archived = parent / "a" / "deep" / converter.RAW_SUBFOLDER
        self.assertFalse((archived / "frame.jpg").exists())
        self.assertFalse((archived / converter.RAW_SUBFOLDER).exists())

    def test_photo_log_points_only_at_existing_jpegs(self):
        (self.folder / "broken.bmp").write_bytes(b"not an image")
        with (self.folder / "photo_log.csv").open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["broken.bmp", "20261007_232044_513"])
            writer.writerow(["never_captured.bmp", "20261007_232044_513"])
        result = self.run_cli("--write-photo-log")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("1 row(s) -> .jpg; 2 row(s) left unchanged (no matching JPEG)", result.stdout)
        with (self.folder / "photo_log_jpg.csv").open(newline="", encoding="utf-8") as handle:
            names = [row["filename_string"] for row in csv.DictReader(handle)]
        self.assertEqual(names, ["frame.jpg", "broken.bmp", "never_captured.bmp"])

    def test_photo_log_written_when_all_bmps_already_moved(self):
        self.assertEqual(self.run_cli("--move-raw").returncode, 0)
        result = self.run_cli("--write-photo-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to convert.", result.stdout)
        self.assertIn("Wrote photo_log_jpg.csv: 1 row(s) -> .jpg", result.stdout)

    def test_stale_part_files_removed_only_in_processed_folder(self):
        (self.folder / "frame.jpg.part").write_bytes(b"half")
        (self.folder / "photo_log_jpg.csv.part").write_text("half")
        other = self.folder / "elsewhere"
        other.mkdir()
        (other / "keep.jpg.part").write_bytes(b"half")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Removed 2 leftover .part file(s)", result.stdout)
        self.assertEqual(list(self.folder.glob("*.part")), [])
        self.assertTrue((other / "keep.jpg.part").exists())

    def test_photo_log_write_is_atomic(self):
        target = self.folder / converter.PHOTO_LOG_JPG_NAME
        target.write_text("previous complete file\n")
        with patch.object(csv.DictWriter, "writerow", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                converter.write_jpeg_photo_log(self.folder)
        self.assertEqual(target.read_text(), "previous complete file\n")
        self.assertFalse((self.folder / (converter.PHOTO_LOG_JPG_NAME + ".part")).exists())

    def test_bad_folder_does_not_stop_others(self):
        other = self.folder.parent / (self.folder.name + "_empty")
        other.mkdir()
        self.addCleanup(other.rmdir)
        result = self.run_cli(str(other))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue((self.folder / "frame.jpg").is_file())
        self.assertIn("could not read photo_log.csv", result.stderr)

    def test_terminal_progress_refreshes_without_completed_work(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=True, width=180)
        with patch.object(converter, "Console", return_value=console):
            with converter.progress_display() as progress:
                task = progress.add_task("Loading photo log", total=None, detail="Reading CSV rows")
                progress.refresh()
                before = len(output.getvalue())
                time.sleep(0.35)
                self.assertGreater(len(output.getvalue()), before)
                self.assertEqual(progress.tasks[task].completed, 0)
        self.assertIn("Loading photo log", output.getvalue())


if __name__ == "__main__":
    unittest.main()
