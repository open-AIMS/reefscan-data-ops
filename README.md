# ReefScan data operations

A small command-line tool that converts ReefScan session **BMP** images into
much smaller **JPEG** images, copying the useful information (capture time, GPS,
depth, temperature, etc.) from each session's `photo_log.csv` into the JPEG's
EXIF metadata.

JPEGs are typically a fraction of the size of the original BMPs, so converting a
session frees up a large amount of disk space once the BMPs are removed.

---

## Part 1 — First-time setup on Windows (no WSL)

You only need to do this **once** on a given computer. If you are comfortable
with a terminal you can skip ahead, but every step is spelled out below for
people who have never run a Python script.

### Step 1: Install `uv`

`uv` is a single, self-contained tool that installs the correct Python version
and all the libraries this script needs. You do **not** need to install Python
yourself first — `uv` handles that for you.

1. Click the Windows **Start** button, type `PowerShell`, and open **Windows
   PowerShell**. A blue or black window with a text prompt appears.
2. Copy the line below, paste it into the PowerShell window (right-click to
   paste), and press **Enter**:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

3. Wait for it to finish. It downloads and installs `uv` for your user account.
4. **Close the PowerShell window and open a new one.** This is important — the
   new window picks up the freshly installed `uv`.
5. Check that it worked by running:

   ```powershell
   uv --version
   ```

   You should see something like `uv 0.13.4`. If instead you see a red error
   saying `uv` is not recognized, close and reopen PowerShell once more. If it
   still fails, see [Troubleshooting](#troubleshooting).

### Step 2: Get a copy of this project onto your computer

If someone sent you this project as a ZIP file, extract it somewhere easy to
find, for example `C:\reefscan-data-ops`.

If you use Git, you can instead clone it. Either way, you end up with a folder
that contains `convert_bmp_to_jpeg.py`, `pyproject.toml`, and this `README.md`.

### Step 3: Open PowerShell *inside* the project folder

Every command in this guide must be run from inside the project folder.

1. Open **File Explorer** and navigate to the project folder (the one that
   contains `convert_bmp_to_jpeg.py`).
2. Click once in the **address bar** at the top (it highlights the current
   path), type `powershell`, and press **Enter**. A PowerShell window opens
   already pointing at that folder.

   Alternatively, open PowerShell and move into the folder manually. The `cd`
   ("change directory") command does this — adjust the path to wherever you put
   the project:

   ```powershell
   cd C:\reefscan-data-ops
   ```

To confirm you are in the right place, run `dir`. You should see
`convert_bmp_to_jpeg.py` listed.

### Step 4: Install the project's dependencies

From inside the project folder, run:

```powershell
uv sync
```

The first time, this downloads the right Python version and the two libraries
the script uses (Pillow for images, Rich for the on-screen progress bars). It
can take a minute. When it finishes you are ready to convert images. You do not
need to run `uv sync` again unless the project is updated.

> **Note:** You run the script with `uv run ...` (shown below). The `uv run`
> prefix automatically uses the private environment `uv sync` created, so you
> never have to "activate" anything.

---

## Part 2 — Converting a session

A "session folder" is a folder that contains a `photo_log.csv` file **and** the
`.bmp` images it refers to. The script writes each JPEG right next to its BMP.

### Always start with a dry run

A **dry run** inspects a folder and tells you what *would* happen, without
changing a single file. Always do this first so there are no surprises. Replace
the path in quotes with the real path to your session folder:

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan\session_folder" --dry-run
```

This reports how many BMPs it found and how much disk space they occupy.

### Do the real conversion

When the dry run looks right, run the same command **without** `--dry-run`:

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan\session_folder"
```

The BMPs stay on disk and the new JPEGs appear beside them. While it runs you
see live progress bars with elapsed time, estimated time remaining, speed, and
running counts of converted / skipped / errors.

### Converting several folders at once

Pass more than one folder, separated by spaces (each path in quotes):

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan\session_a" "D:\reefscan\session_b"
```

Or point at a parent folder and add `--recursive` (or `-r`) to find every
session folder beneath it automatically:

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan" --recursive --dry-run
```

Folders are processed one at a time and kept fully separate: each uses only its
own `photo_log.csv`, and a problem in one folder does not stop the others.

---

## Part 3 — Managing disk space: what to do with the BMPs

After conversion you have both the big BMPs and the small JPEGs. The BMPs are no
longer needed. You have **three** choices for dealing with them. Pick the one
that matches how much free space you have.

### Option A (default): leave the BMPs in place

If you do nothing special, the BMPs are left exactly where they are, next to the
new JPEGs. You can review the results and delete the BMPs yourself later. This
is the safest option but needs enough free space to hold **both** the BMPs and
the JPEGs at the same time.

### Option B: move the BMPs aside with `--move-raw`

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan\session_folder" --move-raw
```

Once a BMP has a valid JPEG, the BMP is moved into a `_raw_data_to_delete`
folder inside that same session folder. Nothing is deleted — you still need
space for both copies during the run — but afterwards you can delete the
`_raw_data_to_delete` folders in one sweep when you are confident the JPEGs are
good.

### Option C: delete each BMP immediately with `--delete-raw`

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan\session_folder" --delete-raw
```

As soon as a BMP's JPEG has been written successfully, that BMP is **permanently
deleted**. Space is freed *continuously as the run proceeds*, so this is the
option to use when the destination disk does **not** have room to hold both the
BMPs and the JPEGs at once.

**`--delete-raw` and `--move-raw` cannot be used together** — choose one. If you
pass both, the script stops and tells you to pick one.

> **Safety:** `--delete-raw` only ever deletes a BMP after confirming a matching,
> non-empty JPEG already exists next to it. A BMP that failed to convert is never
> deleted. Even so, deletion is permanent (the files do **not** go to the
> Recycle Bin), so run a `--dry-run` first and make sure your JPEGs are what you
> expect before using it on irreplaceable data.

### Which should I choose?

| Your situation                                             | Use            |
|------------------------------------------------------------|----------------|
| Plenty of free space; want to review before deleting       | Option A       |
| Enough space for both copies; want easy bulk cleanup later | `--move-raw`   |
| Not enough space to hold BMPs **and** JPEGs together       | `--delete-raw` |

---

## Part 4 — Other useful options

Add any of these to the end of a command. You can combine them.

- `--dry-run` — report what would happen without writing, moving, or deleting
  anything. Always safe.
- `--overwrite` — re-create JPEGs even if they already exist. By default,
  existing JPEGs are skipped so you can safely re-run a command to resume.
- `--write-photo-log` — also write a `photo_log_jpg.csv` whose rows point at the
  new `.jpg` files instead of the `.bmp` files.
- `-w N` / `--workers N` — convert `N` images in parallel (default: one less
  than the number of CPU cores). More workers is faster but uses more CPU.
- `-q N` / `--quality N` — JPEG quality from 1 to 100 (default: 95). Lower
  values make smaller files with slightly lower image quality.
- `-r` / `--recursive` — treat each given path as a parent and find session
  folders beneath it.

See every option with:

```powershell
uv run python .\convert_bmp_to_jpeg.py --help
```

A fuller example combining several options:

```powershell
uv run python .\convert_bmp_to_jpeg.py "D:\reefscan" --recursive --workers 4 --delete-raw --write-photo-log
```

---

## Part 5 — Stopping and resuming safely

- Press **Ctrl + C** once to stop. Images not yet started are cancelled, the few
  already being converted finish cleanly, and the script exits. Press **Ctrl + C**
  a second time to quit immediately without waiting.
- To **resume**, simply run the same command again. Existing JPEGs are skipped,
  so it picks up where it left off, and any leftover temporary `.part` files from
  the interruption are cleaned up.
- JPEGs and `photo_log_jpg.csv` are written to a temporary `.part` file and only
  renamed into place when complete, so an interruption never leaves a truncated
  or corrupted file.

---

## Troubleshooting

- **`uv` is not recognized** — close PowerShell and open a brand-new window
  after installing `uv` (Step 1). If it still fails, sign out of Windows and back
  in, then try again.
- **`folder does not exist`** — check the path is spelled correctly and wrapped
  in double quotes. Copy it from File Explorer's address bar to be sure.
- **`photo_log.csv not found`** — the folder you pointed at is not a session
  folder. Point at the folder that directly contains `photo_log.csv`, or use
  `--recursive` on a parent folder.
- **Running commands from the wrong place** — all commands assume PowerShell is
  open *inside* the project folder (the one with `convert_bmp_to_jpeg.py`). See
  Part 1, Step 3.

### For developers: a separate Windows environment

If this checkout already has a WSL/Linux `.venv` (for example, you share the
folder between WSL and Windows), tell `uv` to use a separate Windows environment
before running any command in the same PowerShell window:

```powershell
$env:UV_PROJECT_ENVIRONMENT = ".venv-windows"
```

### For developers: running the tests

```powershell
uv run python -m unittest discover -s tests -v
```

---

## Reference: how conversion works

The session folder must contain `photo_log.csv` and its BMP images. For each
BMP, the script finds the matching row in the photo log and copies that row's
information into the JPEG's EXIF metadata (capture time, GPS, depth, temperature,
frame number, exposure, and the full CSV row stored as JSON in the UserComment
field). JPEGs are written alongside the BMPs with the same name.

Disk usage — BMP size versus JPEG size, space saved, and compression ratio — is
reported live, per folder and as a grand total. Messages and progress bars are
written to the error stream; the final summary is written to the standard output
stream, so redirected output stays clean and readable.
