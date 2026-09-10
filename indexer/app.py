from __future__ import annotations

import json
import queue
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ruff: noqa
# Uncomment next 2 lines to force XWayland if Wayland causes issues:
# import os
# os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import QByteArray, QEvent, QMimeData, QPoint, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QDrag, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

# ── Dev Mode ───────────────────────────────────────────────────────────────────
# DEV_MODE is activated ONLY by passing --dev as a command-line argument.
#
# CONTRACT - read this before touching anything below:
#   • DEV_MODE is a READ-ONLY boolean set once at import time from sys.argv.
#   • It is COMPLETELY ISOLATED from all production code paths.
#   • It NEVER writes to prefs, databases, scripts files, or any disk state.
#   • It NEVER overwrites "last_db" so the user's real session is preserved.
#   • The fake data and DevDatabase class defined in the _DEV section below
#     are the ONLY things that change when DEV_MODE is True.
#   • If DEV_MODE is False, none of the dev-mode symbols are ever referenced.
#   • The "dev" database CANNOT be opened through the normal Open Database
#     dialog - it only exists in memory while the flag is active.
#
# To start in dev mode:   python app.py --dev
# Normal start:           python app.py
# ──────────────────────────────────────────────────────────────────────────────
DEV_MODE: bool = "--dev" in sys.argv

APP_NAME = "vael. indexer"
APP_ORG = "vael"
APP_DIR = Path.home() / ".vael_indexer"
# Pre-rebrand data folder ("Asset Indexer"). Migrated automatically on first
# launch of this version -- see _migrate_legacy_app_dir() near main().
_LEGACY_APP_DIR = Path.home() / ".asset_indexer"
ICON_PATH = Path(__file__).parent / ("icon.ico" if sys.platform == "win32" else "icon.png")
PREFS_FILE = APP_DIR / "prefs.json"
NOTES_FILE = APP_DIR / "notes.json"

# ── Session-only window positions ─────────────────────────────────────────────
# Positions are stored here (in memory) when a dialog is dragged or closed.
# They are NEVER written to disk, so every app restart centres dialogs fresh.
_SESSION_POS: dict[str, list[int]] = {}

# 2:3 card image area
THUMB_W = 106
THUMB_H = 159
NAME_H = 16
COLS = 6


# ── Prefs ──────────────────────────────────────────────────────────────────────


def _load_prefs() -> dict:
    try:
        if PREFS_FILE.exists():
            return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_prefs(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── File Cache ─────────────────────────────────────────────────────────────────
#
# One JSON file per database, stored at APP_DIR/<name>_file_cache.json.
# Structure:
#   {
#     "/abs/path/to/asset.png": {
#       "png_mtime": 1234567890.123,
#       "json_mtime": 1234567891.456
#     },
#     ...
#   }
#
# Only PNG+JSON pairs that both exist are tracked (mirrors _run_index behaviour).
# The file is written atomically (temp → rename) so a crash mid-write never
# leaves a corrupt cache behind.
# ──────────────────────────────────────────────────────────────────────────────


def _cache_path(name: str) -> Path:
    return APP_DIR / f"{name}_file_cache.json"


def _load_file_cache(name: str) -> dict[str, dict]:
    """Return the stored {png_path: {png_mtime, json_mtime}} mapping, or {}."""
    path = _cache_path(name)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_file_cache(name: str, data: dict[str, dict]) -> None:
    """Atomically write the cache so a crash mid-write leaves the old file intact."""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_cache_path(name))
    except Exception:
        pass


def _build_file_cache(folder: Path) -> dict[str, dict]:
    """
    Walk `folder` and build a fresh cache dict from the current filesystem state.
    Only includes PNG+JSON pairs where both files exist.
    Called after a successful index to update the on-disk cache.
    """
    result: dict[str, dict] = {}
    for png in sorted(folder.rglob("*.png")):
        jpath = png.with_suffix(".json")
        if not jpath.exists():
            continue
        try:
            result[str(png)] = {
                "png_mtime": png.stat().st_mtime,
                "json_mtime": jpath.stat().st_mtime,
            }
        except OSError:
            pass
    return result


def _diff_against_cache(
    folder: Path, cache: dict[str, dict]
) -> tuple[set[str], set[str], set[str]]:
    """
    Fast os.stat pass over `folder`.  Returns three sets of PNG paths:
      added   – present on disk but not in cache
      changed – present in both but at least one mtime differs
      deleted – present in cache but no longer on disk (as a valid pair)

    No file contents are read; only st_mtime is checked.
    """
    added: set[str] = set()
    changed: set[str] = set()
    seen: set[str] = set()

    for png in folder.rglob("*.png"):
        jpath = png.with_suffix(".json")
        if not jpath.exists():
            continue  # unpaired – skip, same rule as _run_index
        key = str(png)
        seen.add(key)
        try:
            png_mtime = png.stat().st_mtime
            json_mtime = jpath.stat().st_mtime
        except OSError:
            continue

        if key not in cache:
            added.add(key)
        else:
            cached = cache[key]
            if png_mtime != cached.get("png_mtime") or json_mtime != cached.get("json_mtime"):
                changed.add(key)

    deleted: set[str] = set(cache.keys()) - seen
    return added, changed, deleted


# ── Startup Scripts ────────────────────────────────────────────────────────────

def _natural_sort_key(name: str) -> str:
    """
    Zero-pad every run of digits in `name` (lowercased) so that plain
    string comparison sorts numbers by magnitude instead of character-by-
    character -- "2" sorts before "10" -- while every other character
    keeps its normal alphabetical position relative to digits and to each
    other (e.g. "!" still sorts before "1", "1" still sorts before "a").

    This also works unchanged on "/"-separated folder paths, since "/" is
    just another character and is compared consistently at every depth.

    e.g. "img2" -> "img00000000000000000002"
         "img10" -> "img00000000000000000010"   (sorts after img2, before img20)

    Earlier version split into [text, number, text, ...] chunks and mixed
    str/int types. That produced a leading "" chunk for any name starting
    with a digit, and comparing "" against a whole word (e.g. "!other")
    always ranked the digit-led name first regardless of which character
    it was actually up against -- e.g. "100-Girlfriends" would incorrectly
    sort above "!Other". Returning a single homogeneous string avoids that.
    """
    import re

    return re.sub(r"\d+", lambda m: m.group().zfill(20), name.lower())


SCRIPTS_FILE = APP_DIR / "startup_scripts.json"


def _load_scripts() -> list[dict]:
    """Return list of {name, path, args} dicts."""
    try:
        if SCRIPTS_FILE.exists():
            data = json.loads(SCRIPTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_scripts(scripts: list[dict]) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPTS_FILE.write_text(json.dumps(scripts, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Pixmap Cache ───────────────────────────────────────────────────────────────

_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(path: str) -> QPixmap:
    """Return a cached thumbnail pixmap, or load+scale synchronously (drag fallback)."""
    if path not in _PIXMAP_CACHE:
        pix = QPixmap(path)
        if not pix.isNull():
            scaled = pix.scaled(
                THUMB_W,
                THUMB_H,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (scaled.width() - THUMB_W) // 2
            y = (scaled.height() - THUMB_H) // 2
            pix = scaled.copy(x, y, THUMB_W, THUMB_H)
        _PIXMAP_CACHE[path] = pix
    return _PIXMAP_CACHE[path]


# ── Background Pixmap Loader ───────────────────────────────────────────────────
#
# A single long-lived worker thread that drains a queue of (card, path) pairs.
# Each pixmap is loaded and scaled off the main thread, then delivered via signal
# so the card can set it without ever blocking the UI.
#
# Usage:
#   PIXMAP_WORKER.submit(card, image_path)
#
# The worker is started once at module level and runs for the lifetime of the app.
# ──────────────────────────────────────────────────────────────────────────────


class PixmapWorker(QThread):
    """Loads and scales thumbnail pixmaps off the main thread."""

    # Delivers (card_widget, pixmap) back to the main thread
    pixmap_ready = Signal(object, QPixmap)

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue = queue.Queue()

    def submit(self, card: "ThumbnailCard", path: str) -> None:
        """Queue a card for image loading. Safe to call from the main thread."""
        self._queue.put((card, path))

    def run(self) -> None:
        while True:
            try:
                card, path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # Skip if the card was already deleted (e.g. after a search refresh)
            try:
                if card is None:
                    continue
            except RuntimeError:
                continue

            # Load into the shared cache if not already present
            if path not in _PIXMAP_CACHE:
                pix = QPixmap(path)
                if not pix.isNull():
                    scaled = pix.scaled(
                        THUMB_W,
                        THUMB_H,
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    x = (scaled.width() - THUMB_W) // 2
                    y = (scaled.height() - THUMB_H) // 2
                    pix = scaled.copy(x, y, THUMB_W, THUMB_H)
                _PIXMAP_CACHE[path] = pix

            self.pixmap_ready.emit(card, _PIXMAP_CACHE[path])


# Single global worker — started once, lives for the whole app session.
PIXMAP_WORKER = PixmapWorker()
PIXMAP_WORKER.start()


# ── Index logic (thread-safe, opens its own connection) ───────────────────────


def _run_index(
    db_path: Path,
    folder: Path,
    full_rebuild: bool = False,
    progress_cb=None,  # callable(current, total, msg) or None
    flagged: Optional[tuple[set[str], set[str], set[str]]] = None,
) -> tuple[int, int]:
    """
    Opens a *fresh* SQLite connection on the calling thread, indexes `folder`,
    and closes the connection before returning.  Safe to call from any thread.
    Returns (total_assets, changes).

    flagged: if provided, a (added, changed, deleted) tuple of PNG path sets
             produced by _diff_against_cache().  Only those files are touched;
             everything else in the DB is left as-is.  When None (or when
             full_rebuild is True) the original full-scan behaviour runs.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if full_rebuild:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM folder_meta")
            conn.commit()

        # Ensure folder_meta exists (for DBs created before this feature)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folder_meta (
                folder_key  TEXT    PRIMARY KEY,
                copy_value  TEXT    NOT NULL DEFAULT ''
            )
        """)
        conn.commit()

        existing: set[str] = {
            r[0] for r in conn.execute("SELECT image_path FROM assets").fetchall()
        }

        # ── Selective (cache-diff) path ───────────────────────────────────────
        # When the caller has already diffed against the file cache we only
        # need to process the flagged files instead of scanning everything.
        if flagged is not None and not full_rebuild:
            added, changed, deleted = flagged

            # Process deleted first – just remove from DB
            for p in deleted:
                conn.execute("DELETE FROM assets WHERE image_path=?", (p,))

            to_process = sorted(added | changed)
            total = len(to_process)
            changes = len(deleted)

            for i, key in enumerate(to_process, 1):
                png = Path(key)
                jpath = png.with_suffix(".json")
                try:
                    raw = jpath.read_text(encoding="utf-8", errors="ignore")
                    json.loads(raw)
                except Exception:
                    raw = "{}"

                rel_folder = str(png.parent.relative_to(folder))
                if rel_folder == ".":
                    rel_folder = ""

                if key not in existing:
                    conn.execute(
                        "INSERT INTO assets(name, folder, image_path, json_path, json_data)"
                        " VALUES(?,?,?,?,?)",
                        (png.stem, rel_folder, key, str(jpath), raw),
                    )
                else:
                    conn.execute(
                        "UPDATE assets SET json_data=?, folder=? WHERE image_path=?",
                        (raw, rel_folder, key),
                    )
                changes += 1

                if progress_cb:
                    progress_cb(i, total, f"Indexing ({i}/{total})")

            conn.commit()
            db_total: int = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            return db_total, changes

        # ── Full scan path (original behaviour) ───────────────────────────────
        candidates = [
            png
            for png in sorted(folder.rglob("*.png"))
            if png.with_suffix(".json").exists()
        ]
        total = len(candidates)
        found: set[str] = set()
        changes = 0

        for i, png in enumerate(candidates, 1):
            jpath = png.with_suffix(".json")
            try:
                raw = jpath.read_text(encoding="utf-8", errors="ignore")
                json.loads(raw)
            except Exception:
                raw = "{}"

            rel_folder = str(png.parent.relative_to(folder))
            if rel_folder == ".":
                rel_folder = ""

            key = str(png)
            found.add(key)

            if key not in existing:
                conn.execute(
                    "INSERT INTO assets(name, folder, image_path, json_path, json_data)"
                    " VALUES(?,?,?,?,?)",
                    (png.stem, rel_folder, key, str(jpath), raw),
                )
                changes += 1
            else:
                conn.execute(
                    "UPDATE assets SET json_data=?, folder=? WHERE image_path=?",
                    (raw, rel_folder, key),
                )

            if progress_cb:
                progress_cb(i, total, f"Indexing ({i}/{total})")

        stale = existing - found
        for p in stale:
            conn.execute("DELETE FROM assets WHERE image_path=?", (p,))
        changes += len(stale)

        # ── Scan for !F-[FolderName].json files and upsert into folder_meta ──
        # Walk every directory under `folder` (including root) and look for a
        # file matching !F-<dirname>.json (case-insensitive on the stem).
        seen_folder_keys: set[str] = set()
        for dir_path in sorted(folder.rglob("*")):
            if not dir_path.is_dir():
                continue
            expected_stem = f"!F-{dir_path.name}"
            meta_file = dir_path / f"{expected_stem}.json"
            if not meta_file.exists():
                # Try case-insensitive match on Windows-style paths
                matches = [
                    f
                    for f in dir_path.iterdir()
                    if f.suffix.lower() == ".json"
                    and f.stem.lower() == expected_stem.lower()
                ]
                meta_file = matches[0] if matches else None
            if meta_file and meta_file.exists():
                try:
                    raw = meta_file.read_text(encoding="utf-8", errors="ignore")
                    data = json.loads(raw)
                    # Take the first (and for now only) value
                    copy_value = str(next(iter(data.values()))) if data else ""
                except Exception:
                    copy_value = ""
                rel = str(dir_path.relative_to(folder)).replace("\\", "/")
                if rel == ".":
                    rel = ""
                seen_folder_keys.add(rel)
                conn.execute(
                    "INSERT INTO folder_meta(folder_key, copy_value)"
                    " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                    (rel, copy_value),
                )
        # Also check the root folder itself for a matching !F-<rootname>.json
        root_stem = f"!F-{folder.name}"
        root_meta = folder / f"{root_stem}.json"
        if not root_meta.exists():
            matches = [
                f
                for f in folder.iterdir()
                if f.suffix.lower() == ".json" and f.stem.lower() == root_stem.lower()
            ]
            root_meta = matches[0] if matches else None
        if root_meta and root_meta.exists():
            try:
                raw = root_meta.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw)
                copy_value = str(next(iter(data.values()))) if data else ""
            except Exception:
                copy_value = ""
            seen_folder_keys.add("")
            conn.execute(
                "INSERT INTO folder_meta(folder_key, copy_value)"
                " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                ("", copy_value),
            )
        # Remove stale folder_meta rows for folders that no longer have an F-*.json
        existing_fk = {
            r[0] for r in conn.execute("SELECT folder_key FROM folder_meta").fetchall()
        }
        for stale_fk in existing_fk - seen_folder_keys:
            conn.execute("DELETE FROM folder_meta WHERE folder_key=?", (stale_fk,))

        conn.commit()

        db_total: int = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        return db_total, changes
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# ██  DEV-MODE SANDBOX  ████████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════════════
#
# Everything between the two ══ banners is EXCLUSIVELY for --dev mode.
# NOTHING in this block is referenced from production code paths.
# NOTHING in this block reads from or writes to disk (no DB, no prefs, no files).
#
# Structure:
#   _DEV_FAKE_ASSETS   - hardcoded list of fake asset dicts (no real images/JSON)
#   _DEV_FOLDER_META   - hardcoded folder copy-tag values
#   DevDatabase        - in-memory stub that mimics the Database API exactly
#                        but operates only on _DEV_FAKE_ASSETS; all mutating
#                        methods (update_json, delete) are no-ops.
# ──────────────────────────────────────────────────────────────────────────────

# Fake assets: three folders, a handful of entries each.
# image_path values are intentionally non-existent - _apply_pixmap will show "?".
# json_data contains realistic example JSON so the context-menu copy actions
# and the Edit JSON dialog both work (they just don't persist anything).
_DEV_FAKE_ASSETS: list[dict] = [
    # ── Folder: Characters ────────────────────────────────────────────────────
    {
        "id": 1,
        "name": "hero_warrior",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/hero_warrior.png",
        "json_path": "/dev/null/Characters/hero_warrior.json",
        "json_data": json.dumps(
            {
                "prompt": "a heroic warrior in gleaming plate armor, cinematic lighting",
                "negative_prompt": "blurry, low quality",
                "model": "stable-diffusion-xl",
                "steps": 30,
                "cfg_scale": 7.5,
            },
            indent=2,
        ),
    },
    {
        "id": 2,
        "name": "rogue_elf",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/rogue_elf.png",
        "json_path": "/dev/null/Characters/rogue_elf.json",
        "json_data": json.dumps(
            {
                "prompt": "nimble elven rogue, emerald cloak, forest background",
                "negative_prompt": "ugly, deformed",
                "model": "stable-diffusion-xl",
                "steps": 25,
                "cfg_scale": 6.0,
            },
            indent=2,
        ),
    },
    {
        "id": 3,
        "name": "dark_mage",
        "folder": "Characters",
        "image_path": "/dev/null/Characters/dark_mage.png",
        "json_path": "/dev/null/Characters/dark_mage.json",
        "json_data": json.dumps(
            {
                "prompt": "dark sorcerer holding a glowing orb, dramatic shadows",
                "negative_prompt": "cartoon, anime",
                "model": "stable-diffusion-xl",
                "steps": 40,
                "cfg_scale": 8.0,
            },
            indent=2,
        ),
    },
    # ── Folder: Landscapes ────────────────────────────────────────────────────
    {
        "id": 4,
        "name": "misty_valley",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/misty_valley.png",
        "json_path": "/dev/null/Landscapes/misty_valley.json",
        "json_data": json.dumps(
            {
                "prompt": "sweeping misty valley at dawn, volumetric fog, epic scale",
                "negative_prompt": "oversaturated, flat",
                "model": "stable-diffusion-xl",
                "steps": 35,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
    {
        "id": 5,
        "name": "crystal_cave",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/crystal_cave.png",
        "json_path": "/dev/null/Landscapes/crystal_cave.json",
        "json_data": json.dumps(
            {
                "prompt": "underground crystal cave, bioluminescent glow, reflections",
                "negative_prompt": "dark, muddy colors",
                "model": "stable-diffusion-xl",
                "steps": 30,
                "cfg_scale": 7.5,
            },
            indent=2,
        ),
    },
    {
        "id": 6,
        "name": "sky_fortress",
        "folder": "Landscapes",
        "image_path": "/dev/null/Landscapes/sky_fortress.png",
        "json_path": "/dev/null/Landscapes/sky_fortress.json",
        "json_data": json.dumps(
            {
                "prompt": "floating sky fortress above clouds, golden hour light",
                "negative_prompt": "low detail, blurry",
                "model": "stable-diffusion-xl",
                "steps": 40,
                "cfg_scale": 8.5,
            },
            indent=2,
        ),
    },
    # ── Folder: Items ─────────────────────────────────────────────────────────
    {
        "id": 7,
        "name": "magic_sword",
        "folder": "Items",
        "image_path": "/dev/null/Items/magic_sword.png",
        "json_path": "/dev/null/Items/magic_sword.json",
        "json_data": json.dumps(
            {
                "prompt": "ancient enchanted sword with glowing blue runes, product shot",
                "negative_prompt": "hands, people",
                "model": "stable-diffusion-xl",
                "steps": 28,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
    {
        "id": 8,
        "name": "potion_red",
        "folder": "Items",
        "image_path": "/dev/null/Items/potion_red.png",
        "json_path": "/dev/null/Items/potion_red.json",
        "json_data": json.dumps(
            {
                "prompt": "crimson health potion in ornate glass vial, studio lighting",
                "negative_prompt": "background clutter",
                "model": "stable-diffusion-xl",
                "steps": 25,
                "cfg_scale": 6.5,
            },
            indent=2,
        ),
    },
    {
        "id": 9,
        "name": "ancient_tome",
        "folder": "Items",
        "image_path": "/dev/null/Items/ancient_tome.png",
        "json_path": "/dev/null/Items/ancient_tome.json",
        "json_data": json.dumps(
            {
                "prompt": "weathered spellbook with arcane symbols, candle light",
                "negative_prompt": "modern, clean",
                "model": "stable-diffusion-xl",
                "steps": 32,
                "cfg_scale": 7.0,
            },
            indent=2,
        ),
    },
]

# Folder copy-tag values (simulates !F-<Name>.json in each folder)
_DEV_FOLDER_META: dict[str, str] = {
    "Characters": "<lora:char_pack_v2:0.8>",
    "Landscapes": "<lora:landscape_v3:0.9>",
    "Items": "<lora:items_v1:0.7>",
}


class DevDatabase:
    """
    ──────────────────────────────────────────────────────────────────────────
    DEV-ONLY in-memory database stub.  Mirrors the public API of Database so
    the rest of the UI code works without any special-casing inside widgets.

    RULES (do not break these):
      • Never reads from or writes to any file or SQLite database.
      • update_json() only mutates the in-memory list - changes vanish on exit.
      • delete() is a silent no-op (cards appear to stay; a refresh restores).
      • get_folder_meta() returns values from the hardcoded _DEV_FOLDER_META dict.
      • This class MUST NOT be instantiated outside of DEV_MODE code paths.
    ──────────────────────────────────────────────────────────────────────────
    """

    def __init__(self) -> None:
        # Work on a shallow copy so multiple resets don't stack mutations
        self._assets: list[dict] = [dict(a) for a in _DEV_FAKE_ASSETS]
        self.path = Path("/dev/null/dev_mode.db")  # sentinel - never accessed
        self.name = "[DEV MODE]"
        # In-memory only, never persisted - resets every DevDatabase() re-init.
        self._identifiers: list[str] = ["Adult", "Curvy", "Fantasy"]
        # Temporary identifiers (never saved anywhere, not even here really -
        # but DevDatabase is already 100% in-memory, so this just tracks which
        # names are *displayed* as temporary and keeps their membership out of
        # asset json_data, matching real Database's contract).
        self._temporary: set[str] = set()
        self._temp_members: dict[str, set[str]] = {}
        self._display_order: list[str] = list(self._identifiers)

    # ── API surface (matches Database) ────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 2000,
        folder_only: bool = False,
        json_only: bool = False,
        id_only: bool = False,
    ) -> list[dict]:
        """Filter fake assets by name (or folder name, or json contents), case-insensitive substring match."""
        q = query.lower()
        if id_only:
            results = []
            for a in self._assets:
                try:
                    data = json.loads(a.get("json_data", "{}"))
                except Exception:
                    data = {}
                tokens = [
                    t.strip()
                    for t in str(data.get("Identifier", "") or "").split(",")
                    if t.strip()
                ]
                tokens.extend(
                    name
                    for name, members in self._temp_members.items()
                    if a.get("image_path", "") in members
                )
                ident = ", ".join(tokens).lower()
                if q in ident:
                    results.append(a)
            return results[:limit]
        if not q:
            return list(self._assets)[:limit]
        if json_only:
            # Search the raw json_data text (matches ';query' UI behaviour).
            results = [a for a in self._assets if q in a.get("json_data", "").lower()]
        elif folder_only:
            # Collect unique folder names that contain the query
            matching_folders = {
                a["folder"] for a in self._assets if q in a["folder"].lower()
            }
            results = [a for a in self._assets if a["folder"] in matching_folders]
        else:
            results = [a for a in self._assets if q in a["name"].lower()]
        return results[:limit]

    def get_folder_meta(self, folder_key: str) -> Optional[str]:
        """Return the hardcoded copy-tag for a folder, or None."""
        return _DEV_FOLDER_META.get(folder_key)

    def update_json(self, image_path: str, new_json_text: str) -> None:
        """
        DEV NO-OP: Updates the in-memory asset only.
        The Edit JSON dialog will show a Save confirmation, but nothing is
        written to disk and changes reset the moment the panel refreshes.
        """
        for asset in self._assets:
            if asset["image_path"] == image_path:
                asset["json_data"] = new_json_text
                break  # in-memory only, not persisted

    def delete(self, image_path: str) -> None:
        """DEV NO-OP: silently ignores delete requests."""
        pass  # intentional no-op - dev mode does not mutate state visibly

    def close(self) -> None:
        """DEV NO-OP: nothing to close."""
        pass

    # ── Identifiers (Manage ID / Edit ID) - in-memory only, never saved ────

    def get_identifiers(self) -> list[str]:
        return list(self._display_order)

    def is_temporary(self, name: str) -> bool:
        return name in self._temporary

    def add_identifier(self, name: str, temporary: bool = False) -> None:
        if temporary:
            self._temporary.add(name)
            self._temp_members.setdefault(name, set())
        else:
            self._identifiers.append(name)
        if name not in self._display_order:
            self._display_order.append(name)

    def rename_identifier(self, old_name: str, new_name: str) -> None:
        if old_name in self._temporary:
            self._temporary.discard(old_name)
            self._temporary.add(new_name)
            self._temp_members[new_name] = self._temp_members.pop(old_name, set())
        else:
            self._identifiers = [
                new_name if n == old_name else n for n in self._identifiers
            ]
        self._display_order = [
            new_name if n == old_name else n for n in self._display_order
        ]

    def remove_identifier(self, name: str) -> None:
        if name in self._temporary:
            self._temporary.discard(name)
            self._temp_members.pop(name, None)
        else:
            self._identifiers = [n for n in self._identifiers if n != name]
        self._display_order = [n for n in self._display_order if n != name]

    def set_identifiers_order(self, names: list[str]) -> None:
        self._display_order = list(names)
        self._identifiers = [n for n in names if n not in self._temporary]

    def all_assets(self) -> list[dict]:
        return list(self._assets)

    def toggle_temp_member(self, name: str, image_path: str) -> bool:
        members = self._temp_members.setdefault(name, set())
        if image_path in members:
            members.discard(image_path)
            return False
        members.add(image_path)
        return True

    def temp_members(self, name: str) -> set[str]:
        return set(self._temp_members.get(name, set()))

    def remove_temp_member(self, name: str, image_path: str) -> None:
        self._temp_members.get(name, set()).discard(image_path)

    def all_temp_member_names_for(self, image_path: str) -> list[str]:
        return [
            n for n, members in self._temp_members.items() if image_path in members
        ]

    def set_identifier_temporary(
        self, name: str, temporary: bool, members: Optional[set[str]] = None
    ) -> None:
        """Flip the persisted<->temporary bookkeeping bit only. The caller is
        responsible for migrating any asset JSON before/after this call."""
        if temporary and name not in self._temporary:
            self._identifiers = [n for n in self._identifiers if n != name]
            self._temporary.add(name)
            self._temp_members[name] = set(members) if members else set()
        elif not temporary and name in self._temporary:
            self._temporary.discard(name)
            self._temp_members.pop(name, None)
            if name not in self._identifiers:
                self._identifiers.append(name)


# ══════════════════════════════════════════════════════════════════════════════
# ██  END DEV-MODE SANDBOX  ████████████████████████████████████████████████████
# ══════════════════════════════════════════════════════════════════════════════


# ── Database ───────────────────────────────────────────────────────────────────


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        # Temporary identifiers live ONLY in this process's memory - never
        # written to the sqlite file or any asset's JSON. They vanish the
        # moment the app closes or this database is reloaded.
        self._temporary: set[str] = set()
        self._temp_members: dict[str, set[str]] = {}
        self._display_order: list[str] = [
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM identifiers ORDER BY sort_order"
            ).fetchall()
        ]

    def _migrate(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                folder      TEXT    NOT NULL DEFAULT '',
                image_path  TEXT    NOT NULL UNIQUE,
                json_path   TEXT    NOT NULL,
                json_data   TEXT    NOT NULL DEFAULT '{}'
            )
        """)
        cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(assets)").fetchall()
        }
        if "folder" not in cols:
            self._conn.execute(
                "ALTER TABLE assets ADD COLUMN folder TEXT NOT NULL DEFAULT ''"
            )
        # Folder-level metadata table (stores F-[Name].json value per folder path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS folder_meta (
                folder_key  TEXT    PRIMARY KEY,
                copy_value  TEXT    NOT NULL DEFAULT ''
            )
        """)
        # Per-database Identifier list (Manage ID / Edit ID feature). "sort_order"
        # drives both the order shown in the Manage Identifiers window AND the
        # order the identifiers are listed in the card's "Edit ID" submenu.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS identifiers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                sort_order  INTEGER NOT NULL
            )
        """)
        self._conn.commit()

    def index(self, folder: Path, full_rebuild: bool = False) -> tuple[int, int]:
        """Blocking index on the *calling* thread's connection. Returns (total, changes)."""
        return _run_index(self.path, folder, full_rebuild, progress_cb=None)

    def search(
        self,
        query: str,
        limit: int = 2000,
        folder_only: bool = False,
        json_only: bool = False,
        id_only: bool = False,
    ) -> list[dict]:
        if id_only:
            # "id-<value>" mode: case-insensitive substring match against the
            # active identifier name(s) - both persisted (stored in the
            # asset's own JSON "Identifier" field) and temporary (tracked only
            # in this Database instance's memory) count equally here.
            q = query.lower()
            rows = self._conn.execute(
                "SELECT * FROM assets ORDER BY folder, name"
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                try:
                    data = json.loads(d.get("json_data", "{}"))
                except Exception:
                    data = {}
                tokens = [
                    t.strip()
                    for t in str(data.get("Identifier", "") or "").split(",")
                    if t.strip()
                ]
                tokens.extend(
                    name
                    for name, members in self._temp_members.items()
                    if d.get("image_path", "") in members
                )
                ident = ", ".join(tokens).lower()
                if q in ident:
                    results.append(d)
            return results[:limit]
        q = f"%{query}%"
        if json_only:
            # Search the raw JSON text itself (values, keys, anything) rather
            # than just the asset name. Triggered by a leading ';' in the UI.
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE json_data LIKE ? ORDER BY folder, name LIMIT ?",
                (q, limit),
            ).fetchall()
        elif folder_only:
            # Return all assets that belong to folders whose relative path contains
            # the query string (covers both shallow and nested folder names).
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE folder LIKE ? ORDER BY folder, name LIMIT ?",
                (q, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM assets WHERE name LIKE ? ORDER BY folder, name LIMIT ?",
                (q, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_folder_meta(self, folder_key: str) -> Optional[str]:
        """Return the copy_value for a folder if an F-[Name].json was indexed, else None."""
        row = self._conn.execute(
            "SELECT copy_value FROM folder_meta WHERE folder_key=?", (folder_key,)
        ).fetchone()
        return row[0] if row else None

    def update_json(self, image_path: str, new_json_text: str) -> None:
        self._conn.execute(
            "UPDATE assets SET json_data=? WHERE image_path=?",
            (new_json_text, image_path),
        )
        self._conn.commit()

    # ── Identifiers (Manage ID / Edit ID) ───────────────────────────────
    # Per-database list of identifier strings. Order matters: it drives both
    # the Manage Identifiers window and the card's "Edit ID" submenu.

    def get_identifiers(self) -> list[str]:
        return list(self._display_order)

    def is_temporary(self, name: str) -> bool:
        return name in self._temporary

    def _next_sort_order(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM identifiers"
        ).fetchone()
        return (row[0] if row and row[0] is not None else -1) + 1

    def add_identifier(self, name: str, temporary: bool = False) -> None:
        if temporary:
            self._temporary.add(name)
            self._temp_members.setdefault(name, set())
        else:
            self._conn.execute(
                "INSERT INTO identifiers(name, sort_order) VALUES(?,?)",
                (name, self._next_sort_order()),
            )
            self._conn.commit()
        if name not in self._display_order:
            self._display_order.append(name)

    def rename_identifier(self, old_name: str, new_name: str) -> None:
        if old_name in self._temporary:
            self._temporary.discard(old_name)
            self._temporary.add(new_name)
            self._temp_members[new_name] = self._temp_members.pop(old_name, set())
        else:
            self._conn.execute(
                "UPDATE identifiers SET name=? WHERE name=?", (new_name, old_name)
            )
            self._conn.commit()
        self._display_order = [
            new_name if n == old_name else n for n in self._display_order
        ]

    def remove_identifier(self, name: str) -> None:
        if name in self._temporary:
            self._temporary.discard(name)
            self._temp_members.pop(name, None)
        else:
            self._conn.execute("DELETE FROM identifiers WHERE name=?", (name,))
            self._conn.commit()
        self._display_order = [n for n in self._display_order if n != name]

    def set_identifiers_order(self, names: list[str]) -> None:
        """Persist a full reordering. `names` must contain every identifier
        (temporary ones included, though only the persisted subset is
        actually written to sqlite - temporary order lives in memory only,
        captured via `_display_order`)."""
        self._display_order = list(names)
        persisted = [n for n in names if n not in self._temporary]
        self._conn.executemany(
            "UPDATE identifiers SET sort_order=? WHERE name=?",
            [(i, n) for i, n in enumerate(persisted)],
        )
        self._conn.commit()

    def all_assets(self) -> list[dict]:
        """Every indexed asset, uncapped (unlike search()'s default limit)."""
        rows = self._conn.execute(
            "SELECT * FROM assets ORDER BY folder, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def toggle_temp_member(self, name: str, image_path: str) -> bool:
        """Flip membership of image_path in temporary identifier `name`.
        Returns the new active state (True=added, False=removed)."""
        members = self._temp_members.setdefault(name, set())
        if image_path in members:
            members.discard(image_path)
            return False
        members.add(image_path)
        return True

    def temp_members(self, name: str) -> set[str]:
        return set(self._temp_members.get(name, set()))

    def remove_temp_member(self, name: str, image_path: str) -> None:
        self._temp_members.get(name, set()).discard(image_path)

    def all_temp_member_names_for(self, image_path: str) -> list[str]:
        """All temporary identifier names currently active on this asset."""
        return [
            n for n, members in self._temp_members.items() if image_path in members
        ]

    def set_identifier_temporary(
        self, name: str, temporary: bool, members: Optional[set[str]] = None
    ) -> None:
        """Flip the persisted<->temporary bookkeeping bit only (sqlite row vs.
        in-memory set). The caller (ManageIdentifiersDialog) is responsible
        for migrating each affected asset's JSON before/after this call -
        this method only knows about the `identifiers` table/memory, not
        asset files."""
        if temporary and name not in self._temporary:
            self._conn.execute("DELETE FROM identifiers WHERE name=?", (name,))
            self._conn.commit()
            self._temporary.add(name)
            self._temp_members[name] = set(members) if members else set()
        elif not temporary and name in self._temporary:
            self._temporary.discard(name)
            self._temp_members.pop(name, None)
            self._conn.execute(
                "INSERT INTO identifiers(name, sort_order) VALUES(?,?)",
                (name, self._next_sort_order()),
            )
            self._conn.commit()

    def count(self) -> int:
        """Return the total number of indexed assets."""
        return self._conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]

    def delete(self, image_path: str) -> None:
        self._conn.execute("DELETE FROM assets WHERE image_path=?", (image_path,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ── Database Manager ───────────────────────────────────────────────────────────


class DatabaseManager:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._roots_file = APP_DIR / "roots.json"
        self._roots: dict[str, Path] = {}
        self._dbs: dict[str, Database] = {}
        self._load()

    def _load(self) -> None:
        if self._roots_file.exists():
            try:
                data = json.loads(self._roots_file.read_text(encoding="utf-8"))
                self._roots = {k: Path(v) for k, v in data.items()}
            except Exception:
                pass

    def _save(self) -> None:
        self._roots_file.write_text(
            json.dumps({k: str(v) for k, v in self._roots.items()}, indent=2),
            encoding="utf-8",
        )

    def names(self) -> list[str]:
        return sorted(self._roots.keys())

    def root_for(self, name: str) -> Optional[Path]:
        return self._roots.get(name)

    def get(self, name: str) -> Optional[Database]:
        if name not in self._roots:
            return None
        if name not in self._dbs:
            self._dbs[name] = Database(APP_DIR / f"{name}.db")
        return self._dbs[name]

    def unload(self, name: str) -> None:
        """Close and remove a loaded database from memory without deleting it."""
        db = self._dbs.pop(name, None)
        if db:
            db.close()

    def add_folder(self, folder: Path) -> str:
        name = folder.name
        base, n = name, 1
        while name in self._roots and self._roots[name] != folder:
            name = f"{base}_{n}"
            n += 1
        self._roots[name] = folder
        self._save()
        return name

    def remove(self, name: str) -> None:
        """Remove a database entry and delete its .db file if it exists."""
        # Close and unload from memory first
        db = self._dbs.pop(name, None)
        if db:
            db.close()
        # Delete the .db file and its cache if they exist
        db_file = APP_DIR / f"{name}.db"
        try:
            if db_file.exists():
                db_file.unlink()
        except Exception:
            pass
        try:
            cf = _cache_path(name)
            if cf.exists():
                cf.unlink()
        except Exception:
            pass
        # Remove from roots registry and persist
        self._roots.pop(name, None)
        self._save()

    def close_all(self) -> None:
        for db in self._dbs.values():
            db.close()
        self._dbs.clear()


# ── Loading Overlay ────────────────────────────────────────────────────────────


class LoadingOverlay(QWidget):
    """
    A full-panel overlay that shows a spinner animation and a progress message.
    Parent it to the ResultsPanel (or any widget) and call show()/hide().
    """

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setObjectName("loadingOverlay")

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(12)

        self._dots_lbl = QLabel("◌")
        self._dots_lbl.setObjectName("loadingDots")
        self._dots_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._msg_lbl = QLabel("Loading...")
        self._msg_lbl.setObjectName("loadingMsg")
        self._msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lay.addWidget(self._dots_lbl)
        lay.addWidget(self._msg_lbl)

        self._frame = 0
        self._frames = ["◜", "◝", "◞", "◟"]
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(120)

        self.hide()

    def set_message(self, msg: str) -> None:
        self._msg_lbl.setText(msg)

    def _tick(self) -> None:
        self._dots_lbl.setText(self._frames[self._frame % len(self._frames)])
        self._frame += 1

    def resizeEvent(self, event) -> None:
        # Always fill parent
        p = self.parent()
        if isinstance(p, QWidget):
            self.setGeometry(p.rect())
        super().resizeEvent(event)

    def showEvent(self, event) -> None:
        p = self.parent()
        if isinstance(p, QWidget):
            self.setGeometry(p.rect())
        super().showEvent(event)


# ── Index Worker ───────────────────────────────────────────────────────────────


class IndexWorker(QThread):
    """Indexes a folder in a background thread using its own SQLite connection."""

    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(int)  # final total

    def __init__(
        self,
        db_path: Path,
        folder: Path,
        full_rebuild: bool = False,
        flagged: Optional[tuple[set[str], set[str], set[str]]] = None,
    ):
        super().__init__()
        self._db_path = db_path
        self._folder = folder
        self._full_rebuild = full_rebuild
        self._flagged = flagged  # (added, changed, deleted) or None for full scan

    def run(self) -> None:
        def _cb(current, total, msg):
            self.progress.emit(current, total, msg)

        total, _ = _run_index(
            self._db_path, self._folder, self._full_rebuild, _cb, self._flagged
        )
        self.finished.emit(total)


# ── Thumbnail Card ─────────────────────────────────────────────────────────────


class _DraggableDialog(QDialog):
    """Base for frameless dialogs that are draggable and remember position.

    Position memory is session-only (stored in _SESSION_POS, never on disk).
    On the first open after each app start the dialog centres itself over its
    parent window; after the user drags it, the new position is remembered for
    the rest of that session.
    """

    _PREFS_KEY: str = ""  # subclasses set this

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos: Optional[QPoint] = None

    def _center_on_parent(self) -> None:
        """Move this dialog to the centre of its parent's current geometry."""
        parent = self.parent()
        if parent is not None:
            # Use the top-level window's *current* frame geometry so we follow
            # the window even if the user has dragged it to another monitor.
            center = parent.window().frameGeometry().center()
        else:
            center = QApplication.primaryScreen().availableGeometry().center()
        fg = self.frameGeometry()
        fg.moveCenter(center)
        self.move(fg.topLeft())

    def _restore_pos(self) -> None:
        """Use session memory position, or centre on parent if not yet moved."""
        key = self._PREFS_KEY
        pos = _SESSION_POS.get(key) if key else None
        if pos and len(pos) == 2:
            self.move(pos[0], pos[1])
        else:
            self._center_on_parent()

    def _save_pos(self) -> None:
        """Remember position in session memory (never written to disk)."""
        key = self._PREFS_KEY
        if not key:
            return
        _SESSION_POS[key] = [self.x(), self.y()]

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None
        self._save_pos()
        super().mouseReleaseEvent(event)


class EditJsonDialog(_DraggableDialog):
    """Frameless dialog to view and edit the JSON file linked to a card."""

    _PREFS_KEY = "edit_json_pos"
    W, H = 500, 460

    def __init__(
        self,
        asset: dict,
        db: Optional[Database],
        parent=None,
        focus_key: str = "",
        focus_value: str = "",
    ):
        super().__init__(parent)
        self._asset = asset
        self._db = db
        self._focus_key = focus_key
        self._focus_value = focus_value
        self.setWindowTitle("Edit JSON")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Shadow container - transparent, just for the drop-shadow effect
        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Accent header bar (matches status bar style) ──────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Edit JSON")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        # ── Editor area with inset border ─────────────────────────────────
        editor_wrap = QWidget()
        editor_wrap.setObjectName("editEditorWrap")
        ew_lay = QVBoxLayout(editor_wrap)
        ew_lay.setContentsMargins(10, 8, 10, 6)

        self._editor = QPlainTextEdit()
        self._editor.setObjectName("jsonEditor")

        # Load raw JSON - in dev mode the json_path is a fake sentinel, so skip disk
        json_path = asset.get("json_path", "")
        raw = ""
        if not DEV_MODE and json_path and Path(json_path).exists():
            try:
                raw = Path(json_path).read_text(encoding="utf-8", errors="ignore")
            except Exception:
                raw = asset.get("json_data", "{}")
        else:
            raw = asset.get("json_data", "{}")

        try:
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            pass

        self._editor.setPlainText(raw)
        ew_lay.addWidget(self._editor)
        lay.addWidget(editor_wrap, 1)

        # ── Footer: cancel left, save right ──────────────────────────────
        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._save)

        self._restore_pos()

        # Scroll to and subtly highlight the focused key after the dialog paints
        if focus_key:
            QTimer.singleShot(0, self._apply_focus_highlight)

    def _apply_focus_highlight(self) -> None:
        from PySide6.QtGui import QTextCharFormat, QTextCursor

        doc = self._editor.document()
        # Search for the key as it appears in pretty-printed JSON: "key":
        search = f'"{self._focus_key}":'

        # Duplicate keys can appear multiple times in the document (e.g. two
        # different entries both named "cat"). Walk every match and prefer
        # the one whose line also contains our specific value, so we land on
        # the exact entry that was clicked instead of always the first hit.
        found = QTextCursor()
        fallback = QTextCursor()
        cursor = QTextCursor(doc)
        while True:
            cursor = doc.find(search, cursor)
            if cursor.isNull():
                break
            if fallback.isNull():
                fallback = QTextCursor(cursor)
            if self._focus_value:
                line_cursor = QTextCursor(cursor)
                line_cursor.movePosition(
                    QTextCursor.MoveOperation.EndOfLine,
                    QTextCursor.MoveMode.KeepAnchor,
                )
                line_text = line_cursor.selectedText()
                if self._focus_value in line_text:
                    found = cursor
                    break

        if found.isNull():
            found = fallback
        if found.isNull():
            return

        # Extend selection to the end of that line to cover the value too
        line_end = QTextCursor(found)
        line_end.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )

        # Scroll editor to show this line, centred in the viewport
        self._editor.setTextCursor(line_end)
        self._editor.centerCursor()
        # Move cursor to start of match so we don't leave a huge selection visible
        plain_cursor = QTextCursor(found)
        plain_cursor.clearSelection()
        self._editor.setTextCursor(plain_cursor)

        # Build the extra selection (amber-tinted background, no border noise)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(200, 170, 80, 38))

        sel = QTextEdit.ExtraSelection()
        sel.cursor = line_end
        sel.format = fmt
        self._editor.setExtraSelections([sel])

        # Clear highlight on first interaction (key press or click)
        def _clear_highlight() -> None:
            self._editor.setExtraSelections([])
            try:
                self._editor.cursorPositionChanged.disconnect(_clear_highlight)
            except RuntimeError:
                pass

        self._editor.cursorPositionChanged.connect(_clear_highlight)

    def _save(self) -> None:
        new_text = self._editor.toPlainText().strip()
        try:
            json.loads(new_text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, APP_NAME, f"Invalid JSON:\n{e}")
            return

        # ── DEV MODE: skip ALL disk I/O; only update the in-memory stub ───────
        if not DEV_MODE:
            json_path = self._asset.get("json_path", "")
            if json_path:
                try:
                    Path(json_path).write_text(new_text, encoding="utf-8")
                except Exception as exc:
                    QMessageBox.critical(
                        self, APP_NAME, f"Could not write file:\n{exc}"
                    )
                    return

        if self._db:
            self._db.update_json(self._asset["image_path"], new_text)

        self.accept()


class AddTagDialog(_DraggableDialog):
    """Frameless dialog for adding a tag to a folder or an entry."""

    _PREFS_KEY = "add_tag_pos"
    W, H = 360, 148

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tag_text: str = ""
        self.setWindowTitle("Add Tag")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Add Tag")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body: single text field ───────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(0)

        self._tag_edit = QLineEdit()
        self._tag_edit.setObjectName("scriptArgsEdit")
        self._tag_edit.setPlaceholderText("Enter tag...")
        self._tag_edit.setFixedHeight(24)
        self._tag_edit.returnPressed.connect(self._accept)
        b_lay.addWidget(self._tag_edit)
        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer: Save (left), Cancel (right) ───────────────────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        save_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)

        self._restore_pos()
        QTimer.singleShot(0, self._tag_edit.setFocus)

    def _accept(self) -> None:
        self.tag_text = self._tag_edit.text().strip()
        self.accept()


def _default_identifier_name(existing: list[str]) -> str:
    """Smallest-unused-N default name ('Identifier 1', 'Identifier 2', ...)
    used whenever an identifier is created without a name typed in."""
    existing_set = set(existing)
    n = 1
    while f"Identifier {n}" in existing_set:
        n += 1
    return f"Identifier {n}"


class _IdentifierMenuRow(QWidget):
    """One row in the card's 'Edit ID' submenu.

    A plain widget (not a QAction) so that clicking it toggles the identifier
    and repaints its dot without the QMenu chain closing - this is what makes
    selecting several identifiers in one right-click possible. The click is
    fully consumed here; the menu only closes when the user clicks elsewhere,
    presses Escape, or picks a real QAction like "Edit JSON...".
    """

    def __init__(self, name: str, active: bool, on_toggle, parent=None):
        super().__init__(parent)
        self._name = name
        self._active = active
        self._on_toggle = on_toggle
        self.setFixedHeight(22)
        self.setMinimumWidth(140)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 16, 0)
        lay.setSpacing(8)

        self._dot = QLabel()
        self._dot.setFixedSize(6, 6)
        lay.addWidget(self._dot)

        self._label = QLabel(name)
        self._label.setStyleSheet("color: rgba(220,220,220,0.88); font-size: 11px;")
        lay.addWidget(self._label)
        lay.addStretch()

        self._update_dot()

    def _update_dot(self) -> None:
        if self._active:
            self._dot.setStyleSheet(
                "background-color: rgba(232,184,75,0.9); border-radius: 3px;"
            )
        else:
            self._dot.setStyleSheet("background: transparent;")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._active = not self._active
            self._update_dot()
            self._on_toggle(self._name)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ThumbnailCard(QWidget):
    deleted = Signal(str)
    edited = Signal(str)       # emitted after a successful JSON edit (image_path)
    view_requested = Signal(object)  # emitted on left-click: passes self

    CARD_W = THUMB_W
    CARD_H = THUMB_H + 4 + NAME_H

    def __init__(
        self,
        asset: dict,
        db: Optional[Database] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.asset = asset
        self._db = db
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hovered = False
        self._drag_start_pos: Optional[QPoint] = None
        self._show_tagged_mode = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("cardImage")
        self._img_lbl.setFixedSize(THUMB_W, THUMB_H)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setScaledContents(False)
        lay.addWidget(self._img_lbl)

        self._name_lbl = QLabel(asset["name"])
        self._name_lbl.setObjectName("cardName")
        self._name_lbl.setFixedHeight(NAME_H)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setMaximumWidth(THUMB_W)
        lay.addWidget(self._name_lbl)

        self._image_loaded = False

        # Connect once to the global worker so pixmaps arrive on the main thread
        PIXMAP_WORKER.pixmap_ready.connect(self._on_pixmap_ready)

    def request_image(self) -> None:
        """Called by FolderSection when the folder is expanded.  No-op if already loaded."""
        if self._image_loaded:
            return
        path = self.asset.get("image_path", "")
        if not path:
            return
        # If the pixmap is already cached (e.g. same folder reopened), apply instantly
        if path in _PIXMAP_CACHE:
            self._apply_pixmap_data(_PIXMAP_CACHE[path])
        else:
            PIXMAP_WORKER.submit(self, path)

    def _on_pixmap_ready(self, card: "ThumbnailCard", pix: QPixmap) -> None:
        """Slot called on the main thread when the worker finishes a pixmap."""
        if card is not self:
            return
        self._apply_pixmap_data(pix)
        # Disconnect to avoid accumulating dead connections after many refreshes
        try:
            PIXMAP_WORKER.pixmap_ready.disconnect(self._on_pixmap_ready)
        except RuntimeError:
            pass

    def _apply_pixmap_data(self, pix: QPixmap) -> None:
        """Render the rounded thumbnail from an already-scaled pixmap."""
        from PySide6.QtGui import QPainterPath

        self._image_loaded = True
        if not pix.isNull():
            rounded = QPixmap(THUMB_W, THUMB_H)
            rounded.fill(Qt.GlobalColor.transparent)
            p = QPainter(rounded)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, THUMB_W, THUMB_H, 7, 7)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            self._img_lbl.setPixmap(rounded)
        else:
            self._img_lbl.setText("?")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (
            self._drag_start_pos is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            dist = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start_pos is not None:
            # Released without triggering a drag → open viewer
            self._drag_start_pos = None
            image_path = self.asset.get("image_path", "")
            if image_path:
                self.view_requested.emit(self)
        super().mouseReleaseEvent(event)

    def _start_drag(self) -> None:
        image_path = self.asset.get("image_path", "")
        if not image_path:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(image_path)])
        drag = QDrag(self)
        drag.setMimeData(mime)
        # Use the thumbnail as the drag pixmap so the user sees what they're dragging
        pix = _load_pixmap(image_path)
        if not pix.isNull():
            drag.setPixmap(
                pix.scaled(
                    THUMB_W // 2,
                    THUMB_H // 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        drag.exec(Qt.DropAction.CopyAction)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        self._update_name_highlight()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        self._update_name_highlight()
        super().leaveEvent(event)

    # ── "Show Tagged" mode ──────────────────────────────────────────────
    #
    # When active, every visible card whose image already has a tag gets its
    # name label highlighted with a neutral white-gray tone, brightening
    # slightly on hover. Untagged images are left untouched. This is purely
    # cosmetic/text-level and does not affect the thumbnail itself.

    def is_tagged(self) -> bool:
        """True if this asset's json_data has a non-empty 'tags' value."""
        try:
            data = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            return False
        return bool(str(data.get("tags", "")).strip())

    def set_tagged_mode(self, enabled: bool) -> None:
        self._show_tagged_mode = enabled
        self._update_name_highlight()

    def _update_name_highlight(self) -> None:
        if not self._show_tagged_mode or not self.is_tagged():
            self._name_lbl.setStyleSheet("")
            return
        bg = "rgba(255,255,255,0.30)" if self._hovered else "rgba(255,255,255,0.15)"
        self._name_lbl.setStyleSheet(
            f"background-color: {bg}; border-radius: 3px; color: rgba(240,240,240,0.95);"
        )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._hovered:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Minimal hover: a faint neutral border around the image...
            pen = QPen(QColor(255, 255, 255, 45))
            pen.setWidth(1)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(1, 1, THUMB_W - 2, THUMB_H - 2, 7, 7)

            # ...and a subtle tint behind the name so it reads as "selected".
            name_y = THUMB_H + 4
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(0, name_y, self.CARD_W, NAME_H, 3, 3)

            p.end()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setObjectName("cardMenu")

        try:
            data: dict = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            data = {}

        # ── LoRA handling ─────────────────────────────────────────────────
        # Only show a small "LORA" badge when lora=true.
        # Nothing is shown for lora=false or when the key is absent.
        # The "lora" key is always excluded from the copy actions.
        if data.get("lora") is True:
            lora_lbl = QLabel("LORA")
            lora_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lora_lbl.setStyleSheet(
                "color: rgba(232,184,75,0.85); font-size: 8px; font-weight: 700;"
                " letter-spacing: 1px; padding: 2px 0px 1px 0px; background: transparent;"
            )
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(lora_lbl)
            menu.addAction(wa)

        # Copy actions - always shown, but "lora" and "Identifier" keys are excluded
        # (case-insensitive, since "lora" is lowercase and "Identifier" is
        # capitalized in practice, but either casing should still be excluded).
        _EXCLUDED_COPY_KEYS = {"lora", "identifier"}
        copy_data = {
            k: v for k, v in data.items() if k.lower() not in _EXCLUDED_COPY_KEYS
        }

        # "Copy Name" copies the asset's filename without its extension. Both
        # the .png and its sibling .json share this same stem, so either works.
        name_stem = Path(self.asset.get("image_path", "")).stem
        if name_stem:
            copy_name_act = menu.addAction("Copy Name")
            copy_name_act.setData(name_stem)
            menu.addSeparator()

        for key, value in copy_data.items():
            text = str(value).strip()
            if not text:
                continue
            label = "Copy " + " ".join(w.capitalize() for w in key.split("_"))
            act = menu.addAction(label)
            act.setData(text)

        if copy_data:
            menu.addSeparator()

        add_tag_act = menu.addAction("Add Tag")
        menu.addSeparator()

        # ── Edit ID submenu ──────────────────────────────────────────────
        # Hover expands the list of this database's Identifiers, in the exact
        # order configured in "Manage Identifiers". Clicking one toggles it on
        # this asset without closing the menu (multi-select). Active
        # identifiers are marked with an orange dot. Nothing here ever emits
        # `edited` / triggers a grid reload - toggling an identifier changes
        # no visible pixel on the card itself, so there's nothing to redraw.
        id_menu = menu.addMenu("Edit ID")
        add_identifier_act = self._build_edit_id_menu(id_menu)

        edit_act = menu.addAction("Edit JSON...")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is edit_act:
            dlg = EditJsonDialog(self.asset, self._db, self)
            if dlg.exec():
                # Refresh asset json_data from db so copies are updated
                self.edited.emit(self.asset["image_path"])
        elif chosen is add_tag_act:
            self._add_tag()
        elif chosen is add_identifier_act:
            self._add_identifier_from_menu()
        elif chosen.data():
            QApplication.clipboard().setText(chosen.data())

    def _build_edit_id_menu(self, id_menu: QMenu) -> Optional[QAction]:
        """Populate the 'Edit ID' submenu with one toggleable row per
        Identifier configured for the active database, in Manage Identifiers
        order. Rows consume their own clicks so the menu stays open, letting
        the user select/deselect multiple identifiers in one go.

        The rows sit inside a small QScrollArea capped at 6 visible rows (any
        more and it scrolls, using the app's minimal scrollbar) so the menu
        never grows unmanageably tall. A fixed "+ Add Identifier" action
        always sits at the very bottom, below a separator, outside the
        scrollable region so it's always reachable. Returns that action so
        the caller can detect it being chosen."""
        if self._db is not None:
            identifiers = self._db.get_identifiers()
            if identifiers:
                active = self._get_active_identifier_names()
                rows_container = QWidget()
                rows_lay = QVBoxLayout(rows_container)
                rows_lay.setContentsMargins(0, 2, 0, 2)
                rows_lay.setSpacing(0)
                for name in identifiers:
                    row = _IdentifierMenuRow(
                        name, name in active, self._toggle_identifier
                    )
                    rows_lay.addWidget(row)

                scroll = QScrollArea()
                scroll.setObjectName("idMenuScroll")
                scroll.setWidget(rows_container)
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.Shape.NoFrame)
                scroll.setStyleSheet("background: transparent;")
                scroll.setHorizontalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                )
                scroll.setVerticalScrollBarPolicy(
                    Qt.ScrollBarPolicy.ScrollBarAsNeeded
                )
                row_h = 22
                visible_count = min(len(identifiers), 6)
                scroll.setFixedHeight(row_h * visible_count)
                scroll.setMinimumWidth(160)

                scroll_wa = QWidgetAction(id_menu)
                scroll_wa.setDefaultWidget(scroll)
                id_menu.addAction(scroll_wa)
            else:
                empty_lbl = QLabel("No identifiers yet")
                empty_lbl.setStyleSheet(
                    "color: rgba(200,200,200,0.45); font-size: 11px;"
                    " padding: 4px 12px; background: transparent;"
                )
                empty_wa = QWidgetAction(id_menu)
                empty_wa.setDefaultWidget(empty_lbl)
                id_menu.addAction(empty_wa)
        id_menu.addSeparator()
        return id_menu.addAction("+ Add Identifier")

    def _get_active_identifiers(self) -> list[str]:
        """Persisted identifiers currently set on this asset's 'Identifier'
        JSON field (does not include temporary identifiers - see
        `_get_active_identifier_names` for the combined view)."""
        try:
            data = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            data = {}
        raw = str(data.get("Identifier", "") or "")
        if not raw.strip():
            return []
        return [tok.strip() for tok in raw.split(",") if tok.strip()]

    def _get_active_identifier_names(self) -> set[str]:
        """Every identifier - persisted or temporary - currently active on
        this asset. Used to light up the dot in the Edit ID submenu."""
        names = set(self._get_active_identifiers())
        if self._db is not None:
            names.update(
                self._db.all_temp_member_names_for(self.asset.get("image_path", ""))
            )
        return names

    def _toggle_identifier(self, name: str) -> None:
        """Add/remove `name` from this asset.

        Temporary identifiers never touch the JSON at all - membership just
        flips a bit in the Database instance's in-memory set. Persisted
        identifiers keep the original behaviour: written into the comma-
        separated 'Identifier' field of the asset's own JSON (and its sidecar
        .json file on disk), immediately, without emitting `edited` (nothing
        visible on the card changes either way).
        """
        if self._db is not None and self._db.is_temporary(name):
            self._db.toggle_temp_member(name, self.asset.get("image_path", ""))
            return

        try:
            data = json.loads(self.asset.get("json_data", "{}"))
        except Exception:
            data = {}

        current = self._get_active_identifiers()
        if name in current:
            current = [c for c in current if c != name]
        else:
            current.append(name)

        if current:
            data["Identifier"] = ", ".join(current)
        else:
            data.pop("Identifier", None)
        new_text = json.dumps(data, indent=2, ensure_ascii=False)

        if DEV_MODE:
            self.asset["json_data"] = new_text
            if self._db:
                self._db.update_json(self.asset["image_path"], new_text)
            return

        json_path = self.asset.get("json_path", "")
        if json_path:
            try:
                Path(json_path).write_text(new_text, encoding="utf-8")
            except Exception as exc:
                QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
                return

        if self._db:
            self._db.update_json(self.asset["image_path"], new_text)
        self.asset["json_data"] = new_text

    def _add_identifier_from_menu(self) -> None:
        """'+ Add Identifier' row at the bottom of the card's Edit ID
        submenu - lets the user create a new Identifier (optionally
        temporary) without opening Manage ID."""
        if self._db is None:
            return
        dlg = AddIdentifierDialog(self)
        if not dlg.exec():
            return
        name = dlg.identifier_text.strip() or _default_identifier_name(
            self._db.get_identifiers()
        )
        self._db.add_identifier(name, temporary=dlg.temporary)

    def _add_tag(self) -> None:
        dlg = AddTagDialog(self)
        if not dlg.exec():
            return
        tag = dlg.tag_text
        if not tag:
            return

        if DEV_MODE:
            # In dev mode just update in-memory json_data
            try:
                data = json.loads(self.asset.get("json_data", "{}"))
            except Exception:
                data = {}
            existing = data.get("tags", "")
            data["tags"] = f"{existing}, {tag}" if existing else tag
            new_text = json.dumps(data, indent=2, ensure_ascii=False)
            self.asset["json_data"] = new_text
            if self._db:
                self._db.update_json(self.asset["image_path"], new_text)
            self.edited.emit(self.asset["image_path"])
            return

        json_path = self.asset.get("json_path", "")
        if not json_path:
            return
        try:
            raw = (
                Path(json_path).read_text(encoding="utf-8", errors="ignore")
                if Path(json_path).exists()
                else "{}"
            )
            data = json.loads(raw)
        except Exception:
            data = {}

        existing = data.get("tags", "")
        data["tags"] = f"{existing}, {tag}" if existing else tag
        new_text = json.dumps(data, indent=2, ensure_ascii=False)

        try:
            Path(json_path).write_text(new_text, encoding="utf-8")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
            return

        if self._db:
            self._db.update_json(self.asset["image_path"], new_text)
        self.asset["json_data"] = new_text
        self.edited.emit(self.asset["image_path"])


# ── Folder Section ─────────────────────────────────────────────────────────────


class FolderSection(QWidget):
    card_deleted = Signal(str)
    card_edited = Signal(str)
    folder_tagged = Signal(str)   # emitted when a folder tag is written (folder_key)
    card_view_requested = Signal(object, list)  # (card, ordered_cards_in_folder)

    def __init__(
        self,
        title: str,
        depth: int = 0,
        copy_value: str = "",
        folder_key: str = "",
        root_folder: Optional[Path] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        # Without an objectName, this widget only matches the blanket
        # "QWidget { background: ... }" rule in the app stylesheet, which
        # paints it fully opaque -- every row would then hide the
        # #resultsCanvas gradient behind it. Naming it lets the stylesheet
        # explicitly make it transparent (see #folderSection rule).
        self.setObjectName("folderSection")
        # Force styled-background painting explicitly rather than relying on
        # Qt's implicit "any QWidget selector enables it for everyone" trick,
        # which doesn't reliably apply to every custom QWidget subclass.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._expanded = False
        self._depth = depth
        self._child_sections: list[FolderSection] = []
        self._copy_value = copy_value
        self._folder_key = folder_key
        self._root_folder = root_folder

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        header_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_container.installEventFilter(self)
        self._header_wrap = header_container
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(4)

        self._title = title
        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setProperty("depth0", "true" if depth == 0 else "false")
        self._header.setProperty("expanded", "false")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(26 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(
            lambda pos: self._on_header_context_menu(self._header.mapToGlobal(pos))
        )

        hc_lay.addWidget(self._header, 0)

        # Orange dot shown (only in "Show Tagged" mode) when every image in
        # this folder, including images in every nested subfolder, is tagged.
        self._tag_dot = QLabel("●")
        self._tag_dot.setObjectName("folderTagDot")
        self._tag_dot.setFixedWidth(10)
        self._tag_dot.hide()
        hc_lay.addWidget(self._tag_dot, 0)

        # Copy button - only created when this folder has an !F-*.json
        self._copy_btn: Optional[QToolButton] = None
        if copy_value:
            btn = QToolButton()
            btn.setText("Copy")
            btn.setObjectName("folderCopyBtn")
            btn.setToolTip("Copy Folder Tag")
            btn.setVisible(False)  # shown for as long as the folder is expanded
            btn.setFixedHeight(16)
            btn.adjustSize()
            btn.clicked.connect(self._copy_meta_value)
            hc_lay.addWidget(btn)
            self._copy_btn = btn

        hc_lay.addStretch()
        outer.addWidget(header_container)

        # ── Body ─────────────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        self._card_widget = QWidget()
        self._card_widget.setObjectName("cardGrid")
        self._card_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

        self._cards: list[ThumbnailCard] = []
        self._current_cols: int = COLS
        self._db_ref: Optional[Database] = None
        self._show_tagged: bool = False

    def set_db(self, db: Optional[Database]) -> None:
        self._db_ref = db

    def add_card(self, asset: dict, db: Optional[Database] = None) -> None:
        card = ThumbnailCard(asset, db or self._db_ref)
        card.deleted.connect(self.card_deleted)
        card.edited.connect(self.card_edited)
        card.view_requested.connect(self._on_card_view_requested)
        # Add to grid immediately using a reasonable default col count;
        # actual layout is corrected when the section is expanded/shown.
        i = len(self._cards)
        self._cards.append(card)
        self._card_grid.addWidget(card, i // COLS, i % COLS)
        card.set_tagged_mode(self._show_tagged)
        self._update_tag_dot()

    def _on_card_view_requested(self, card: ThumbnailCard) -> None:
        """Relay view request with the ordered card list of this folder."""
        self.card_view_requested.emit(card, list(self._cards))

    def _relayout_cards(self) -> None:
        """Re-flow cards into the grid based on current available width."""
        avail_w = self._card_widget.width()
        if avail_w < THUMB_W:
            return  # not laid out yet, skip
        cols = max(1, avail_w // (THUMB_W + 6))

        # Only rebuild if column count changed
        if cols == self._current_cols:
            return
        self._current_cols = cols

        # takeAt removes from the layout but does NOT reparent the widget
        while self._card_grid.count():
            self._card_grid.takeAt(0)

        for i, card in enumerate(self._cards):
            self._card_grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Guard: only reflow when body is visible and we actually have cards
        if self._expanded and self._cards:
            self._relayout_cards()

    def add_child_section(self, sec: "FolderSection") -> None:
        self._child_sections.append(sec)
        sec.card_deleted.connect(self.card_deleted)
        sec.card_edited.connect(self.card_edited)
        sec.folder_tagged.connect(self.folder_tagged)
        sec.card_view_requested.connect(self.card_view_requested)
        self._body_lay.addWidget(sec)
        sec.set_tagged_mode(self._show_tagged)
        self._update_tag_dot()

    def has_cards(self) -> bool:
        return len(self._cards) > 0

    # ── "Show Tagged" mode ──────────────────────────────────────────────

    def set_tagged_mode(self, enabled: bool) -> None:
        """Toggle Show Tagged mode for this folder and everything inside it."""
        self._show_tagged = enabled
        for card in self._cards:
            card.set_tagged_mode(enabled)
        for child in self._child_sections:
            child.set_tagged_mode(enabled)
        self._update_tag_dot()

    def _all_cards(self) -> list["ThumbnailCard"]:
        """Every card in this folder plus every card in nested subfolders."""
        cards = list(self._cards)
        for child in self._child_sections:
            cards.extend(child._all_cards())
        return cards

    def _is_fully_tagged(self) -> bool:
        cards = self._all_cards()
        if not cards:
            return False
        return all(c.is_tagged() for c in cards)

    def _update_tag_dot(self) -> None:
        if self._show_tagged and self._is_fully_tagged():
            self._tag_dot.show()
        else:
            self._tag_dot.hide()

    def _copy_meta_value(self) -> None:
        if self._copy_value:
            QApplication.clipboard().setText(self._copy_value)

    def _update_copy_btn_visibility(self) -> None:
        """Copy pill shows for as long as the folder is expanded, regardless
        of hover state."""
        if self._copy_btn is None:
            return
        self._copy_btn.setVisible(self._expanded)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._header_wrap and event.type() in (
            QEvent.Type.Enter,
            QEvent.Type.Leave,
        ):
            self._update_copy_btn_visibility()
        return super().eventFilter(obj, event)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        self._header.setProperty("expanded", "true" if self._expanded else "false")
        self._header.style().unpolish(self._header)
        self._header.style().polish(self._header)
        self._update_copy_btn_visibility()
        if self._expanded and self._cards:
            self._current_cols = 0  # force reflow on next call
            QTimer.singleShot(0, self._relayout_cards)
            # Kick off background image loading for every card in this folder
            for card in self._cards:
                card.request_image()

    def _on_header_context_menu(self, global_pos) -> None:
        # Context menu only available when folder is expanded
        if not self._expanded:
            return

        menu = QMenu(self)
        menu.setObjectName("cardMenu")

        add_tag_act = menu.addAction("Add Tag")
        menu.addSeparator()
        edit_json_act = menu.addAction("Edit JSON...")
        explorer_act = menu.addAction("Explorer")

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is add_tag_act:
            self._add_folder_tag()
        elif chosen is edit_json_act:
            self._edit_folder_json()
        elif chosen is explorer_act:
            self._open_in_explorer()

    def _get_folder_dir(self) -> Optional[Path]:
        """Return the actual filesystem directory for this folder section."""
        if self._root_folder is None:
            return None
        if self._folder_key:
            return self._root_folder / self._folder_key
        return self._root_folder

    def _add_folder_tag(self) -> None:
        dlg = AddTagDialog(self)
        if not dlg.exec():
            return
        tag = dlg.tag_text
        if not tag:
            return

        if DEV_MODE:
            self.folder_tagged.emit(self._folder_key)
            return

        folder_dir = self._get_folder_dir()
        if folder_dir is None or not folder_dir.exists():
            QMessageBox.warning(self, APP_NAME, "Could not locate folder on disk.")
            return

        # The meta file for a folder named "Foo" is: Foo/!F-Foo.json
        folder_name = folder_dir.name
        meta_filename = f"!F-{folder_name}.json"
        meta_path = folder_dir / meta_filename

        try:
            if meta_path.exists():
                raw = meta_path.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw)
            else:
                data = {}
        except Exception:
            data = {}

        existing = data.get("tags", "")
        data["tags"] = f"{existing}, {tag}" if existing else tag

        try:
            meta_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not write file:\n{exc}")
            return

        # Re-index the folder meta and update copy_value so Copy button appears
        if self._db_ref and self._root_folder:
            try:
                conn = sqlite3.connect(str(self._db_ref.path))
                conn.row_factory = sqlite3.Row
                copy_val = str(next(iter(data.values()))) if data else ""
                conn.execute(
                    "INSERT INTO folder_meta(folder_key, copy_value)"
                    " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                    (self._folder_key, copy_val),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        self.folder_tagged.emit(self._folder_key)

    def _edit_folder_json(self) -> None:
        """Open Edit JSON for the folder's !F-*.json meta file."""
        if DEV_MODE:
            return

        folder_dir = self._get_folder_dir()
        if folder_dir is None or not folder_dir.exists():
            QMessageBox.warning(self, APP_NAME, "Could not locate folder on disk.")
            return

        folder_name = folder_dir.name
        meta_filename = f"!F-{folder_name}.json"
        meta_path = folder_dir / meta_filename

        try:
            if meta_path.exists():
                raw = meta_path.read_text(encoding="utf-8", errors="ignore")
            else:
                raw = "{}"
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            raw = "{}"

        # Build a fake asset dict so EditJsonDialog can work
        fake_asset = {
            "image_path": "",
            "json_path": str(meta_path),
            "json_data": raw,
            "name": meta_filename,
        }
        dlg = EditJsonDialog(fake_asset, None, self)
        if dlg.exec():
            # Re-index folder meta in DB
            if self._db_ref:
                try:
                    new_raw = (
                        meta_path.read_text(encoding="utf-8", errors="ignore")
                        if meta_path.exists()
                        else "{}"
                    )
                    data = json.loads(new_raw)
                    copy_val = str(next(iter(data.values()))) if data else ""
                    conn = sqlite3.connect(str(self._db_ref.path))
                    conn.row_factory = sqlite3.Row
                    conn.execute(
                        "INSERT INTO folder_meta(folder_key, copy_value)"
                        " VALUES(?,?) ON CONFLICT(folder_key) DO UPDATE SET copy_value=excluded.copy_value",
                        (self._folder_key, copy_val),
                    )
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            self.folder_tagged.emit(self._folder_key)

    def _open_in_explorer(self) -> None:
        """Open the folder's location in the OS file explorer."""
        folder_dir = self._get_folder_dir()
        if folder_dir is None or not folder_dir.exists():
            QMessageBox.warning(self, APP_NAME, "Could not locate folder on disk.")
            return
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(folder_dir)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder_dir)])
            else:
                subprocess.Popen(["xdg-open", str(folder_dir)])
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not open Explorer:\n{exc}")


# ── Results Panel ──────────────────────────────────────────────────────────────


class ResultsPanel(QScrollArea):
    card_view_requested = Signal(object, list)  # (card, ordered_cards_in_folder)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("resultsPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setObjectName("resultsContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(6, 8, 6, 8)
        self._layout.setSpacing(5)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)
        # The gradient/"bloom" background lives on the viewport (fixed to the
        # visible area), NOT on self._content -- self._content grows taller
        # every time a folder is expanded, and a growing box would rescale a
        # percentage-based radial-gradient under it, making the background
        # visibly shift/disrupt on every expand/collapse.
        self.viewport().setObjectName("resultsCanvas")
        # Plain QWidgets (which is what QScrollArea's viewport is) only paint
        # a stylesheet background if WA_StyledBackground is set -- without
        # this the #resultsCanvas gradient rule matches but never actually
        # renders, leaving the viewport flat black.
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._db: Optional[Database] = None
        self._root_folder: Optional[Path] = None
        self._az_sort: bool = True
        self._show_tagged: bool = False

        # Loading overlay (child of the viewport so it covers the scroll area)
        self._overlay = LoadingOverlay(self.viewport())

        # ── Middle-mouse autopan ──────────────────────────────────────────
        # Attributes must all exist before installEventFilter, which can
        # immediately trigger eventFilter calls during initialisation.
        self._autopan_active: bool = False
        self._autopan_origin: QPoint = QPoint()
        self._autopan_timer: QTimer = QTimer(self)
        self._autopan_timer.setInterval(16)  # ~60 fps
        self._autopan_timer.timeout.connect(self._autopan_tick)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.viewport():
            t = event.type()
            if t == t.MouseButtonPress and event.button() == Qt.MouseButton.MiddleButton:
                self._autopan_active = True
                self._autopan_origin = event.position().toPoint()
                self.viewport().setCursor(Qt.CursorShape.SizeVerCursor)
                self._autopan_timer.start()
                return True
            if t == t.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
                self._stop_autopan()
                return True
        return super().eventFilter(obj, event)

    def _autopan_tick(self) -> None:
        if not self._autopan_active:
            return
        cursor_pos = self.viewport().mapFromGlobal(self.viewport().cursor().pos())
        delta_y = cursor_pos.y() - self._autopan_origin.y()
        # Dead zone of ±8 px, then scale speed with distance
        if abs(delta_y) < 8:
            return
        speed = int((delta_y - (8 if delta_y > 0 else -8)) * 0.4)
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + speed)

    def _stop_autopan(self) -> None:
        self._autopan_active = False
        self._autopan_timer.stop()
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def set_db(
        self, db: Optional[Database], root_folder: Optional[Path] = None
    ) -> None:
        self._db = db
        self._root_folder = root_folder
        self.refresh()

    def refresh(
        self,
        query: str = "",
        restore_keys: Optional[set[str]] = None,
        restore_scroll: int = 0,
        folder_only: bool = False,
        json_only: bool = False,
        id_only: bool = False,
    ) -> None:
        assets = (
            self._db.search(
                query, folder_only=folder_only, json_only=json_only, id_only=id_only
            )
            if self._db
            else []
        )
        if assets:
            # Show a message BEFORE the UI freezes while rendering all thumbnails.
            # processEvents() flushes it to screen before the heavy _populate() call.
            self.show_loading("Rendering images... this may take a moment")
            QApplication.processEvents()
        self._populate(assets, restore_keys=restore_keys)
        self.hide_loading()
        if restore_scroll:
            # Two deferred ticks: first lets folder bodies become visible,
            # second lets the scroll area recalculate its full content height.
            QTimer.singleShot(0, lambda: QTimer.singleShot(
                0, lambda: self.verticalScrollBar().setValue(restore_scroll)
            ))

    def show_loading(self, msg: str = "Loading...") -> None:
        self._overlay.set_message(msg)
        self._overlay.show()
        self._overlay.raise_()

    def update_loading(self, msg: str) -> None:
        self._overlay.set_message(msg)

    def hide_loading(self) -> None:
        self._overlay.hide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep overlay in sync
        self._overlay.setGeometry(self.viewport().rect())

    def _get_expanded_keys(self) -> set[str]:
        """Recursively collect folder_key of every currently expanded FolderSection."""
        keys: set[str] = set()

        def _collect(layout) -> None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, FolderSection):
                    if w._expanded:
                        keys.add(w._folder_key)
                    # Recurse into the section's body to catch child sections
                    _collect(w._body_lay)

        _collect(self._layout)
        return keys

    def restore_expanded(self, keys: set[str]) -> None:
        """Re-open every FolderSection whose folder_key is in `keys`."""
        def _apply(layout) -> None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, FolderSection):
                    if w._folder_key in keys and not w._expanded:
                        w._toggle()
                    _apply(w._body_lay)

        _apply(self._layout)

    def _populate(self, assets: list[dict], restore_keys: Optional[set[str]] = None) -> None:
        # If no explicit keys provided, snapshot what's currently open so a
        # normal refresh (JSON edit, delete, etc.) preserves expanded state.
        if restore_keys is None:
            restore_keys = self._get_expanded_keys()

        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (w := item.widget()):
                w.deleteLater()

        # Collect all unique folder keys (including implicit ancestor folders)
        all_folders_set: set[str] = set()
        for asset in assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            all_folders_set.add(fk)
            parts = fk.split("/")
            for depth in range(1, len(parts)):
                all_folders_set.add("/".join(parts[:depth]))

        # Sort all folders; this controls display order within each level
        all_folders = sorted(
            all_folders_set, key=_natural_sort_key, reverse=not self._az_sort
        )

        # Build sections depth-first (parents before children) so that
        # add_child_section always finds its parent already in `sections`,
        # regardless of whether the display order is A-Z or Z-A.
        all_folders_by_depth = sorted(all_folders_set, key=lambda f: (len(f.split("/")), f))

        sections: dict[str, FolderSection] = {}
        for fk in all_folders_by_depth:
            parts = fk.split("/") if fk else []
            depth = len(parts)
            title = parts[-1] if parts else "(root)"
            copy_value = self._db.get_folder_meta(fk) if self._db else None
            sec = FolderSection(
                title,
                depth=depth,
                copy_value=copy_value or "",
                folder_key=fk,
                root_folder=self._root_folder,
            )
            sec.set_db(self._db)
            sec.card_deleted.connect(self._on_card_deleted)
            sec.card_edited.connect(self._on_card_edited)
            sec.folder_tagged.connect(self._on_folder_tagged)
            sec.card_view_requested.connect(self.card_view_requested)
            sections[fk] = sec

        # Now add sections to the layout in sorted display order
        for fk in all_folders:
            parts = fk.split("/") if fk else []
            depth = len(parts)
            sec = sections[fk]
            if depth <= 1:
                self._layout.addWidget(sec)
            else:
                parent_key = "/".join(parts[:-1])
                if parent_key in sections:
                    sections[parent_key].add_child_section(sec)
                else:
                    self._layout.addWidget(sec)

        # Sort cards by natural (numeric-aware) name order before adding.
        # The DB query only does a plain SQL "ORDER BY folder, name", which
        # is a raw text sort (1, 10, 11, ..., 2, 20, ...) -- this is what
        # actually re-sorts them into 1, 2, 3, ... within each folder.
        sorted_assets = sorted(
            assets,
            key=lambda a: _natural_sort_key(a.get("name", "")),
            reverse=not self._az_sort,
        )
        for asset in sorted_assets:
            fk = (asset.get("folder", "") or "").replace("\\", "/")
            if fk in sections:
                sections[fk].add_card(asset, self._db)

        # Re-open any folder that was expanded before the refresh
        for fk, sec in sections.items():
            if fk in restore_keys:
                sec._toggle()

        self._layout.addStretch()
        self._apply_tagged_mode()

    # ── "Show Tagged" mode ──────────────────────────────────────────────

    def set_tagged_mode(self, enabled: bool) -> None:
        self._show_tagged = enabled
        self._apply_tagged_mode()

    def _apply_tagged_mode(self) -> None:
        """(Re)apply the current Show Tagged mode to every top-level folder.

        Each FolderSection propagates the mode down to its own cards and
        nested subfolders, so a single top-level call recurses through the
        whole tree. Called after every _populate() so newly rebuilt folder
        trees (from a search, refresh, or edit) keep the current mode.
        """
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if isinstance(w, FolderSection):
                w.set_tagged_mode(self._show_tagged)

    def _on_card_deleted(self, image_path: str) -> None:
        if self._db:
            self._db.delete(image_path)
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]

    def _on_card_edited(self, image_path: str) -> None:
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]

    def _on_folder_tagged(self, folder_key: str) -> None:
        # Reload the whole view so the Copy button appears after a new tag
        win = self.window()
        if hasattr(win, "_do_search"):
            win._do_search()  # type: ignore[union-attr]


# ── Open Database Dialog ───────────────────────────────────────────────────────


class ConfirmRemoveDialog(_DraggableDialog):
    """Styled confirmation dialog used by OpenDatabaseDialog's Remove action."""

    _PREFS_KEY = ""  # not persisted
    W, H = 300, 160

    def __init__(self, db_name: str, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Remove Database")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body ──────────────────────────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(16, 12, 16, 8)
        b_lay.setSpacing(6)

        main_lbl = QLabel(f'Remove "{db_name}"?')
        main_lbl.setObjectName("confirmMainLbl")
        main_lbl.setWordWrap(True)

        info_lbl = QLabel(
            "This deletes the index file. Your original asset folder will not be touched."
        )
        info_lbl.setObjectName("confirmInfoLbl")
        info_lbl.setWordWrap(True)

        b_lay.addWidget(main_lbl)
        b_lay.addWidget(info_lbl)
        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer: Yes (left accent), Cancel (right ghost) ───────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        yes_btn = QPushButton("Yes, Remove")
        yes_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(yes_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        yes_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)


class OpenDatabaseDialog(_DraggableDialog):
    _PREFS_KEY = "open_db_pos"
    W, H = 280, 320

    def __init__(self, db_manager: DatabaseManager, current: str, parent=None):
        super().__init__(parent)
        self._db_manager = db_manager
        self.setWindowTitle("Change Database")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.chosen: str = current

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Accent header bar ─────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Change Database")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        list_wrap = QWidget()
        list_wrap.setObjectName("dbListWrap")
        lw_lay = QVBoxLayout(list_wrap)
        lw_lay.setContentsMargins(8, 6, 8, 4)
        lw_lay.setSpacing(0)

        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        for name in db_manager.names():
            self._list.addItem(name)
            if name == current:
                self._list.setCurrentRow(self._list.count() - 1)
        lw_lay.addWidget(self._list)
        lay.addWidget(list_wrap, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")
        ok_btn = QPushButton("Open")
        ok_btn.setObjectName("editSaveBtn")

        self._remove_db_btn = QPushButton("Remove")
        self._remove_db_btn.setObjectName("dbRemoveBtn")
        self._remove_db_btn.setEnabled(False)

        f_lay.addWidget(ok_btn)
        f_lay.addStretch()
        f_lay.addWidget(self._remove_db_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        ok_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._list.itemDoubleClicked.connect(self._accept)
        self._list.currentRowChanged.connect(self._on_selection_changed)
        self._remove_db_btn.clicked.connect(self._remove_db)

        self._restore_pos()

    def _on_selection_changed(self, row: int) -> None:
        self._remove_db_btn.setEnabled(row >= 0)

    def _remove_db(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        name = item.text()
        dlg = ConfirmRemoveDialog(name, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._db_manager.remove(name)
        # Remove from list widget
        row = self._list.currentRow()
        self._list.takeItem(row)
        # If the removed db was the current/chosen one, clear chosen
        if self.chosen == name:
            self.chosen = ""
        self._remove_db_btn.setEnabled(False)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item:
            self.chosen = item.text()
        self.accept()


# ── Script Runner ──────────────────────────────────────────────────────────────


class ScriptRunner(QThread):
    """Runs startup scripts sequentially in a background thread."""

    progress = Signal(int, int, str)  # current, total, message
    finished = Signal()

    def __init__(self, scripts: list[dict]):
        super().__init__()
        self._scripts = scripts

    def run(self) -> None:
        total = len(self._scripts)
        for i, entry in enumerate(self._scripts, 1):
            self.progress.emit(i, total, f"Executing Startup Scripts ({i}/{total})")
            cmd = f'python "{entry["path"]}"'
            if entry.get("args", "").strip():
                cmd += f" {entry['args'].strip()}"
            try:
                subprocess.run(cmd, shell=True, check=False)
            except Exception:
                pass
        self.finished.emit()


# ── Add Script Dialog ──────────────────────────────────────────────────────────


class AddScriptDialog(_DraggableDialog):
    _PREFS_KEY = "add_script_pos"
    W, H = 340, 262

    def __init__(self, parent=None):
        super().__init__(parent)
        self.script_path: str = ""
        self.script_args: str = ""
        self.script_name: str = ""
        self.setWindowTitle("Add Script")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)
        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)
        title_lbl = QLabel("Add Script")
        title_lbl.setObjectName("editDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # Body
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(8)

        notice = QLabel("Note: the script relies on its path not changing.")
        notice.setObjectName("scriptNotice")
        notice.setWordWrap(True)
        b_lay.addWidget(notice)

        self._path_btn = QPushButton("Select Python Script...")
        self._path_btn.setObjectName("editSaveBtn")
        self._path_btn.clicked.connect(self._pick_script)
        b_lay.addWidget(self._path_btn)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("scriptArgsEdit")
        self._name_edit.setPlaceholderText("Script name  (auto-filled from filename)")
        self._name_edit.setFixedHeight(24)
        b_lay.addWidget(self._name_edit)

        self._args_edit = QLineEdit()
        self._args_edit.setObjectName("scriptArgsEdit")
        self._args_edit.setPlaceholderText(
            "Execute arguments  (e.g. -r C:/some/folder)"
        )
        self._args_edit.setFixedHeight(24)
        b_lay.addWidget(self._args_edit)

        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)
        add_btn = QPushButton("Add")
        add_btn.setObjectName("editSaveBtn")
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")
        f_lay.addWidget(add_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        add_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._restore_pos()

    def _pick_script(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Python Script", "", "Python Scripts (*.py)"
        )
        if path:
            self.script_path = path
            self._path_btn.setText(Path(path).name)
            # Auto-fill name only if the user hasn't typed one yet
            if not self._name_edit.text().strip():
                self._name_edit.setText(Path(path).stem)

    def _accept(self) -> None:
        if not self.script_path:
            QMessageBox.warning(self, APP_NAME, "Please select a Python script first.")
            return
        self.script_name = self._name_edit.text().strip() or Path(self.script_path).stem
        self.script_args = self._args_edit.text().strip()
        self.accept()


# ── Startup Scripts Dialog ─────────────────────────────────────────────────────


class StartupScriptsDialog(_DraggableDialog):
    _PREFS_KEY = "startup_scripts_pos"
    W, H = 280, 340

    def __init__(self, parent=None, readonly: bool = False):
        super().__init__(parent)
        # ── DEV MODE: when readonly=True all _save() calls are no-ops.
        #    The dialog is fully interactive - add, remove, reorder, edit all
        #    work visually - but nothing is written to startup_scripts.json.
        #    Changes are lost when the dialog closes. The real scripts file on
        #    disk is left completely untouched.
        self._save_scripts = (lambda _scripts: None) if readonly else _save_scripts

        self.setWindowTitle(
            "Startup Scripts" + ("  [dev - changes not saved]" if readonly else "")
        )
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._scripts: list[dict] = _load_scripts()

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)
        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)
        title_lbl = QLabel("Startup Scripts")
        title_lbl.setObjectName("editDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # List
        list_wrap = QWidget()
        list_wrap.setObjectName("dbListWrap")
        lw_lay = QVBoxLayout(list_wrap)
        lw_lay.setContentsMargins(8, 6, 8, 4)
        lw_lay.setSpacing(0)
        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.currentRowChanged.connect(self._on_selection_changed)
        lw_lay.addWidget(self._list)
        lay.addWidget(list_wrap, 1)
        self._refresh_list()

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # Footer with +/- and ↑/↓ buttons centered
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 6, 10, 7)
        f_lay.setSpacing(6)

        self._up_btn = QToolButton()
        self._up_btn.setText("↑")
        self._up_btn.setObjectName("scriptOrderBtn")
        self._up_btn.setFixedSize(22, 22)
        self._up_btn.setToolTip("Move Up")
        self._up_btn.setEnabled(False)
        self._up_btn.clicked.connect(self._move_up)

        self._down_btn = QToolButton()
        self._down_btn.setText("↓")
        self._down_btn.setObjectName("scriptOrderBtn")
        self._down_btn.setFixedSize(22, 22)
        self._down_btn.setToolTip("Move Down")
        self._down_btn.setEnabled(False)
        self._down_btn.clicked.connect(self._move_down)

        self._edit_btn = QToolButton()
        self._edit_btn.setText("Edit")
        self._edit_btn.setObjectName("scriptEditBtn")
        self._edit_btn.setFixedSize(48, 20)
        self._edit_btn.setToolTip("Edit Script")
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._edit_script)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("scriptAddBtn")
        self._add_btn.setFixedSize(22, 22)
        self._add_btn.setToolTip("Add Script")
        self._add_btn.clicked.connect(self._add_script)

        self._remove_btn = QToolButton()
        self._remove_btn.setText("−")
        self._remove_btn.setObjectName("scriptRemoveBtn")
        self._remove_btn.setFixedSize(22, 22)
        self._remove_btn.setToolTip("Remove Script")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._remove_script)

        f_lay.addStretch()
        f_lay.addWidget(self._up_btn)
        f_lay.addWidget(self._down_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._edit_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._add_btn)
        f_lay.addWidget(self._remove_btn)
        f_lay.addStretch()
        lay.addWidget(footer)

        self._restore_pos()

    def _refresh_list(self) -> None:
        row = self._list.currentRow()
        self._list.clear()
        for s in self._scripts:
            self._list.addItem(s["name"])
        # Restore selection if possible
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def _on_selection_changed(self, row: int) -> None:
        has = row >= 0
        count = len(self._scripts)
        self._remove_btn.setEnabled(has)
        self._edit_btn.setEnabled(has)
        self._up_btn.setEnabled(has and row > 0)
        self._down_btn.setEnabled(has and row < count - 1)

    def _edit_script(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        entry = self._scripts[row]
        dlg = AddScriptDialog(self)
        # Pre-fill the dialog with the existing values
        dlg.setWindowTitle("Edit Script")
        dlg.script_path = entry["path"]
        dlg._path_btn.setText(
            Path(entry["path"]).name if entry["path"] else "Select Python Script..."
        )
        dlg._name_edit.setText(entry.get("name", ""))
        dlg._args_edit.setText(entry.get("args", ""))
        if dlg.exec():
            self._scripts[row] = {
                "name": dlg.script_name,
                "path": dlg.script_path,
                "args": dlg.script_args,
            }
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(row)

    def _add_script(self) -> None:
        dlg = AddScriptDialog(self)
        if dlg.exec():
            entry = {
                "name": dlg.script_name,
                "path": dlg.script_path,
                "args": dlg.script_args,
            }
            self._scripts.append(entry)
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(len(self._scripts) - 1)

    def _remove_script(self) -> None:
        row = self._list.currentRow()
        if row >= 0:
            del self._scripts[row]
            self._save_scripts(self._scripts)
            self._refresh_list()

    def _move_up(self) -> None:
        row = self._list.currentRow()
        if row > 0:
            self._scripts[row - 1], self._scripts[row] = (
                self._scripts[row],
                self._scripts[row - 1],
            )
            self._save_scripts(self._scripts)
            self._list.setCurrentRow(
                row - 1
            )  # triggers _on_selection_changed via signal
            self._refresh_list()
            self._list.setCurrentRow(row - 1)

    def _move_down(self) -> None:
        row = self._list.currentRow()
        if row < len(self._scripts) - 1:
            self._scripts[row], self._scripts[row + 1] = (
                self._scripts[row + 1],
                self._scripts[row],
            )
            self._save_scripts(self._scripts)
            self._refresh_list()
            self._list.setCurrentRow(row + 1)



# ── Add Identifier Dialog ────────────────────────────────────────────────────


class AddIdentifierDialog(_DraggableDialog):
    """Frameless dialog for adding a single Identifier string. Also reused
    for editing an existing one (title/text is swapped by the caller)."""

    _PREFS_KEY = "add_identifier_pos"
    W, H = 360, 178

    def __init__(self, parent=None):
        super().__init__(parent)
        self.identifier_text: str = ""
        self.temporary: bool = False
        self.setWindowTitle("Add Identifier")
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        self._title_lbl = QLabel("Add Identifier")
        self._title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(self._title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body: single text field ───────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(0)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("scriptArgsEdit")
        self._name_edit.setPlaceholderText("Enter identifier...")
        self._name_edit.setFixedHeight(24)
        self._name_edit.returnPressed.connect(self._accept)
        b_lay.addWidget(self._name_edit)

        self._temp_check = QCheckBox("Temporary (not saved)")
        self._temp_check.setObjectName("identifierTempCheck")
        self._temp_check.setStyleSheet(
            "color: rgba(220,220,220,0.75); font-size: 11px; background: transparent;"
        )
        self._temp_check.setToolTip(
            "Kept only in memory for this session - never written to disk,\n"
            "so it isn't included in this image's JSON and won't survive a restart."
        )
        b_lay.addSpacing(10)
        b_lay.addWidget(self._temp_check)

        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer: Save (left), Cancel (right) ───────────────────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        save_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)

        self._restore_pos()
        QTimer.singleShot(0, self._name_edit.setFocus)

    def set_title(self, title: str) -> None:
        self.setWindowTitle(title)
        self._title_lbl.setText(title)

    def set_text(self, text: str) -> None:
        self._name_edit.setText(text)

    def set_temporary(self, temporary: bool) -> None:
        self._temp_check.setChecked(temporary)

    def _accept(self) -> None:
        # An empty name is allowed here - the caller falls back to an
        # auto-generated "Identifier N" name when this comes back blank.
        self.identifier_text = self._name_edit.text().strip()
        self.temporary = self._temp_check.isChecked()
        self.accept()


# ── Manage Identifiers Dialog ────────────────────────────────────────────────


class _ManageIdRow(QWidget):
    """One top-level identifier row inside the Manage Identifiers list.

    Has an expand/collapse arrow (same look as a folder header's arrow) on
    the left, then the name (italic when the identifier is temporary). The
    row forwards plain clicks to `on_select` so the underlying QListWidgetItem
    still gets selected (needed for the Up/Down/Edit/Remove footer buttons),
    while the arrow toggles expansion independently.
    """

    arrow_clicked = Signal(str)
    row_clicked = Signal(str)

    def __init__(self, name: str, temporary: bool, expanded: bool, parent=None):
        super().__init__(parent)
        self._name = name
        self.setFixedHeight(24)
        self.setStyleSheet("background: transparent;")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 8, 0)
        lay.setSpacing(4)

        self._arrow_btn = QToolButton()
        self._arrow_btn.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._arrow_btn.setFixedSize(16, 16)
        self._arrow_btn.setStyleSheet(
            "QToolButton { background: transparent; border: none; }"
        )
        self._arrow_btn.clicked.connect(lambda: self.arrow_clicked.emit(self._name))
        lay.addWidget(self._arrow_btn)

        self._label = QLabel(name)
        font = self._label.font()
        font.setItalic(temporary)
        self._label.setFont(font)
        self._label.setStyleSheet(
            "color: rgba(220,220,220,0.88); font-size: 11px; background: transparent;"
        )
        lay.addWidget(self._label)

        if temporary:
            tag = QLabel("temp")
            tag.setStyleSheet(
                "color: rgba(232,184,75,0.75); font-size: 9px; font-style: italic;"
                " background: transparent;"
            )
            lay.addWidget(tag)

        lay.addStretch()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.row_clicked.emit(self._name)
        super().mouseReleaseEvent(event)


class _ManageMemberRow(QWidget):
    """One member row nested under an expanded identifier: just the image's
    name, plus a hover-reveal '−' button on the right for removing it from
    this identifier. No thumbnail, no other interaction."""

    remove_clicked = Signal(str, str)  # (identifier_name, image_path)

    def __init__(self, identifier_name: str, display_name: str, image_path: str, parent=None):
        super().__init__(parent)
        self._identifier_name = identifier_name
        self._image_path = image_path
        self.setFixedHeight(22)
        self.setStyleSheet("background: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 0, 8, 0)
        lay.setSpacing(4)

        label = QLabel(display_name)
        label.setStyleSheet(
            "color: rgba(200,200,200,0.65); font-size: 10.5px; background: transparent;"
        )
        lay.addWidget(label)
        lay.addStretch()

        self._remove_btn = QToolButton()
        self._remove_btn.setText("−")
        self._remove_btn.setFixedSize(16, 16)
        self._remove_btn.setStyleSheet(
            "QToolButton { background: transparent; border: none;"
            " color: rgba(255,120,120,0.85); font-size: 12px; }"
            "QToolButton:hover { color: rgb(255,120,120); }"
        )
        self._remove_btn.setVisible(False)
        self._remove_btn.clicked.connect(
            lambda: self.remove_clicked.emit(self._identifier_name, self._image_path)
        )
        lay.addWidget(self._remove_btn)

    def enterEvent(self, event) -> None:
        self._remove_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._remove_btn.setVisible(False)
        super().leaveEvent(event)


class ManageIdentifiersDialog(_DraggableDialog):
    """Per-database Identifier list manager. Add/edit/remove entries and
    reorder with ↑/↓, same chrome as Startup Scripts. The order shown here is
    the exact order identifiers later appear in a card's 'Edit ID' submenu.

    Each row can be expanded (arrow on the left, same as a folder's) to show
    which images currently carry that identifier - hovering a member reveals
    a '−' to remove just that one. Editing an identifier can also flip its
    Temporary state, which migrates every current member's membership between
    the asset's own JSON and this Database instance's in-memory bookkeeping
    (a LoadingOverlay is shown while that migration runs)."""

    _PREFS_KEY = "manage_identifiers_pos"
    W, H = 280, 340

    def __init__(self, db, parent=None, readonly: bool = False):
        super().__init__(parent)
        self._db = db
        # ── DEV MODE: when readonly=True, changes are visually interactive
        #    but never persisted beyond the in-memory DevDatabase instance.
        self._readonly = readonly

        self.setWindowTitle(
            "Manage Identifiers" + ("  [dev - changes not saved]" if readonly else "")
        )
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._identifiers: list[str] = self._db.get_identifiers() if self._db else []
        self._expanded: set[str] = set()

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Header
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)
        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)
        title_lbl = QLabel("Manage Identifiers")
        title_lbl.setObjectName("editDialogTitle")
        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)
        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # List
        list_wrap = QWidget()
        list_wrap.setObjectName("dbListWrap")
        lw_lay = QVBoxLayout(list_wrap)
        lw_lay.setContentsMargins(8, 6, 8, 4)
        lw_lay.setSpacing(0)
        self._list = QListWidget()
        self._list.setObjectName("dbDialogList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.currentRowChanged.connect(self._on_selection_changed)
        lw_lay.addWidget(self._list)
        lay.addWidget(list_wrap, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # Footer with +/- and ↑/↓ buttons centered
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 6, 10, 7)
        f_lay.setSpacing(6)

        self._up_btn = QToolButton()
        self._up_btn.setText("↑")
        self._up_btn.setObjectName("scriptOrderBtn")
        self._up_btn.setFixedSize(22, 22)
        self._up_btn.setToolTip("Move Up")
        self._up_btn.setEnabled(False)
        self._up_btn.clicked.connect(self._move_up)

        self._down_btn = QToolButton()
        self._down_btn.setText("↓")
        self._down_btn.setObjectName("scriptOrderBtn")
        self._down_btn.setFixedSize(22, 22)
        self._down_btn.setToolTip("Move Down")
        self._down_btn.setEnabled(False)
        self._down_btn.clicked.connect(self._move_down)

        self._edit_btn = QToolButton()
        self._edit_btn.setText("Edit")
        self._edit_btn.setObjectName("scriptEditBtn")
        self._edit_btn.setFixedSize(48, 20)
        self._edit_btn.setToolTip("Edit Identifier")
        self._edit_btn.setEnabled(False)
        self._edit_btn.clicked.connect(self._edit_identifier)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("scriptAddBtn")
        self._add_btn.setFixedSize(22, 22)
        self._add_btn.setToolTip("Add Identifier")
        self._add_btn.clicked.connect(self._add_identifier)

        self._remove_btn = QToolButton()
        self._remove_btn.setText("−")
        self._remove_btn.setObjectName("scriptRemoveBtn")
        self._remove_btn.setFixedSize(22, 22)
        self._remove_btn.setToolTip("Remove Identifier")
        self._remove_btn.setEnabled(False)
        self._remove_btn.clicked.connect(self._remove_identifier)

        f_lay.addStretch()
        f_lay.addWidget(self._up_btn)
        f_lay.addWidget(self._down_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._edit_btn)
        f_lay.addSpacing(8)
        f_lay.addWidget(self._add_btn)
        f_lay.addWidget(self._remove_btn)
        f_lay.addStretch()
        lay.addWidget(footer)

        # Shown over the whole dialog while a temporary<->permanent
        # migration is writing/stripping JSON across potentially many assets.
        self._overlay = LoadingOverlay(frame)

        self._refresh_list()
        self._restore_pos()

    # ── Selection helpers ────────────────────────────────────────────────
    # Member sub-rows are inserted as extra (non-selectable) QListWidgetItems
    # right below their parent identifier, so `currentRow()` alone can't be
    # trusted as an index into self._identifiers - we track selection by
    # identifier *name* instead, tagged onto each item's UserRole data.

    def _current_identifier_name(self) -> Optional[str]:
        item = self._list.currentItem()

    def _select_identifier_row(self, name: str) -> None:
        for i in range(self._list.count()):
            item = self._list.item(i)
            info = item.data(Qt.ItemDataRole.UserRole)
            if info and info.get("type") == "id" and info.get("name") == name:
                self._list.setCurrentItem(item)
                return

    # ── Member lookups / JSON migration ─────────────────────────────────

    def _get_members(self, name: str) -> list[tuple[str, str]]:
        """[(display_name, image_path), ...] for every asset currently
        carrying identifier `name`, sorted by name."""
        if self._db is None:
            return []
        if self._db.is_temporary(name):
            paths = self._db.temp_members(name)
            if not paths:
                return []
            by_path = {a["image_path"]: a for a in self._db.all_assets()}
            out = []
            for p in paths:
                a = by_path.get(p)
                nm = a["name"] if a else Path(p).stem
                out.append((nm, p))
            return sorted(out)
        out = []
        for a in self._db.all_assets():
            try:
                data = json.loads(a.get("json_data", "{}"))
            except Exception:
                data = {}
            toks = [
                t.strip()
                for t in str(data.get("Identifier", "") or "").split(",")
                if t.strip()
            ]
            if name in toks:
                out.append((a["name"], a["image_path"]))
        return sorted(out)

    def _find_asset(self, image_path: str) -> Optional[dict]:
        if self._db is None:
            return None
        for a in self._db.all_assets():
            if a["image_path"] == image_path:
                return a
        return None

    def _write_asset_json(self, asset: dict, new_text: str) -> None:
        """Mirrors ThumbnailCard._toggle_identifier's disk+db write path."""
        if not self._readonly:
            json_path = asset.get("json_path", "")
            if json_path:
                try:
                    Path(json_path).write_text(new_text, encoding="utf-8")
                except Exception:
                    pass  # best-effort; the DB copy below stays authoritative
        if self._db:
            self._db.update_json(asset["image_path"], new_text)
        asset["json_data"] = new_text

    def _strip_identifier_from_asset(self, name: str, image_path: str) -> None:
        asset = self._find_asset(image_path)
        if asset is None:
            return
        try:
            data = json.loads(asset.get("json_data", "{}"))
        except Exception:
            data = {}
        toks = [
            t.strip()
            for t in str(data.get("Identifier", "") or "").split(",")
            if t.strip()
        ]
        toks = [t for t in toks if t != name]
        if toks:
            data["Identifier"] = ", ".join(toks)
        else:
            data.pop("Identifier", None)
        self._write_asset_json(asset, json.dumps(data, indent=2, ensure_ascii=False))

    def _add_identifier_to_asset(self, name: str, asset: dict) -> None:
        try:
            data = json.loads(asset.get("json_data", "{}"))
        except Exception:
            data = {}
        toks = [
            t.strip()
            for t in str(data.get("Identifier", "") or "").split(",")
            if t.strip()
        ]
        if name not in toks:
            toks.append(name)
        data["Identifier"] = ", ".join(toks)
        self._write_asset_json(asset, json.dumps(data, indent=2, ensure_ascii=False))

    def _rename_identifier_in_asset(
        self, old_name: str, new_name: str, image_path: str
    ) -> None:
        """Swap old_name -> new_name in-place within one asset's Identifier
        list, preserving its position and de-duping if new_name happens to
        already be present separately."""
        asset = self._find_asset(image_path)
        if asset is None:
            return
        try:
            data = json.loads(asset.get("json_data", "{}"))
        except Exception:
            data = {}
        toks = [
            t.strip()
            for t in str(data.get("Identifier", "") or "").split(",")
            if t.strip()
        ]
        toks = [new_name if t == old_name else t for t in toks]
        deduped: list[str] = []
        for t in toks:
            if t not in deduped:
                deduped.append(t)
        if deduped:
            data["Identifier"] = ", ".join(deduped)
        else:
            data.pop("Identifier", None)
        self._write_asset_json(asset, json.dumps(data, indent=2, ensure_ascii=False))

    def _remove_member(self, name: str, image_path: str) -> None:
        if self._db is None:
            return
        if self._db.is_temporary(name):
            self._db.remove_temp_member(name, image_path)
        else:
            self._strip_identifier_from_asset(name, image_path)
        self._refresh_list()
        self._select_identifier_row(name)

    def _migrate_temporary(self, name: str, new_temporary: bool) -> None:
        """Move every current member of `name` between the asset's own JSON
        and this Database's in-memory temp set, then flip the bookkeeping
        bit. Shows a loading overlay since this may touch many assets."""
        if self._db is None:
            return
        self._overlay.set_message("Converting...")
        self._overlay.show()
        self._overlay.raise_()
        QApplication.processEvents()
        try:
            if new_temporary:
                # permanent -> temporary: strip from every asset's JSON
                members = self._get_members(name)
                paths: set[str] = set()
                total = len(members)
                for i, (_, image_path) in enumerate(members):
                    self._strip_identifier_from_asset(name, image_path)
                    paths.add(image_path)
                    if total > 25 and i % 10 == 0:
                        self._overlay.set_message(f"Converting... ({i}/{total})")
                        QApplication.processEvents()
                self._db.set_identifier_temporary(name, True, members=paths)
            else:
                # temporary -> permanent: write into every member's JSON
                paths = self._db.temp_members(name)
                by_path = {a["image_path"]: a for a in self._db.all_assets()}
                total = len(paths)
                for i, p in enumerate(paths):
                    asset = by_path.get(p)
                    if asset is not None:
                        self._add_identifier_to_asset(name, asset)
                    if total > 25 and i % 10 == 0:
                        self._overlay.set_message(f"Converting... ({i}/{total})")
                        QApplication.processEvents()
                self._db.set_identifier_temporary(name, False)
        finally:
            self._overlay.hide()

    def _cascade_rename(self, old_name: str, new_name: str, members: list) -> None:
        """Rewrite `old_name` -> `new_name` inside every asset that currently
        has it, so a rename actually propagates everywhere instead of just
        relabeling the entry in this dialog (temporary identifiers don't need
        this - their membership is keyed by path, not by a name copied into
        JSON, so `Database.rename_identifier` already handles it fully)."""
        if not members:
            return
        self._overlay.set_message("Renaming...")
        self._overlay.show()
        self._overlay.raise_()
        QApplication.processEvents()
        try:
            total = len(members)
            for i, (_, image_path) in enumerate(members):
                self._rename_identifier_in_asset(old_name, new_name, image_path)
                if total > 25 and i % 10 == 0:
                    self._overlay.set_message(f"Renaming... ({i}/{total})")
                    QApplication.processEvents()
        finally:
            self._overlay.hide()

    def _cascade_remove(self, name: str, members: list) -> None:
        """Strip `name` out of every asset that currently has it, so a
        removal doesn't leave orphaned, invisible-but-still-searchable
        entries behind in those assets' JSON."""
        if not members:
            return
        self._overlay.set_message("Removing...")
        self._overlay.show()
        self._overlay.raise_()
        QApplication.processEvents()
        try:
            total = len(members)
            for i, (_, image_path) in enumerate(members):
                self._strip_identifier_from_asset(name, image_path)
                if total > 25 and i % 10 == 0:
                    self._overlay.set_message(f"Removing... ({i}/{total})")
                    QApplication.processEvents()
        finally:
            self._overlay.hide()

    # ── List building ────────────────────────────────────────────────────

    def _toggle_expand(self, name: str) -> None:
        if name in self._expanded:
            self._expanded.discard(name)
        else:
            self._expanded.add(name)
        self._refresh_list()
        self._select_identifier_row(name)

    def _add_identifier_item(self, name: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, {"type": "id", "name": name})
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        temporary = self._db.is_temporary(name) if self._db else False
        row_widget = _ManageIdRow(name, temporary, name in self._expanded)
        item.setSizeHint(row_widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row_widget)
        row_widget.arrow_clicked.connect(self._toggle_expand)
        row_widget.row_clicked.connect(self._select_identifier_row)

    def _add_member_items(self, name: str) -> None:
        members = self._get_members(name)
        if not members:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, {"type": "empty"})
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            lbl = QLabel("No images")
            lbl.setContentsMargins(24, 0, 0, 0)
            lbl.setStyleSheet(
                "color: rgba(200,200,200,0.35); font-size: 10.5px; font-style: italic;"
                " background: transparent;"
            )
            item.setSizeHint(QSize(0, 20))
            self._list.addItem(item)
            self._list.setItemWidget(item, lbl)
            return
        for display_name, image_path in members:
            item = QListWidgetItem()
            item.setData(
                Qt.ItemDataRole.UserRole,
                {"type": "member", "identifier": name, "image_path": image_path},
            )
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            row_widget = _ManageMemberRow(name, display_name, image_path)
            item.setSizeHint(row_widget.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row_widget)
            row_widget.remove_clicked.connect(self._remove_member)

    def _persist_order(self) -> None:
        if self._db:
            self._db.set_identifiers_order(self._identifiers)

    def _refresh_list(self) -> None:
        current_name = self._current_identifier_name()
        self._list.clear()
        for name in self._identifiers:
            self._add_identifier_item(name)
            if name in self._expanded:
                self._add_member_items(name)
        if current_name and current_name in self._identifiers:
            self._select_identifier_row(current_name)
        self._on_selection_changed(self._list.currentRow())

    def _on_selection_changed(self, row: int) -> None:
        name = self._current_identifier_name()
        has = name is not None
        self._remove_btn.setEnabled(has)
        self._edit_btn.setEnabled(has)
        if has:
            idx = self._identifiers.index(name)
            self._up_btn.setEnabled(idx > 0)
            self._down_btn.setEnabled(idx < len(self._identifiers) - 1)
        else:
            self._up_btn.setEnabled(False)
            self._down_btn.setEnabled(False)

    # ── Actions ──────────────────────────────────────────────────────────

    def _edit_identifier(self) -> None:
        name = self._current_identifier_name()
        if name is None:
            return
        was_temp = self._db.is_temporary(name) if self._db else False
        dlg = AddIdentifierDialog(self)
        dlg.set_title("Edit Identifier")
        dlg.set_text(name)
        dlg.set_temporary(was_temp)
        if not dlg.exec():
            return
        new_name = dlg.identifier_text.strip() or _default_identifier_name(
            self._identifiers
        )
        new_temp = dlg.temporary

        if new_name != name and self._db:
            # A temporary identifier's membership is keyed by image path, not
            # by this name string, so rename_identifier() below already moves
            # it completely - no asset JSON is ever involved. A *persisted*
            # identifier's name is literally copied into each member's JSON,
            # so renaming it also has to rewrite every one of those copies -
            # otherwise the old name keeps floating around as an invisible,
            # still-searchable ghost. Grab the member list under the OLD name
            # before renaming the table entry.
            members = [] if was_temp else self._get_members(name)
            self._db.rename_identifier(name, new_name)
            if not was_temp:
                self._cascade_rename(name, new_name, members)
            idx = self._identifiers.index(name)
            self._identifiers[idx] = new_name
            if name in self._expanded:
                self._expanded.discard(name)
                self._expanded.add(new_name)

        if new_temp != was_temp and self._db:
            self._migrate_temporary(new_name, new_temp)

        self._refresh_list()
        self._select_identifier_row(new_name)

    def _add_identifier(self) -> None:
        dlg = AddIdentifierDialog(self)
        if not dlg.exec():
            return
        name = dlg.identifier_text.strip() or _default_identifier_name(
            self._identifiers
        )
        self._identifiers.append(name)
        if self._db:
            self._db.add_identifier(name, temporary=dlg.temporary)
        self._refresh_list()
        self._select_identifier_row(name)

    def _remove_identifier(self) -> None:
        name = self._current_identifier_name()
        if name is None:
            return
        is_temp = self._db.is_temporary(name) if self._db else False
        # Temporary identifiers have no JSON footprint to clean up - removing
        # the definition already removes every membership with it.
        members = [] if is_temp else self._get_members(name)
        if members:
            count = len(members)
            resp = QMessageBox.question(
                self,
                "Remove Identifier",
                f"'{name}' is currently applied to {count} image"
                f"{'s' if count != 1 else ''}. Removing it here will also "
                f"clear it from all of them.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if resp != QMessageBox.StandardButton.Yes:
                return
        self._identifiers.remove(name)
        self._expanded.discard(name)
        if members:
            self._cascade_remove(name, members)
        if self._db:
            self._db.remove_identifier(name)
        self._refresh_list()

    def _move_up(self) -> None:
        name = self._current_identifier_name()
        if name is None:
            return
        idx = self._identifiers.index(name)
        if idx > 0:
            self._identifiers[idx - 1], self._identifiers[idx] = (
                self._identifiers[idx],
                self._identifiers[idx - 1],
            )
            self._persist_order()
            self._refresh_list()
            self._select_identifier_row(name)

    def _move_down(self) -> None:
        name = self._current_identifier_name()
        if name is None:
            return
        idx = self._identifiers.index(name)
        if idx < len(self._identifiers) - 1:
            self._identifiers[idx + 1], self._identifiers[idx] = (
                self._identifiers[idx],
                self._identifiers[idx + 1],
            )
            self._persist_order()
            self._refresh_list()
            self._select_identifier_row(name)


# ── Image Viewer Overlay ───────────────────────────────────────────────────────



class ImgViewerOverlay(QWidget):
    """
    Full-size overlay that dims the app and shows one image at a time.

    • Opens centred in the central widget on left-click of any ThumbnailCard.
    • Prev/Next arrows navigate cards within the same folder (the list passed
      from FolderSection).
    • Right-click on the image fires the originating card's context-menu so all
      card actions (Copy, Edit JSON, Add Tag, Delete) work exactly as normal.
    • Click on the dim area (outside the image panel) closes the viewer.
    • ESC and the × button also close the viewer.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        self._cards: list[ThumbnailCard] = []
        self._idx: int = 0
        self._current_card: Optional[ThumbnailCard] = None

        # ── Layout ---------------------------------------------------------
        # The overlay has no layout manager; everything is placed manually in
        # resizeEvent so we can precisely control the image area size.

        # Dim backdrop (the overlay widget itself is the backdrop; we paint it)
        # ── Close button (top-right)
        self._close_btn = QToolButton(self)
        self._close_btn.setText("✕")
        self._close_btn.setObjectName("viewerCloseBtn")
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.clicked.connect(self.close_viewer)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        # ── Image label
        self._img_lbl = QLabel(self)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setObjectName("viewerImage")
        self._img_lbl.setScaledContents(False)
        self._img_lbl.installEventFilter(self)  # capture right-click

        # ── Name label (below image)
        self._name_lbl = QLabel(self)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setObjectName("viewerName")

        # ── Prev / Next buttons
        self._prev_btn = QToolButton(self)
        self._prev_btn.setText("‹")
        self._prev_btn.setObjectName("viewerNavBtn")
        self._prev_btn.setFixedSize(44, 80)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._go_prev)

        self._next_btn = QToolButton(self)
        self._next_btn.setText("›")
        self._next_btn.setObjectName("viewerNavBtn")
        self._next_btn.setFixedSize(44, 80)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._go_next)

    # ── Public API ─────────────────────────────────────────────────────────

    def open_viewer(self, card: ThumbnailCard, cards: list[ThumbnailCard]) -> None:
        """Show the viewer for , with  as the navigation list."""
        self._cards = cards
        self._idx = cards.index(card) if card in cards else 0
        self._current_card = card
        self.resize(self.parent().size())  # type: ignore[union-attr]
        self._load_current()
        self._update_nav_state()
        self.show()
        self.raise_()
        self.setFocus()

    def close_viewer(self) -> None:
        self.hide()
        self._current_card = None
        self._cards = []

    # ── Navigation ─────────────────────────────────────────────────────────

    def _go_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._current_card = self._cards[self._idx]
            self._load_current()
            self._update_nav_state()

    def _go_next(self) -> None:
        if self._idx < len(self._cards) - 1:
            self._idx += 1
            self._current_card = self._cards[self._idx]
            self._load_current()
            self._update_nav_state()

    def _update_nav_state(self) -> None:
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(self._cards) - 1)

    # ── Image loading ──────────────────────────────────────────────────────

    def _load_current(self) -> None:
        card = self._current_card
        if card is None:
            return
        name = card.asset.get("name", "")
        self._name_lbl.setText(name)

        path = card.asset.get("image_path", "")
        if not path:
            self._img_lbl.clear()
            self._img_lbl.setText("(no image)")
            self._place_widgets()
            return

        pix = QPixmap(path)
        if pix.isNull():
            self._img_lbl.setText("?")
        else:
            self._img_lbl.setProperty("_raw_pix", pix)
            self._scale_image()
        self._place_widgets()

    def _scale_image(self) -> None:
        """Scale the stored raw pixmap to fit the available image area."""
        pix = self._img_lbl.property("_raw_pix")
        if pix is None or pix.isNull():
            return
        max_w, max_h = self._image_area_size()
        scaled = pix.scaled(
            max_w, max_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img_lbl.setPixmap(scaled)
        self._img_lbl.resize(scaled.width(), scaled.height())

    def _image_area_size(self) -> tuple[int, int]:
        """Return (max_w, max_h) for the image, leaving room for name + margins."""
        w = self.width()
        h = self.height()
        margin_x = 110  # space for prev/next buttons + padding
        margin_y = 100  # space for name label + padding
        return max(200, w - margin_x * 2), max(200, h - margin_y)

    # ── Widget placement ───────────────────────────────────────────────────

    def _place_widgets(self) -> None:
        """Manually position all child widgets relative to the overlay size."""
        ow, oh = self.width(), self.height()

        # Close button – top right
        pad = 14
        self._close_btn.move(ow - self._close_btn.width() - pad, pad)

        # Image – centred in available area (vertically biased slightly upward)
        img_w = self._img_lbl.width()
        img_h = self._img_lbl.height()
        img_x = (ow - img_w) // 2
        img_y = max(50, (oh - img_h - 40) // 2)  # 40 = room for name below
        self._img_lbl.move(img_x, img_y)

        # Name label – directly below image
        name_h = 28
        self._name_lbl.setFixedSize(min(600, ow - 40), name_h)
        name_x = (ow - self._name_lbl.width()) // 2
        name_y = img_y + img_h + 10
        self._name_lbl.move(name_x, name_y)

        # Prev / next – vertically centred on the image
        btn_y = img_y + (img_h - self._prev_btn.height()) // 2
        left_edge  = img_x - self._prev_btn.width() - 12
        right_edge = img_x + img_w + 12
        self._prev_btn.move(max(8, left_edge), btn_y)
        self._next_btn.move(min(ow - self._next_btn.width() - 8, right_edge), btn_y)

    # ── Events ─────────────────────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.isVisible():
            self._scale_image()
            self._place_widgets()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 180))
        p.end()

    def mousePressEvent(self, event) -> None:
        """Click outside the image panel closes the viewer."""
        if event.button() == Qt.MouseButton.LeftButton:
            img_rect = self._img_lbl.geometry()
            if not img_rect.contains(event.position().toPoint()):
                self.close_viewer()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_viewer()
        elif key == Qt.Key.Key_Left:
            self._go_prev()
        elif key == Qt.Key.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event) -> bool:
        """Intercept right-click on the image label → delegate to card's context menu."""
        if obj is self._img_lbl and event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.RightButton and self._current_card:
                # Synthesise a context-menu event on the card
                global_pos = self._img_lbl.mapToGlobal(event.position().toPoint())
                from PySide6.QtGui import QContextMenuEvent
                ctx = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(0, 0), global_pos)
                QApplication.sendEvent(self._current_card, ctx)
                return True
        return super().eventFilter(obj, event)


# ── Main Window ────────────────────────────────────────────────────────────────


def _make_win_icon(kind: str, color: str = "#b4b4b4", size: int = 10) -> QIcon:
    """Hand-draw a small crisp icon for the maximize/restore window button.

    Unicode glyphs like '□' render at wildly different sizes/weights
    depending on the font, which is why the maximize button looked off.
    A tiny drawn pixmap keeps it the same visual weight as the plain
    text minimize/close buttons next to it.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    pen = QPen(QColor(color))
    pen.setWidth(1)
    p.setPen(pen)
    if kind == "max":
        p.drawRect(0, 0, size - 1, size - 1)
    elif kind == "restore":
        back = size - 3
        p.drawRect(2, 0, back, back)
        p.fillRect(0, 3, back, back, QColor("#0a0a0a"))
        p.drawRect(0, 3, back, back)
    elif kind == "min":
        mid = size // 2
        p.drawLine(0, mid, size - 1, mid)
    elif kind == "close":
        p.drawLine(0, 0, size - 1, size - 1)
        p.drawLine(0, size - 1, size - 1, 0)
    p.end()
    return QIcon(pm)


# ── Windows: real Aero-Snap for the frameless main window ────────────────────
# Qt's FramelessWindowHint strips the WS_THICKFRAME/WS_CAPTION styles Windows
# uses to decide whether a window can be dragged to an edge to snap, or moved
# with Win+Arrow -- startSystemMove()/startSystemResize() (used below) get you
# an OS-driven drag, but they do NOT restore snapping on their own.
#
# So on Windows we skip FramelessWindowHint entirely and keep the native
# frame, then hide the native titlebar ourselves by intercepting two window
# messages:
#   WM_NCCALCSIZE - claim the whole window as "client area" (no caption bar
#                   gets drawn), while the resizable frame style stays intact.
#   WM_NCHITTEST  - tell Windows our custom title bar IS the caption, so
#                   drag-to-move / drag-to-snap / Win+Arrow / double-click-
#                   to-maximize all keep working exactly like a normal window.
#
# macOS/Linux are untouched and keep using FramelessWindowHint plus the
# startSystemMove()/startSystemResize() calls already used elsewhere.
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class _NCCALCSIZE_PARAMS(ctypes.Structure):
        _fields_ = [("rgrc", _RECT * 3), ("lppos", ctypes.c_void_p)]

    _WM_NCCALCSIZE = 0x0083
    _WM_NCHITTEST = 0x0084
    _HTCLIENT = 1
    _HTCAPTION = 2
    _HTLEFT, _HTRIGHT, _HTTOP, _HTBOTTOM = 10, 11, 12, 15
    _HTTOPLEFT, _HTTOPRIGHT, _HTBOTTOMLEFT, _HTBOTTOMRIGHT = 13, 14, 16, 17
    _SM_CXSIZEFRAME = 32
    _SM_CYSIZEFRAME = 33
    _SM_CXPADDEDBORDER = 92
    _RESIZE_BORDER_PX = 8  # invisible grab margin for edge/corner resize


class _TitleBar(QWidget):
    """Thin custom title bar for the frameless main window.

    Provides drag-to-move and double-click-to-maximize, matching the
    frameless chrome already used by vael.'s other dialogs/apps.

    Dragging is delegated to the OS via QWindow.startSystemMove() rather
    than being done by hand (self._window.move(...) on every mouse-move).
    A manually-moved frameless window never generates the native
    "this window is being dragged" signal Windows needs to show Snap zones
    and Snap Assist, so features like drag-to-edge-to-snap or Win+Arrow
    didn't reliably engage. startSystemMove() hands the drag off to the
    real OS window-move, which restores that behaviour for free (and also
    handles "un-maximize and keep the cursor at the same relative spot"
    automatically, so that no longer needs to be done manually either).
    A manual move loop is kept as a fallback for the rare case where
    startSystemMove() isn't available (e.g. some X11/Wayland setups).
    """

    def __init__(self, window: "MainWindow", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._window = window
        self._drag_offset: Optional[QPoint] = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            wh = self._window.windowHandle()
            started = bool(wh is not None and wh.startSystemMove())
            if not started:
                # Fallback: manual move loop (pre-existing behaviour).
                self._drag_offset = (
                    event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
                )
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            if self._window.isMaximized():
                # Unmaximize, then keep the cursor at the same relative spot
                self._window._toggle_maximize(force_normal=True)
                self._drag_offset = QPoint(self._window.width() // 2, 14)
            self._window.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._window._toggle_maximize()
        super().mouseDoubleClickEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__()
        self.db_manager = db_manager
        self._active_db = ""
        self._app_menu: Optional[QMenu] = None
        # "Show Tagged" mode: False (default) = normal behaviour, unchanged.
        # True = tagged-image highlighting + fully-tagged folder dots.
        self._show_tagged: bool = False
        self._index_worker: Optional[IndexWorker] = None
        self._pre_search_expanded: Optional[set[str]] = None
        self._pre_search_scroll: int = 0
        self._script_runner: Optional[ScriptRunner] = None
        self._note_window: Optional[NoteWindow] = None
        self._img_viewer: Optional[ImgViewerOverlay] = None
        self._size_grip: Optional[QSizeGrip] = None
        # Defined unconditionally (not just in the frameless branch below) so
        # eventFilter()/_edges_at() can never hit an AttributeError if they
        # end up being invoked before/without the frameless setup running.
        self._RESIZE_MARGIN = 6

        # ── DEV MODE: create the in-memory stub database once here.
        #    This reference is ONLY ever used when DEV_MODE is True.
        #    It is never stored to prefs, never written to disk.
        self._dev_db: Optional[DevDatabase] = DevDatabase() if DEV_MODE else None

        title = f"{APP_NAME}  [DEV MODE - fake data only]" if DEV_MODE else APP_NAME
        self.setWindowTitle(title)
        self._is_windows = _IS_WINDOWS
        if not self._is_windows:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            # Needed so the stylesheet's rounded corners on #appShell actually show
            # rounded instead of being clipped to a square opaque window surface.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            # The global stylesheet paints "QMainWindow" (which this widget IS)
            # with an opaque square background. That paints *underneath* the
            # rounded #appShell and shows through at its corners as a square.
            # Give this window its own objectName so the stylesheet can force
            # it transparent (see "#mainWindowFrameless" rule below) so only
            # the rounded #appShell surface is ever visible.
            self.setObjectName("mainWindowFrameless")
            self.setMouseTracking(True)
            QApplication.instance().installEventFilter(self)
        # On Windows we deliberately keep the native frame -- see
        # nativeEvent()/_win_hit_test() below -- specifically so drag-to-edge
        # snapping and Win+Arrow keep working.
        self.setMinimumSize(560, 460)
        self.resize(1000, 720)
        # ── Restore last window geometry from prefs ────────────────────────
        #
        # We store plain x/y/width/height ints rather than Qt's own
        # saveGeometry()/restoreGeometry() blob. restoreGeometry() looks
        # appealing (frame-aware, handles maximized state, etc.) but it
        # also silently clamps the restored *size* to fit the current
        # screen's available geometry, and -- critically -- repositions
        # the window as part of that clamping even when nothing is
        # actually off-screen. That fights exactly the kind of placement
        # people use a second monitor for: dragged flush to an edge and
        # stretched close to the full height. Plain move()/resize() with
        # explicit numbers has none of that "helpful" behaviour -- it
        # reproduces the exact saved geometry every time.
        if not DEV_MODE:
            _prefs = _load_prefs()
            _geo = _prefs.get("main_window_geometry")
            if isinstance(_geo, dict):
                x = _geo.get("x")
                y = _geo.get("y")
                w = _geo.get("width")
                h = _geo.get("height")
                if all(isinstance(v, int) for v in (x, y, w, h)):
                    # Clamp to minimum size so saved values can never shrink below it
                    w = max(560, w)
                    h = max(460, h)
                    self.resize(w, h)
                    self.move(x, y)
            # Guard against the window landing off-screen (e.g. a second
            # monitor that is no longer connected).  We require at least
            # 100 px of the title-bar area to be visible on some screen
            # so the user can still grab and drag the window.
            from PySide6.QtGui import QGuiApplication
            available = QGuiApplication.primaryScreen().virtualGeometry()
            title_bar_rect = self.frameGeometry()
            title_bar_rect.setHeight(min(100, title_bar_rect.height()))
            if not available.intersects(title_bar_rect):
                # Centre on the primary screen instead
                primary = QGuiApplication.primaryScreen().availableGeometry()
                self.move(
                    primary.x() + (primary.width() - self.width()) // 2,
                    primary.y() + (primary.height() - self.height()) // 2,
                )
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        # ── Outer shell: custom title bar on top, existing content below ───
        shell = QWidget()
        shell.setObjectName("appShell")
        shell.setProperty("maximized", "false")
        self._shell = shell
        self.setCentralWidget(shell)
        shell_lay = QVBoxLayout(shell)
        # 1px inset == the shell's own border width, so the rounded border
        # (and its corner curve) stays visible instead of being covered by
        # the flush, square-cornered title bar / content beneath it.
        shell_lay.setContentsMargins(1, 1, 1, 1)
        shell_lay.setSpacing(0)

        # ── Custom title bar ────────────────────────────────────────────
        title_bar = _TitleBar(self)
        title_bar.setObjectName("appTitleBar")
        title_bar.setFixedHeight(34)
        self._title_bar = title_bar
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(14, 0, 8, 0)
        tb_lay.setSpacing(2)

        brand_lbl = QLabel()
        brand_lbl.setObjectName("brandLbl")
        _brand_suffix = "indexer" + (" · dev" if DEV_MODE else "")
        brand_lbl.setText(
            f'<span style="color:#e8e8e8;">vael. </span>'
            f'<span style="color:#00d4a0;">{_brand_suffix}</span>'
        )
        tb_lay.addWidget(brand_lbl)
        tb_lay.addStretch()

        self._min_btn = QToolButton()
        self._min_btn.setObjectName("winMinBtn")
        self._min_btn.setIcon(_make_win_icon("min"))
        self._min_btn.setIconSize(QSize(10, 10))
        self._min_btn.setFixedSize(30, 24)
        self._min_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._min_btn.clicked.connect(self.showMinimized)

        self._max_btn = QToolButton()
        self._max_btn.setObjectName("winMaxBtn")
        self._max_btn.setIcon(_make_win_icon("max"))
        self._max_btn.setIconSize(QSize(10, 10))
        self._max_btn.setFixedSize(30, 24)
        self._max_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._max_btn.clicked.connect(self._toggle_maximize)

        self._close_win_btn = QToolButton()
        self._close_win_btn.setObjectName("winCloseBtn")
        self._close_win_btn.setIcon(_make_win_icon("close"))
        self._close_win_btn.setIconSize(QSize(10, 10))
        self._close_win_btn.setFixedSize(30, 24)
        self._close_win_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._close_win_btn.clicked.connect(self.close)

        tb_lay.addWidget(self._min_btn)
        tb_lay.addWidget(self._max_btn)
        tb_lay.addWidget(self._close_win_btn)

        shell_lay.addWidget(title_bar)

        root = QWidget()
        root.setObjectName("appRoot")
        shell_lay.addWidget(root, 1)
        self._content_root = root
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        # Resize grip for the bottom-right corner (frameless windows lose
        # the OS's native resize handles, so we provide a small one).
        self._size_grip = QSizeGrip(shell)
        self._size_grip.setFixedSize(14, 14)
        if self._is_windows:
            # Windows already has a native resize border (WS_THICKFRAME is
            # kept intact -- see nativeEvent() below), so the manual grip
            # would just be a redundant overlay.
            self._size_grip.setVisible(False)

        # ── Top row ───────────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._menu_btn = QToolButton()
        self._menu_btn.setText("≡")
        self._menu_btn.setObjectName("menuBtn")
        self._menu_btn.setFixedSize(26, 26)
        self._menu_btn.clicked.connect(self._toggle_app_menu)

        self._search = QLineEdit()
        self._search.setObjectName("mainSearch")
        self._search.setPlaceholderText("Press / to search...")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._on_search_text_changed)
        self._search.returnPressed.connect(self._on_search_return_pressed)

        # Suppress the default right-click context menu on the search bar
        self._search.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Clear button – real QToolButton overlaid inside the search field
        self._search_clear_btn = QToolButton(self._search)
        self._search_clear_btn.setText("Clear")
        self._search_clear_btn.setObjectName("searchClearBtn")
        self._search_clear_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._search_clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._search_clear_btn.hide()
        self._search_clear_btn.clicked.connect(self._search.clear)
        # Right-pad the text so it doesn't run under the button
        self._search.setTextMargins(0, 0, 36, 0)
        self._search.installEventFilter(self)

        # Debounce timer: fires _do_search 1 s after the user stops typing
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(1000)
        self._search_timer.timeout.connect(self._do_search)

        top_row.addWidget(self._menu_btn)
        top_row.addWidget(self._search, 1)

        self._note_btn = QToolButton()
        self._note_btn.setText("\uf249")
        self._note_btn.setObjectName("noteBtn")
        self._note_btn.setFixedSize(26, 26)
        self._note_btn.setToolTip("Notes")
        self._note_btn.clicked.connect(self._open_note_window)
        top_row.addWidget(self._note_btn)

        outer.addLayout(top_row)

        # ── Status bar ────────────────────────────────────────────────────
        status_bar = QWidget()
        status_bar.setObjectName("statusBar")
        status_bar.setFixedHeight(22)
        sb_lay = QHBoxLayout(status_bar)
        sb_lay.setContentsMargins(8, 0, 8, 0)
        sb_lay.setSpacing(8)

        self._db_lbl = QLabel("-")
        self._db_lbl.setObjectName("dbLbl")

        dot = QLabel("·")
        dot.setObjectName("statusDot")

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setObjectName("statusLbl")

        sb_lay.addWidget(self._db_lbl)
        sb_lay.addWidget(dot)
        sb_lay.addWidget(self._status_lbl)
        sb_lay.addStretch()

        self._sort_btn = QPushButton("A-Z")
        self._sort_btn.setObjectName("sortBtn")
        self._sort_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._sort_btn.clicked.connect(self._toggle_sort)
        sb_lay.addWidget(self._sort_btn)

        outer.addWidget(status_bar)

        # ── Results canvas ────────────────────────────────────────────────
        self._results = ResultsPanel()
        self._results.setMinimumHeight(340)
        outer.addWidget(self._results, 1)
        # Indexing is started by run_startup_scripts() called after show()

        # ── Image Viewer Overlay ───────────────────────────────────────────
        # Parented to the central widget so it covers the full app area.
        self._img_viewer = ImgViewerOverlay(root)
        self._img_viewer.resize(root.size())
        self._results.card_view_requested.connect(self._img_viewer.open_viewer)

        # Apply the rounded-corner clip mask now that geometry is final.
        self._update_window_mask()

    # ── Startup script execution ──────────────────────────────────────────

    def run_startup_scripts(self) -> None:
        """Run saved startup scripts then begin indexing. Called after show()."""
        # ── DEV MODE BRANCH ───────────────────────────────────────────────────
        # When --dev is active we skip ALL startup scripts and ALL real database
        # loading.  We inject the in-memory DevDatabase directly into the results
        # panel and display a status message.  No prefs are read or written here.
        # ─────────────────────────────────────────────────────────────────────
        if DEV_MODE:
            self._db_lbl.setText("[DEV MODE]")
            self._set_status("Dev mode - fake data, no disk access")
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        # ── PRODUCTION PATH (unchanged) ───────────────────────────────────────
        scripts = _load_scripts()
        if not scripts:
            self._pick_initial_db()
            return

        self._results.show_loading(f"Executing Startup Scripts (1/{len(scripts)})")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        runner = ScriptRunner(scripts)
        runner.progress.connect(self._on_script_progress)
        runner.finished.connect(self._on_scripts_finished)
        self._script_runner = runner
        runner.start()

    def _on_script_progress(self, current: int, total: int, msg: str) -> None:
        self._results.update_loading(msg)
        self._set_status(msg)

    def _on_scripts_finished(self) -> None:
        self._script_runner = None
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        self._pick_initial_db()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _pick_initial_db(self) -> None:
        prefs = _load_prefs()
        last = prefs.get("last_db", "")
        names = self.db_manager.names()
        if not names:
            self._set_status("No folders yet - use ≡ → Add Folder")
            return
        target = last if last in names else names[0]
        self._start_load_db(target)

    def _set_active_db(self, name: str) -> None:
        self._active_db = name
        self._db_lbl.setText(name or "-")
        # ── DEV MODE: never overwrite last_db - user's real session must survive
        if not DEV_MODE:
            prefs = _load_prefs()
            prefs["last_db"] = name
            _save_prefs(prefs)
        db = self.db_manager.get(name) if name else None
        root_folder = self.db_manager.root_for(name) if name else None
        self._results.set_db(db, root_folder)
        self._do_search()

    def _start_load_db(self, name: str, full_rebuild: bool = False) -> None:
        """Begin background indexing for a database, showing the loading overlay.

        On a normal (non-rebuild) startup the file cache is diffed first so only
        changed/added/deleted files are re-indexed.  If no files changed at all
        the worker is skipped entirely.  full_rebuild bypasses the cache.
        """
        if self._index_worker and self._index_worker.isRunning():
            return  # already busy

        self._active_db = name
        self._db_lbl.setText(name or "-")
        self._set_status("Starting...")
        self._results.show_loading("Starting...")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        db = self.db_manager.get(name)
        folder = self.db_manager.root_for(name)
        if db is None or folder is None or not folder.exists():
            self._results.hide_loading()
            self._search.setEnabled(True)
            self._menu_btn.setEnabled(True)
            self._set_active_db(name)
            return

        # ── Cache diff (skipped on full rebuild) ─────────────────────────────────────────
        flagged: Optional[tuple[set[str], set[str], set[str]]] = None
        if not full_rebuild:
            cache = _load_file_cache(name)
            if cache:
                self._results.update_loading("Checking for changes...")
                self._set_status("Checking for changes...")
                QApplication.processEvents()
                added, changed, deleted = _diff_against_cache(folder, cache)
                flagged = (added, changed, deleted)
                n_changes = len(added) + len(changed) + len(deleted)
                if n_changes == 0:
                    # Nothing changed — skip indexing entirely
                    self._results.hide_loading()
                    self._search.setEnabled(True)
                    self._menu_btn.setEnabled(True)
                    self._set_active_db(name)
                    db_total = db.count()
                    self._set_status(f"{db_total} assets (no changes)")
                    return
            # No cache yet (first run) — flagged stays None → full scan

        worker = IndexWorker(db.path, folder, full_rebuild=full_rebuild, flagged=flagged)
        worker.progress.connect(self._on_index_progress)
        worker.finished.connect(
            lambda total, n=name, rb=full_rebuild: self._on_index_finished(n, total, rb)
        )
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, current: int, total: int, msg: str) -> None:
        self._results.update_loading(msg)
        self._set_status(msg)

    def _on_index_finished(self, name: str, total: int, full_rebuild: bool = False) -> None:
        # Don't hide the overlay yet - _set_active_db → refresh() will show
        # "Rendering..." and hide the overlay once _populate() completes.
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        self._set_active_db(name)
        self._set_status(f"{total} assets")
        self._index_worker = None

        # ── Write updated file cache after a successful index ───────────────────
        # Rebuild the cache from disk so it reflects the current state exactly.
        # Runs in the main thread after indexing; the rglob is stat-only (fast).
        folder = self.db_manager.root_for(name)
        if folder is not None and folder.exists():
            new_cache = _build_file_cache(folder)
            _save_file_cache(name, new_cache)

    def _open_note_window(self) -> None:
        if self._note_window is None:
            self._note_window = NoteWindow(self)
        self._note_window.show_and_reload()

    def eventFilter(self, obj, event) -> bool:
        if obj is self._search and event.type() == QEvent.Type.Resize:
            btn = self._search_clear_btn
            btn.adjustSize()
            btn.move(self._search.width() - btn.width() - 2, (self._search.height() - btn.height()) // 2)
        return super().eventFilter(obj, event)

    def _on_search_text_changed(self) -> None:
        text = self._search.text().strip()

        # Show/hide the inline clear button
        self._search_clear_btn.setVisible(bool(text))
        if bool(text):
            self._search_clear_btn.adjustSize()
            btn = self._search_clear_btn
            btn.move(self._search.width() - btn.width() - 2, (self._search.height() - btn.height()) // 2)

        # Snapshot expanded folders + scroll position the moment the user starts a new search
        if text and self._pre_search_expanded is None:
            self._pre_search_expanded = self._results._get_expanded_keys()
            self._pre_search_scroll = self._results.verticalScrollBar().value()

        # If the field just became empty, restore immediately without waiting
        if not text:
            self._search_timer.stop()
            self._do_search()
            return

        self._search_timer.start()  # restart the 1-second debounce window

    def _on_search_return_pressed(self) -> None:
        """User pressed Enter – skip the debounce wait and search immediately."""
        if self._search.text().strip():
            self._search_timer.stop()
            self._do_search()

    def _do_search(self) -> None:
        self._search_timer.stop()
        text = self._search.text().strip()

        # ── JSON-contents mode: query starting with ';' ────────────────────────
        # e.g. ";short hair" → search the raw JSON data for "short hair"
        # anywhere it appears (keys or values), instead of just the asset name.
        # Takes precedence over folder-only mode below.
        json_only = False
        folder_only = False
        id_only = False
        effective_text = text
        if text.startswith(";"):
            json_only = True
            effective_text = text[1:].strip()  # strip the leading ';'
        # ── Identifier-field mode: query starting with 'id-' ──────────────────
        # e.g. "id-adult" → case-insensitive substring match against the
        # "Identifier" field's value only (not any other field or the name).
        elif text.lower().startswith("id-"):
            id_only = True
            effective_text = text[3:].strip()  # strip the leading 'id-'
        # ── Folder-only mode: query ending with ' f' ──────────────────────────
        # e.g. "pokemon f" → search only folder names for "pokemon"
        elif text.lower().endswith(" f"):
            folder_only = True
            effective_text = text[:-2].strip()  # strip the trailing ' f'

        if (
            not effective_text
            and not folder_only
            and not json_only
            and not id_only
            and self._pre_search_expanded is not None
        ):
            # Search cleared → restore exactly where the user was
            self._results.refresh(
                "",
                restore_keys=self._pre_search_expanded,
                restore_scroll=self._pre_search_scroll,
            )
            self._pre_search_expanded = None
            self._pre_search_scroll = 0
        else:
            self._results.refresh(
                effective_text,
                folder_only=folder_only,
                json_only=json_only,
                id_only=id_only,
            )

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)

    def _toggle_sort(self) -> None:
        self._results._az_sort = not self._results._az_sort
        self._sort_btn.setText("A-Z" if self._results._az_sort else "Z-A")
        self._do_search()

    # ── App menu ──────────────────────────────────────────────────────────

    def _toggle_app_menu(self) -> None:
        if self._app_menu is not None:
            self._app_menu.close()
            self._app_menu = None
            return

        menu = QMenu(self)
        # ── DEV MODE: annotate menu items that are suppressed or limited ──────
        if DEV_MODE:
            menu.addAction("⚠ Dev Mode Active - no real DB loaded").setEnabled(False)
            menu.addSeparator()
        reload_menu = menu.addMenu("Reload Database")
        reload_menu.addAction("without Scripts", self._action_reload)
        reload_menu.addAction("with Scripts", self._action_reload_with_scripts)
        menu.addAction("Change Database", self._action_open_db)
        menu.addAction("Add Folder", self._action_add_folder)
        menu.addSeparator()
        tagged_label = "Hide Tagged" if self._show_tagged else "Show Tagged"
        menu.addAction(tagged_label, self._action_toggle_tagged)
        menu.addSeparator()
        menu.addAction("Startup Scripts", self._action_startup_scripts)
        menu.addAction("Manage ID", self._action_manage_identifiers)
        menu.aboutToHide.connect(self._on_menu_hide)
        self._app_menu = menu
        menu.exec(self._menu_btn.mapToGlobal(self._menu_btn.rect().bottomLeft()))

    def _on_menu_hide(self) -> None:
        self._app_menu = None

    def _action_reload(self) -> None:
        # ── DEV MODE: reset the in-memory stub and re-render fake data ────────
        if DEV_MODE:
            self._dev_db = DevDatabase()
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        if self._active_db:
            self._start_load_db(self._active_db)

    def _action_reload_with_scripts(self) -> None:
        """Run startup scripts first, then reload the active database."""
        # ── DEV MODE: just reset the stub (no real scripts to run) ───────────
        if DEV_MODE:
            self._dev_db = DevDatabase()
            self._results.set_db(self._dev_db)  # type: ignore[arg-type]
            self._do_search()
            return
        scripts = _load_scripts()
        if not scripts:
            # No scripts configured - fall back to a plain reload
            if self._active_db:
                self._start_load_db(self._active_db)
            return

        self._results.show_loading(f"Executing Startup Scripts (1/{len(scripts)})")
        self._search.setEnabled(False)
        self._menu_btn.setEnabled(False)

        runner = ScriptRunner(scripts)
        runner.progress.connect(self._on_script_progress)
        runner.finished.connect(self._on_reload_scripts_finished)
        self._script_runner = runner
        runner.start()

    def _on_reload_scripts_finished(self) -> None:
        """Called when startup scripts finish during a 'Reload with Scripts'."""
        self._script_runner = None
        self._search.setEnabled(True)
        self._menu_btn.setEnabled(True)
        if self._active_db:
            self._start_load_db(self._active_db)

    def _action_open_db(self) -> None:
        dlg = OpenDatabaseDialog(self.db_manager, self._active_db, self)
        if dlg.exec() and dlg.chosen:
            name = dlg.chosen
            if name != self._active_db:
                # Unload the current db from memory if it isn't the same
                if self._active_db:
                    self.db_manager.unload(self._active_db)
                self._start_load_db(name)

    def _action_add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to add")
        if not path:
            return
        folder = Path(path)
        name = self.db_manager.add_folder(folder)
        if self._active_db:
            self.db_manager.unload(self._active_db)
        self._start_load_db(name)

    def _action_toggle_tagged(self) -> None:
        """Toggle 'Show Tagged' mode.

        Default (off) state = 'Hide Tagged': app behaves as normal.
        On state = 'Show Tagged': every visible image's name is highlighted,
        and folders whose images are all tagged (including nested
        subfolders) get an orange dot next to their name.
        """
        self._show_tagged = not self._show_tagged
        self._results.set_tagged_mode(self._show_tagged)

    def _action_startup_scripts(self) -> None:
        # ── DEV MODE: open dialog in readonly mode - fully interactive but
        #    nothing is written to disk. Changes are lost on close.
        dlg = StartupScriptsDialog(self, readonly=DEV_MODE)
        dlg.exec()

    def _action_manage_identifiers(self) -> None:
        """Identifiers are a per-database feature, so this needs an active
        database (or the in-memory dev stub while in DEV_MODE)."""
        db = self._dev_db if DEV_MODE else self.db_manager.get(self._active_db)
        if db is None:
            QMessageBox.information(
                self, APP_NAME, "Open a database first to manage its Identifiers."
            )
            return
        dlg = ManageIdentifiersDialog(db, self, readonly=DEV_MODE)
        dlg.exec()
        # Reflects any Identifier field changes made indirectly, though
        # Manage Identifiers itself doesn't touch asset JSON - safe no-op
        # refresh keeps the results view consistent if a rename affected it.
        self._do_search()

    # ── Windows: native hit-testing so the OS drives move/resize/snap ─────

    def nativeEvent(self, eventType, message):
        if self._is_windows and eventType in ("windows_generic_MSG", b"windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_NCCALCSIZE:
                if msg.wParam:
                    if self.isMaximized():
                        # Inset the maximized client rect by the standard
                        # frame size so the window doesn't hang off the
                        # edges of the monitor / over the taskbar.
                        params = _NCCALCSIZE_PARAMS.from_address(msg.lParam)
                        cx = ctypes.windll.user32.GetSystemMetrics(
                            _SM_CXSIZEFRAME
                        ) + ctypes.windll.user32.GetSystemMetrics(_SM_CXPADDEDBORDER)
                        cy = ctypes.windll.user32.GetSystemMetrics(
                            _SM_CYSIZEFRAME
                        ) + ctypes.windll.user32.GetSystemMetrics(_SM_CXPADDEDBORDER)
                        params.rgrc[0].left += cx
                        params.rgrc[0].top += cy
                        params.rgrc[0].right -= cx
                        params.rgrc[0].bottom -= cy
                    return True, 0
            elif msg.message == _WM_NCHITTEST:
                return self._win_hit_test(msg)
        return super().nativeEvent(eventType, message)

    def _win_hit_test(self, msg):
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        local = self.mapFromGlobal(QPoint(x, y))
        w, h, b = self.width(), self.height(), _RESIZE_BORDER_PX

        if not self.isMaximized() and not self.isFullScreen():
            if local.x() < b and local.y() < b:
                return True, _HTTOPLEFT
            if local.x() >= w - b and local.y() < b:
                return True, _HTTOPRIGHT
            if local.x() < b and local.y() >= h - b:
                return True, _HTBOTTOMLEFT
            if local.x() >= w - b and local.y() >= h - b:
                return True, _HTBOTTOMRIGHT
            if local.x() < b:
                return True, _HTLEFT
            if local.x() >= w - b:
                return True, _HTRIGHT
            if local.y() < b:
                return True, _HTTOP
            if local.y() >= h - b:
                return True, _HTBOTTOM

        title_bar = self._title_bar
        if title_bar.geometry().contains(local):
            child = title_bar.childAt(title_bar.mapFrom(self, local))
            if not isinstance(child, QToolButton):
                return True, _HTCAPTION

        return True, _HTCLIENT

    # ── Border resize (frameless windows have no native resize edges) ────
    #
    # FramelessWindowHint strips the OS's resize borders, so without this the
    # only way to resize the window is the tiny 14x14 grip in the corner.
    # This restores dragging from any edge/corner by detecting proximity to
    # the window's border and delegating to the OS's own resize via
    # QWindow.startSystemResize(), which is smoother and more reliable than
    # hand-rolling geometry updates on every mouse move.

    def _edges_at(self, global_pos: QPoint) -> Qt.Edges:
        if self.isMaximized() or self.isFullScreen():
            return Qt.Edges()
        r = self.frameGeometry()
        m = self._RESIZE_MARGIN
        edges = Qt.Edges()
        if abs(global_pos.x() - r.left()) <= m:
            edges |= Qt.Edge.LeftEdge
        elif abs(global_pos.x() - r.right()) <= m:
            edges |= Qt.Edge.RightEdge
        if abs(global_pos.y() - r.top()) <= m:
            edges |= Qt.Edge.TopEdge
        elif abs(global_pos.y() - r.bottom()) <= m:
            edges |= Qt.Edge.BottomEdge
        return edges

    _EDGE_CURSORS = {
        frozenset({Qt.Edge.LeftEdge}): Qt.CursorShape.SizeHorCursor,
        frozenset({Qt.Edge.RightEdge}): Qt.CursorShape.SizeHorCursor,
        frozenset({Qt.Edge.TopEdge}): Qt.CursorShape.SizeVerCursor,
        frozenset({Qt.Edge.BottomEdge}): Qt.CursorShape.SizeVerCursor,
        frozenset({Qt.Edge.TopEdge, Qt.Edge.LeftEdge}): Qt.CursorShape.SizeFDiagCursor,
        frozenset({Qt.Edge.BottomEdge, Qt.Edge.RightEdge}): Qt.CursorShape.SizeFDiagCursor,
        frozenset({Qt.Edge.TopEdge, Qt.Edge.RightEdge}): Qt.CursorShape.SizeBDiagCursor,
        frozenset({Qt.Edge.BottomEdge, Qt.Edge.LeftEdge}): Qt.CursorShape.SizeBDiagCursor,
    }

    def eventFilter(self, obj, event) -> bool:
        et = event.type()
        if et in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress) and self.isVisible():
            gpos = event.globalPosition().toPoint()
            # Only react when the cursor is actually over *this* window
            # (not some other dialog that happens to overlap it).
            under = QApplication.widgetAt(gpos)
            if under is None or not (under is self or self.isAncestorOf(under)):
                return super().eventFilter(obj, event)

            edges = self._edges_at(gpos)
            if et == QEvent.Type.MouseMove:
                if edges:
                    cursor = self._EDGE_CURSORS.get(frozenset(self._edge_set(edges)))
                    if cursor is not None:
                        self.setCursor(cursor)
                else:
                    self.unsetCursor()
            elif et == QEvent.Type.MouseButtonPress:
                if edges and event.button() == Qt.MouseButton.LeftButton:
                    wh = self.windowHandle()
                    if wh is not None:
                        wh.startSystemResize(edges)
                        return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _edge_set(edges: Qt.Edges) -> set:
        result = set()
        for e in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
            if edges & e:
                result.add(e)
        return result

    # ── Resize: keep overlay filling the central widget ──────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._img_viewer is not None:
            self._img_viewer.resize(self._content_root.size())
        if self._size_grip is not None:
            self._size_grip.move(
                self.width() - self._size_grip.width() - 2,
                self.height() - self._size_grip.height() - 2,
            )
            self._size_grip.raise_()
        self._update_window_mask()

    def _toggle_maximize(self, force_normal: bool = False) -> None:
        if force_normal or self.isMaximized():
            self.showNormal()
            self._max_btn.setIcon(_make_win_icon("max"))
        else:
            self.showMaximized()
            self._max_btn.setIcon(_make_win_icon("restore"))
        self._sync_shell_rounding()

    def _sync_shell_rounding(self) -> None:
        """Square off the shell's corners while maximized/fullscreen (there's
        no desktop showing behind it to round away from), and restore the
        rounded look the rest of the time."""
        is_max = self.isMaximized() or self.isFullScreen()
        prop = "true" if is_max else "false"
        if self._shell.property("maximized") != prop:
            self._shell.setProperty("maximized", prop)
            self._shell.style().unpolish(self._shell)
            self._shell.style().polish(self._shell)
        self._update_window_mask()

    def _update_window_mask(self) -> None:
        """Physically clip the top-level window to a rounded-rect region.
        WA_TranslucentBackground + a "background: transparent" stylesheet
        rule is supposed to make the four corners outside #appShell's
        rounded border see-through, but that depends on desktop compositing
        actually producing a real per-pixel-alpha surface -- and on some
        machines/setups that silently fails, leaving the square corners
        filled with the app's background color instead of transparent.
        setMask() sidesteps that entirely: it clips the *actual window
        shape* at the OS level, so the corners are genuinely cut away
        regardless of whether translucency compositing is working.
        """
        if self.objectName() != "mainWindowFrameless":
            return
        if self.isMaximized() or self.isFullScreen() or self.width() <= 0 or self.height() <= 0:
            self.clearMask()
            return
        from PySide6.QtGui import QPainterPath, QRegion

        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 10, 10)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_shell_rounding()
            self._max_btn.setIcon(_make_win_icon("restore" if self.isMaximized() else "max"))
        super().changeEvent(event)

    # ── Keyboard shortcuts ────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Slash and not self._search.hasFocus():
            self._search.setFocus()
            self._search.clear()
            event.accept()
        else:
            super().keyPressEvent(event)

    # ── Window close ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        QApplication.instance().removeEventFilter(self)
        # Stop any running worker
        if self._index_worker and self._index_worker.isRunning():
            self._index_worker.quit()
            self._index_worker.wait(2000)
        # Close all open database connections
        self.db_manager.close_all()
        # Clear pixmap cache to release file handles
        _PIXMAP_CACHE.clear()
        # ── Persist window geometry so the next launch reopens here ───────
        if not DEV_MODE:
            _prefs = _load_prefs()
            _prefs["main_window_geometry"] = {
                "x": self.x(),
                "y": self.y(),
                "width": self.width(),
                "height": self.height(),
            }
            _save_prefs(_prefs)
        super().closeEvent(event)


# ── Notes helpers ──────────────────────────────────────────────────────────────


def _load_notes() -> dict:
    try:
        if NOTES_FILE.exists():
            return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_notes(data: dict) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        NOTES_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


_AZ_SORT_KEY = "A-Z Sort in Folder"


def _ensure_az_sort_flag(data: dict) -> dict:
    """Check for the 'A-Z Sort in Folder' flag at the root of the notes JSON.

    - If the key is missing it is inserted at the very top (position 0) with
      value True and the file is saved immediately.
    - If the key already exists its value is left unchanged.
    - Returns the (possibly updated) data dict.
    """
    if _AZ_SORT_KEY not in data:
        # Insert at the very top by rebuilding the dict with the flag first
        updated = {_AZ_SORT_KEY: True}
        updated.update(data)
        _save_notes(updated)
        return updated
    return data


def _flatten_notes(data: dict, query: str = "", category_path: str = "") -> list[dict]:
    """Recursively flatten notes JSON into {name, value, category} dicts.

    The 'A-Z Sort in Folder' key is a control flag, not a user entry; it is
    silently skipped at every level so it never appears as a copyable card.
    """
    results: list[dict] = []
    for key, value in data.items():
        if key == _AZ_SORT_KEY:
            continue  # internal flag – never shown as a card
        if isinstance(value, str):
            if not query or query.lower() in key.lower():
                results.append({"name": key, "value": value, "category": category_path})
        elif isinstance(value, dict):
            sub = f"{category_path}/{key}" if category_path else key
            results.extend(_flatten_notes(value, query, sub))
    return results


def _set_nested(data: dict, keys: list, name: str, value: str) -> None:
    if not keys:
        data[name] = value
        return
    k = keys[0]
    if k not in data or not isinstance(data[k], dict):
        data[k] = {}
    _set_nested(data[k], keys[1:], name, value)


def _add_note_entry(name: str, value: str, category: str) -> None:
    data = _load_notes()
    if category:
        parts = [p for p in category.split("/") if p]
        _set_nested(data, parts, name, value)
    else:
        data[name] = value
    _save_notes(data)


# ── Note Entry Card ────────────────────────────────────────────────────────────


class NoteEntryCard(QWidget):
    CARD_W = THUMB_W  # 106 px
    CARD_H = 80  # 4:3 ratio  (106 × 3/4 ≈ 80)

    def __init__(
        self,
        name: str,
        value: str,
        notes_file: Path,
        panel: "NotePanel",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._name = name
        self._value = value
        self._notes_file = notes_file
        self._panel = panel
        self._flashing = False
        self._flash_timer: Optional[QTimer] = None
        self._hovered = False

        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("noteEntry")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(0)

        # Equal stretch=1 gives each label exactly half the card height.
        # AlignBottom on Name keeps it visually near the centre-line from above;
        # AlignTop on Value keeps it near the centre-line from below.
        name_lbl = QLabel(name)
        name_lbl.setObjectName("noteEntryName")
        name_lbl.setAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
        )
        lay.addWidget(name_lbl, 1)

        val_lbl = QLabel(value)
        val_lbl.setObjectName("noteEntryValue")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(val_lbl, 1)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._copy_value()
        super().mousePressEvent(event)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def _copy_value(self) -> None:
        QApplication.clipboard().setText(self._value)
        self._flashing = True
        self.update()
        if self._flash_timer:
            self._flash_timer.stop()
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._flash_timer.start(350)

    def _end_flash(self) -> None:
        self._flashing = False
        self._flash_timer = None
        self.update()

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QPainterPath, QPen

        super().paintEvent(event)
        if self._flashing:
            # Clicked: subtle green fill + border
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(80, 200, 120, 28))
            pen = QPen(QColor(80, 200, 120, 90))
            pen.setWidth(2)
            p.setPen(pen)
            path = QPainterPath()
            path.addRoundedRect(1, 1, self.CARD_W - 2, self.CARD_H - 2, 7, 7)
            p.drawPath(path)
            p.end()
        elif self._hovered:
            # Hover: very faint white/neutral tint
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor(255, 255, 255, 10))
            pen = QPen(QColor(255, 255, 255, 35))
            pen.setWidth(1)
            p.setPen(pen)
            path = QPainterPath()
            path.addRoundedRect(1, 1, self.CARD_W - 2, self.CARD_H - 2, 7, 7)
            p.drawPath(path)
            p.end()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setObjectName("cardMenu")
        copy_act = menu.addAction("Copy")
        menu.addSeparator()
        edit_act = menu.addAction("Edit Json")
        chosen = menu.exec(event.globalPos())
        if chosen is copy_act:
            QApplication.clipboard().setText(self._value)
        elif chosen is edit_act:
            self._open_edit_json()

    def _open_edit_json(self) -> None:
        try:
            raw = (
                self._notes_file.read_text(encoding="utf-8")
                if self._notes_file.exists()
                else "{}"
            )
            raw = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
        except Exception:
            raw = "{}"
        fake = {
            "image_path": "",
            "json_path": str(self._notes_file),
            "json_data": raw,
            "name": "notes",
        }
        dlg = EditJsonDialog(
            fake, None, self, focus_key=self._name, focus_value=self._value
        )
        if dlg.exec():
            self._panel.reload()


# ── Note Section ───────────────────────────────────────────────────────────────


class NoteSection(QWidget):
    def __init__(
        self,
        title: str,
        depth: int = 0,
        notes_file: Optional[Path] = None,
        category_path: str = "",
        global_az_sort: bool = True,
        panel: Optional["NotePanel"] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        # See FolderSection: without an objectName this widget only matches
        # the blanket "QWidget { background: ... }" rule and paints itself
        # opaque, hiding the #resultsCanvas gradient behind every row.
        self.setObjectName("folderSection")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._expanded = False
        self._depth = depth
        self._child_sections: list["NoteSection"] = []
        self._notes_file = notes_file
        self._category_path = category_path   # e.g. "Animals/Dogs"
        self._global_az_sort = global_az_sort
        self._panel_ref: Optional["NotePanel"] = panel

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header ──────────────────────────────────────────────────────
        header_container = QWidget()
        header_container.setObjectName("sectionHeaderWrap")
        header_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hc_lay = QHBoxLayout(header_container)
        indent = depth * 14
        hc_lay.setContentsMargins(indent, 0, 0, 0)
        hc_lay.setSpacing(4)

        self._header = QToolButton()
        self._header.setObjectName("sectionHeader")
        self._header.setProperty("depth0", "true" if depth == 0 else "false")
        self._header.setProperty("expanded", "false")
        self._header.setCheckable(False)
        self._header.setArrowType(Qt.ArrowType.RightArrow)
        self._header.setText(title)
        self._header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        self._header.setFixedHeight(26 if depth == 0 else 22)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.clicked.connect(self._toggle)
        self._header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._header.customContextMenuRequested.connect(
            lambda pos: self._on_header_context_menu(self._header.mapToGlobal(pos))
        )

        hc_lay.addWidget(self._header, 0)
        hc_lay.addStretch()
        outer.addWidget(header_container)

        # ── Body ─────────────────────────────────────────────────────────
        self._body = QWidget()
        self._body.setObjectName("sectionBody")
        self._body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        self._body_lay.setSpacing(2)

        self._card_widget = QWidget()
        self._card_widget.setObjectName("cardGrid")
        self._card_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self._card_grid = QGridLayout(self._card_widget)
        self._card_grid.setContentsMargins(0, 0, 0, 0)
        self._card_grid.setSpacing(6)
        self._card_grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._body_lay.addWidget(self._card_widget)

        self._body.setVisible(False)
        outer.addWidget(self._body)

        self._cards: list[NoteEntryCard] = []
        self._current_cols: int = COLS

    def _get_local_az_sort(self) -> Optional[bool]:
        """Return folder-local 'A-Z Sort in Folder' from notes.json, or None if absent."""
        if not self._notes_file or not self._notes_file.exists() or not self._category_path:
            return None
        try:
            data = json.loads(self._notes_file.read_text(encoding="utf-8", errors="ignore"))
            # Navigate to this folder's dict
            parts = self._category_path.split("/")
            node = data
            for part in parts:
                if not isinstance(node, dict) or part not in node:
                    return None
                node = node[part]
            if isinstance(node, dict) and _AZ_SORT_KEY in node:
                return bool(node[_AZ_SORT_KEY])
        except Exception:
            pass
        return None

    def _toggle_az_sort(self, current_effective: bool) -> None:
        """Write the inverse of current_effective into this folder's dict in notes.json."""
        if not self._notes_file or not self._category_path:
            return
        try:
            data = (
                json.loads(self._notes_file.read_text(encoding="utf-8", errors="ignore"))
                if self._notes_file.exists()
                else {}
            )
        except Exception:
            data = {}

        # Navigate to this folder's dict, preserving existing content at each level
        parts = self._category_path.split("/")
        node = data
        for part in parts:
            existing = node.get(part)
            if not isinstance(existing, dict):
                node[part] = {}
            node = node[part]

        node[_AZ_SORT_KEY] = not current_effective
        _save_notes(data)

        # Reload the panel so the new sort takes effect immediately
        if self._panel_ref is not None:
            self._panel_ref.reload(self._panel_ref._current_query)

    def _on_header_context_menu(self, global_pos) -> None:
        if not self._expanded:
            return

        local_val = self._get_local_az_sort()
        effective_az = local_val if local_val is not None else self._global_az_sort

        menu = QMenu(self)
        menu.setObjectName("cardMenu")

        # Label shows the CURRENT state (what is active right now):
        # "A-Z Sort"      → currently sorting A-Z  (local=true OR no local + global=true)
        # "Standard Sort" → currently standard sort (local=false OR no local + global=false)
        if effective_az:
            sort_act = menu.addAction("A-Z Sort")
        else:
            sort_act = menu.addAction("Standard Sort")

        chosen = menu.exec(global_pos)
        if chosen is sort_act:
            self._toggle_az_sort(effective_az)

    def add_card(
        self, name: str, value: str, notes_file: Path, panel: "NotePanel"
    ) -> None:
        card = NoteEntryCard(name, value, notes_file, panel)
        i = len(self._cards)
        self._cards.append(card)
        row = i // COLS
        self._card_grid.addWidget(card, row, i % COLS)
        self._card_grid.setRowMinimumHeight(row, 0)
        self._card_grid.setRowStretch(row, 0)

    def add_child_section(self, sec: "NoteSection") -> None:
        self._child_sections.append(sec)
        self._body_lay.addWidget(sec)

    def _relayout_cards(self) -> None:
        avail_w = self._card_widget.width()
        if avail_w < NoteEntryCard.CARD_W:
            return
        cols = max(1, avail_w // (NoteEntryCard.CARD_W + 6))
        if cols == self._current_cols:
            return
        self._current_cols = cols
        while self._card_grid.count():
            self._card_grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._card_grid.addWidget(card, i // cols, i % cols)
        # Force each row to only be as tall as the card — no extra expansion
        num_rows = (len(self._cards) + cols - 1) // cols
        for r in range(num_rows):
            self._card_grid.setRowMinimumHeight(r, 0)
            self._card_grid.setRowStretch(r, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._expanded and self._cards:
            self._relayout_cards()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._body.setVisible(self._expanded)
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if self._expanded else Qt.ArrowType.RightArrow
        )
        self._header.setProperty("expanded", "true" if self._expanded else "false")
        self._header.style().unpolish(self._header)
        self._header.style().polish(self._header)
        if self._expanded and self._cards:
            self._current_cols = 0
            QTimer.singleShot(0, self._relayout_cards)


# ── Note Panel ─────────────────────────────────────────────────────────────────


class NotePanel(QScrollArea):
    def __init__(self, notes_file: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._notes_file = notes_file
        self.setObjectName("resultsPanel")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setObjectName("resultsContent")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(6, 8, 6, 8)
        self._layout.setSpacing(5)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self._content)
        # See ResultsPanel: the gradient background belongs on the fixed-size
        # viewport, not on this ever-growing scroll content widget.
        self.viewport().setObjectName("resultsCanvas")
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._current_query: str = ""

    def _get_expanded_titles(self) -> set[str]:
        """Recursively collect the title text of every expanded NoteSection."""
        titles: set[str] = set()

        def _collect(layout) -> None:
            for i in range(layout.count()):
                item = layout.itemAt(i)
                if item is None:
                    continue
                w = item.widget()
                if isinstance(w, NoteSection):
                    if w._expanded:
                        titles.add(w._header.text())
                    _collect(w._body_lay)

        _collect(self._layout)
        return titles

    def reload(self, query: str = "") -> None:
        self._current_query = query
        # Snapshot state before clearing so we can restore it after repopulating
        expanded_titles = self._get_expanded_titles()
        scroll_value = self.verticalScrollBar().value()
        try:
            data = (
                json.loads(self._notes_file.read_text(encoding="utf-8"))
                if self._notes_file.exists()
                else {}
            )
        except Exception:
            data = {}
        # Ensure the A-Z Sort flag exists; inserts it at the top if missing
        data = _ensure_az_sort_flag(data)
        az_sort = bool(data.get(_AZ_SORT_KEY, True))
        self._populate(data, query, expanded_titles, scroll_value, az_sort)

    def _populate(
        self,
        data: dict,
        query: str = "",
        expanded_titles: set[str] | None = None,
        scroll_value: int = 0,
        az_sort: bool = True,
    ) -> None:
        # Clear layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and (w := item.widget()):
                w.deleteLater()

        all_entries = _flatten_notes(data, query)

        # ── Split entries into root vs. folder ────────────────────────────
        # Root entries (no category) are sorted by the global az_sort flag.
        # Folder entries keep their original JSON insertion order here; each
        # folder's cards are sorted individually below using that folder's
        # effective sort setting (local override → global fallback).
        # The global pre-sort must NOT touch folder entries, because it would
        # bake in A-Z order and make a local "Standard sort" override invisible.
        root_entries = [e for e in all_entries if not e["category"]]
        other_entries = [e for e in all_entries if e["category"]]

        if az_sort:
            root_entries.sort(key=lambda e: _natural_sort_key(e["name"]))

        # ── Root entries (no category) shown at top as flat card grid ────
        if root_entries:
            root_widget = QWidget()
            root_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
            )
            root_grid = QGridLayout(root_widget)
            root_grid.setContentsMargins(0, 2, 0, 6)
            root_grid.setSpacing(6)
            root_grid.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
            )
            for i, entry in enumerate(root_entries):
                card = NoteEntryCard(
                    entry["name"], entry["value"], self._notes_file, self
                )
                root_grid.addWidget(card, i // COLS, i % COLS)
            num_rows = (len(root_entries) + COLS - 1) // COLS
            for r in range(num_rows):
                root_grid.setRowMinimumHeight(r, 0)
                root_grid.setRowStretch(r, 0)
            self._layout.addWidget(root_widget)

        # ── Folder sections for categorised entries ───────────────────────
        if other_entries:
            all_folder_paths: set[str] = set()
            for e in other_entries:
                parts = e["category"].split("/")
                for depth in range(1, len(parts) + 1):
                    all_folder_paths.add("/".join(parts[:depth]))

            folder_list = sorted(all_folder_paths, key=_natural_sort_key)
            sections: dict[str, NoteSection] = {}

            for folder_path in folder_list:
                parts = folder_path.split("/")
                depth = len(parts)
                title = parts[-1]
                sec = NoteSection(
                    title,
                    depth=depth - 1,
                    notes_file=self._notes_file,
                    category_path=folder_path,
                    global_az_sort=az_sort,
                    panel=self,
                )
                sections[folder_path] = sec

                if depth == 1:
                    self._layout.addWidget(sec)
                else:
                    parent_path = "/".join(parts[:-1])
                    if parent_path in sections:
                        sections[parent_path].add_child_section(sec)
                    else:
                        self._layout.addWidget(sec)

            # Group entries by category, then apply per-folder sort before adding
            from collections import defaultdict
            by_cat: dict[str, list[dict]] = defaultdict(list)
            for entry in other_entries:
                by_cat[entry["category"]].append(entry)

            for folder_path, sec in sections.items():
                entries = by_cat.get(folder_path, [])
                if not entries:
                    continue
                # Check for a local A-Z Sort override in this folder's dict
                local_val = sec._get_local_az_sort()
                effective = local_val if local_val is not None else az_sort
                if effective:
                    entries = sorted(entries, key=lambda e: _natural_sort_key(e["name"]))
                for entry in entries:
                    sec.add_card(entry["name"], entry["value"], self._notes_file, self)

        self._layout.addStretch()

        # Re-open any section that was expanded before the refresh
        if expanded_titles:

            def _restore(layout) -> None:
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item is None:
                        continue
                    w = item.widget()
                    if (
                        isinstance(w, NoteSection)
                        and w._header.text() in expanded_titles
                    ):
                        if not w._expanded:
                            w._toggle()
                        _restore(w._body_lay)

            _restore(self._layout)

        # Restore scroll position after layout settles
        if scroll_value:
            QTimer.singleShot(
                0, lambda: self.verticalScrollBar().setValue(scroll_value)
            )


# ── Create Note Dialog ─────────────────────────────────────────────────────────


class CreateNoteDialog(_DraggableDialog):
    _PREFS_KEY = "create_note_pos"
    W, H = 310, 230

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedSize(self.W, self.H)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        shadow_frame = QFrame(self)
        shadow_frame.setObjectName("dialogShadow")
        shadow_frame.setGeometry(4, 4, self.W - 4, self.H - 4)

        frame = QFrame(self)
        frame.setObjectName("editDialogFrame")
        frame.setGeometry(0, 0, self.W - 4, self.H - 4)

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("editDialogHeader")
        header.setFixedHeight(26)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(14, 0, 10, 0)
        h_lay.setSpacing(8)

        dot = QWidget()
        dot.setObjectName("editDialogDot")
        dot.setFixedSize(6, 6)

        title_lbl = QLabel("Create New...")
        title_lbl.setObjectName("editDialogTitle")

        close_btn = QToolButton()
        close_btn.setText("✕")
        close_btn.setObjectName("dbDialogClose")
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(self.reject)

        h_lay.addWidget(dot)
        h_lay.addWidget(title_lbl)
        h_lay.addStretch()
        h_lay.addWidget(close_btn)
        lay.addWidget(header)

        sep = QFrame()
        sep.setObjectName("dbDialogSep")
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)

        # ── Body: three fields ────────────────────────────────────────────
        body = QWidget()
        b_lay = QVBoxLayout(body)
        b_lay.setContentsMargins(14, 10, 14, 8)
        b_lay.setSpacing(8)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("scriptArgsEdit")
        self._name_edit.setPlaceholderText("Name  (required)")
        self._name_edit.setFixedHeight(24)
        b_lay.addWidget(self._name_edit)

        self._value_edit = QLineEdit()
        self._value_edit.setObjectName("scriptArgsEdit")
        self._value_edit.setPlaceholderText("Value  (required)")
        self._value_edit.setFixedHeight(24)
        b_lay.addWidget(self._value_edit)

        self._cat_edit = QLineEdit()
        self._cat_edit.setObjectName("scriptArgsEdit")
        self._cat_edit.setPlaceholderText("Category  (optional, e.g. Pet/Dog)")
        self._cat_edit.setFixedHeight(24)
        b_lay.addWidget(self._cat_edit)

        b_lay.addStretch()
        lay.addWidget(body, 1)

        sep2 = QFrame()
        sep2.setObjectName("dbDialogSep")
        sep2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep2)

        # ── Footer ────────────────────────────────────────────────────────
        footer = QWidget()
        footer.setObjectName("editDialogFooter")
        f_lay = QHBoxLayout(footer)
        f_lay.setContentsMargins(10, 5, 10, 6)
        f_lay.setSpacing(0)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("editSaveBtn")

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("editCancelBtn")

        f_lay.addWidget(save_btn)
        f_lay.addStretch()
        f_lay.addWidget(cancel_btn)
        lay.addWidget(footer)

        save_btn.clicked.connect(self._accept)
        cancel_btn.clicked.connect(self.reject)
        self._name_edit.returnPressed.connect(self._accept)
        self._value_edit.returnPressed.connect(self._accept)
        self._cat_edit.returnPressed.connect(self._accept)

        self._restore_pos()
        QTimer.singleShot(0, self._name_edit.setFocus)

    def _accept(self) -> None:
        name = self._name_edit.text().strip()
        value = self._value_edit.text().strip()
        if not name or not value:
            QMessageBox.warning(self, APP_NAME, "Name and Value are required.")
            return
        category = self._cat_edit.text().strip()
        _add_note_entry(name, value, category)
        self.accept()


# ── Note Window ────────────────────────────────────────────────────────────────


class NoteWindow(QDialog):
    """Floating, non-modal notes window with its own search + canvas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Notes")
        self.setModal(False)
        self.setMinimumSize(480, 360)
        self.resize(720, 540)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        # Make the dialog background match the main app window exactly
        self.setObjectName("noteWindowRoot")

        # Restore session-only position (resets to centre each app start).
        # Actual centering happens in showEvent once geometry is finalised.
        pos = _SESSION_POS.get("note_window_pos")
        if pos and len(pos) == 2:
            self.move(pos[0], pos[1])

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(600)
        self._search_timer.timeout.connect(self._do_search)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)

        # ── Top row: "+" button + search bar ─────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._add_btn = QToolButton()
        self._add_btn.setText("+")
        self._add_btn.setObjectName("noteAddBtn")
        self._add_btn.setFixedSize(26, 26)
        # Force the text to center properly
        self._add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._add_btn.clicked.connect(self._open_create_dialog)

        self._search = QLineEdit()
        self._search.setObjectName("noteSearch")
        self._search.setPlaceholderText("Search notes by name...")
        self._search.setFixedHeight(26)
        self._search.textChanged.connect(self._on_search_changed)
        self._search.returnPressed.connect(self._on_search_return_pressed)
        self._search.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Clear button overlaid inside the note search field
        self._search_clear_btn = QToolButton(self._search)
        self._search_clear_btn.setText("Clear")
        self._search_clear_btn.setObjectName("noteSearchClearBtn")
        self._search_clear_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self._search_clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._search_clear_btn.hide()
        self._search_clear_btn.clicked.connect(self._search.clear)
        self._search.setTextMargins(0, 0, 36, 0)
        self._search.installEventFilter(self)

        top_row.addWidget(self._add_btn)
        top_row.addWidget(self._search, 1)
        lay.addLayout(top_row)

        # ── Canvas ────────────────────────────────────────────────────────
        self._panel = NotePanel(NOTES_FILE)
        self._panel.setMinimumHeight(280)
        lay.addWidget(self._panel, 1)

    def closeEvent(self, event) -> None:
        _SESSION_POS["note_window_pos"] = [self.x(), self.y()]
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Centre over the parent on the very first show of each session.
        # If the user moved the window this session, _SESSION_POS will have been
        # set in closeEvent and the __init__ move() placed it correctly already.
        if "note_window_pos" not in _SESSION_POS:
            parent = self.parent()
            if parent is not None:
                center = parent.window().frameGeometry().center()
            else:
                center = QApplication.primaryScreen().availableGeometry().center()
            fg = self.frameGeometry()
            fg.moveCenter(center)
            self.move(fg.topLeft())

    def show_and_reload(self) -> None:
        """Show (or raise) the window and reload note data."""
        self._panel.reload(self._search.text().strip())
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _open_create_dialog(self) -> None:
        dlg = CreateNoteDialog(self)
        if dlg.exec():
            self._panel.reload(self._search.text().strip())

    def eventFilter(self, obj, event) -> bool:
        if obj is self._search and event.type() == QEvent.Type.Resize:
            btn = self._search_clear_btn
            btn.adjustSize()
            btn.move(self._search.width() - btn.width() - 2, (self._search.height() - btn.height()) // 2)
        return super().eventFilter(obj, event)

    def _on_search_changed(self) -> None:
        text = self._search.text().strip()

        # Show/hide the inline clear button
        self._search_clear_btn.setVisible(bool(text))
        if bool(text):
            self._search_clear_btn.adjustSize()
            btn = self._search_clear_btn
            btn.move(self._search.width() - btn.width() - 2, (self._search.height() - btn.height()) // 2)

        # If the field just became empty, restore immediately without waiting
        if not text:
            self._search_timer.stop()
            self._do_search()
            return

        self._search_timer.start()

    def _on_search_return_pressed(self) -> None:
        """User pressed Enter – skip the debounce wait and search immediately."""
        if self._search.text().strip():
            self._search_timer.stop()
            self._do_search()

    def _do_search(self) -> None:
        self._search_timer.stop()
        self._panel.reload(self._search.text().strip())


# ── Styling ────────────────────────────────────────────────────────────────────


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")

    BG_BASE = "#0a0a0a"
    BG_SURFACE = "#181818"
    BG_RAISED = "#1e1e1e"
    BG_BORDER = "rgba(255,255,255,0.07)"
    # Darkish-gray outer outline for the frameless window's #appShell border,
    # so the window edge reads as a distinct frame rather than blending into
    # the near-black app background (BG_BASE = #0a0a0a).
    OUTER_OUTLINE = "#5c5c5c"
    ACCENT = "#00d4a0"
    ACCENT_DIM = "rgba(0,212,160,0.18)"
    ACCENT_MID = "rgba(0,212,160,0.38)"
    TEXT_PRI = "#e8e8e8"
    TEXT_SEC = "rgba(200,200,200,0.55)"
    TEXT_DIM = "rgba(200,200,200,0.30)"

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(10, 10, 10))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(232, 232, 232))
    pal.setColor(QPalette.ColorRole.Base, QColor(7, 7, 7))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(17, 17, 17))
    pal.setColor(QPalette.ColorRole.Text, QColor(232, 232, 232))
    pal.setColor(QPalette.ColorRole.Button, QColor(24, 24, 24))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(232, 232, 232))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(0, 212, 160))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(24, 24, 24))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    app.setPalette(pal)

    app.setStyleSheet(f"""
        QMainWindow, QWidget {{
            background: {BG_BASE};
            color: {TEXT_PRI};
            font-size: 12px;
        }}

        /* Frameless top-level window itself must stay fully transparent --
           otherwise it paints an opaque square behind #appShell and that
           square peeks out from behind the shell's rounded corners. */
        #mainWindowFrameless {{
            background: transparent;
        }}

        /* ── app shell / custom title bar ────────────────────────────── */
        #appShell {{
            background: {BG_BASE};
            border: 1px solid {OUTER_OUTLINE};
            border-radius: 10px;
        }}
        #appShell[maximized="true"] {{
            border-radius: 0px;
            border: 1px solid {BG_BORDER};
        }}
        #appTitleBar {{
            background: {BG_BASE};
            border: none;
            border-bottom: 1px solid rgba(255,255,255,0.12);
            border-top-left-radius: 9px;
            border-top-right-radius: 9px;
        }}
        #appShell[maximized="true"] #appTitleBar {{
            border-top-left-radius: 0px;
            border-top-right-radius: 0px;
        }}
        #brandLbl {{
            background: transparent;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.2px;
        }}
        #winMinBtn, #winMaxBtn, #winCloseBtn {{
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.11);
            border-radius: 4px;
            color: rgba(200,200,200,0.65);
            font-size: 13px;
            font-weight: 500;
        }}
        #winMinBtn:hover, #winMaxBtn:hover {{
            background: rgba(255,255,255,0.09);
            border-color: rgba(255,255,255,0.22);
            color: rgba(230,230,230,0.95);
        }}
        #winMinBtn:pressed, #winMaxBtn:pressed {{
            background: rgba(255,255,255,0.04);
        }}
        #winCloseBtn:hover {{
            background: rgba(197,79,79,0.75);
            border-color: rgba(220,100,100,0.85);
            color: white;
        }}
        #winCloseBtn:pressed {{
            background: rgba(160,60,60,0.85);
        }}

        /* ── main canvas: a hint of depth instead of flat black ──────── */
        #appRoot {{
            background: {BG_BASE};
            border-bottom-left-radius: 9px;
            border-bottom-right-radius: 9px;
        }}
        #appShell[maximized="true"] #appRoot {{
            border-bottom-left-radius: 0px;
            border-bottom-right-radius: 0px;
        }}
        #resultsCanvas {{
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 #23292a, stop: 0.12 #181c1c,
                stop: 0.35 #101010, stop: 1 #060606
            );
        }}
        #resultsContent {{
            background: transparent;
        }}

        /* ── status bar ──────────────────────────────────────────────── */
        #statusBar {{
            background: {BG_RAISED};
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 5px;
        }}
        /* Labels inside the status bar must share its background */
        #statusBar QLabel {{
            background: transparent;
        }}
        #statusLbl {{ color: {TEXT_SEC}; font-size: 11px; }}
        #statusDot {{ color: {TEXT_DIM}; font-size: 11px; }}
        #dbLbl     {{ color: {ACCENT}; font-size: 11px; font-weight: 600; letter-spacing: 0.3px; }}
        #sortBtn {{
            background: transparent;
            border: 1px solid {BG_BORDER};
            border-radius: 3px;
            color: {TEXT_SEC};
            font-size: 9px;
            padding: 1px 5px;
            min-width: 0;
        }}
        #sortBtn:hover {{
            color: {TEXT_PRI};
            background: rgba(255,255,255,0.06);
            border-color: rgba(255,255,255,0.18);
        }}
        #sortBtn:pressed {{
            background: rgba(255,255,255,0.03);
        }}

        /* ── hamburger ───────────────────────────────────────────────── */
        #menuBtn {{
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.11);
            font-size: 18px; color: rgba(255,255,255,0.55);
            border-radius: 5px;
        }}
        #menuBtn:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; color: {ACCENT}; }}
        #menuBtn:pressed {{ background: rgba(255,255,255,0.03); }}

        /* ── notes button ────────────────────────────────────────────── */
        #noteBtn {{
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.11);
            border-radius: 6px;
            color: rgba(200,200,200,0.50);
            font-size: 14px;
            font-weight: 400;
        }}
        #noteBtn:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; color: {ACCENT}; }}
        #noteBtn:pressed {{ background: rgba(255,255,255,0.03); }}

        /* ── note window "+" button ──────────────────────────────────── */
        #noteAddBtn {{
            background: rgba(0,212,160,0.18);
            border: 1px solid rgba(0,212,160,0.35);
            border-radius: 4px;
            color: rgba(40,225,180,0.90);
            font-size: 14px;
            font-weight: 700;
            padding: 0px;
            text-align: center;
        }}
        #noteAddBtn:hover   {{ background: rgba(0,212,160,0.30); border-color: rgba(0,212,160,0.60); color: rgb(60,235,190); }}
        #noteAddBtn:pressed {{ background: rgba(0,212,160,0.10); }}

        /* ── note window background (match main app window) ─────────── */
        #noteWindowRoot {{
            background: {BG_BASE};
        }}
        #noteWindowRoot QScrollArea {{
            background: {BG_BASE};
        }}
        #noteWindowRoot QWidget {{
            background: {BG_BASE};
        }}
        /* Re-apply for the note search bar so the QWidget catch-all above doesn't fight it */
        #noteWindowRoot #noteSearch {{
            background: {BG_BASE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        #noteWindowRoot #noteSearch:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_BASE}; }}

        /* ── note entry card ─────────────────────────────────────────── */
        #noteEntry {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 7px;
        }}
        #noteEntryName {{
            font-size: 11px;
            font-weight: 700;
            color: {TEXT_PRI};
            background: transparent;
        }}
        #noteEntryValue {{
            font-size: 9px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── generic inputs (dialog fields, etc.) ────────────────────── */
        QLineEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        QLineEdit:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── main app search bar ─────────────────────────────────────── */
        #mainSearch {{
            background: {BG_SURFACE};
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        #mainSearch:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── note window search bar ───────────────────────────────────── */
        #noteSearch {{
            background: {BG_BASE};
            border: 1px solid {BG_BORDER};
            border-radius: 6px; padding: 2px 10px; font-size: 12px;
            color: {TEXT_PRI};
            selection-background-color: {ACCENT};
        }}
        #noteSearch:focus {{ border: 1px solid {ACCENT_MID}; background: {BG_BASE}; }}

        /* ── main app clear button ───────────────────────────────────── */
        #searchClearBtn {{
            background: transparent;
            border: 1px solid transparent;
            border-radius: 3px;
            color: rgba(200,200,200,0.65);
            font-size: 9px;
            padding: 1px 4px;
        }}
        #searchClearBtn:hover {{
            color: rgba(220,220,220,0.95);
            background: rgba(255,255,255,0.08);
            border-color: rgba(255,255,255,0.15);
        }}
        #searchClearBtn:pressed {{
            color: rgba(255,255,255,0.50);
            background: rgba(255,255,255,0.03);
        }}

        /* ── note window clear button ────────────────────────────────── */
        #noteSearchClearBtn {{
            background: transparent;
            border: 1px solid transparent;
            border-radius: 3px;
            color: rgba(0,212,160,0.55);
            font-size: 9px;
            padding: 1px 4px;
        }}
        #noteSearchClearBtn:hover {{
            color: {ACCENT};
            background: rgba(0,212,160,0.14);
            border-color: rgba(0,212,160,0.35);
        }}
        #noteSearchClearBtn:pressed {{
            color: rgba(0,212,160,0.70);
            background: rgba(0,212,160,0.06);
        }}

        /* ── folder section header ───────────────────────────────────── */
        #folderSection     {{ background: transparent; }}
        #sectionHeaderWrap {{ background: transparent; }}
        #sectionBody       {{ background: transparent; }}
        #cardGrid          {{ background: transparent; }}

        /* Idle: completely flat, no border/fill of any kind -- text only.
           Depth is conveyed purely by weight/color, not a box. A transparent
           left border is reserved here (not added on :hover) so the hover
           accent bar doesn't shift the text by changing the box width. */
        #sectionHeader {{
            background: transparent;
            border: 1px solid transparent;
            border-left: 2px solid transparent;
            border-radius: 6px;
            text-align: left;
            padding: 0px 10px 0px 7px;
            font-size: 11px;
            font-weight: 600;
            color: rgba(200,200,200,0.68);
            letter-spacing: 0.2px;
        }}
        #sectionHeader[depth0="true"] {{
            color: rgba(220,220,220,0.86);
            font-weight: 700;
        }}
        #sectionHeader[depth0="false"] {{
            color: rgba(195,195,195,0.55);
            font-weight: 500;
        }}

        /* Hover: subtle neutral tint with a thin accent bar on the left --
           no saturated full-width fill. */
        #sectionHeader:hover {{
            background: rgba(255,255,255,0.045);
            border: 1px solid transparent;
            border-left: 2px solid {ACCENT};
            color: rgba(235,235,235,0.95);
            font-weight: 700;
        }}

        /* ── folder "fully tagged" dot (Show Tagged mode) ─────────────── */
        #folderTagDot {{
            background: transparent;
            color: #E8A24B;
            font-size: 10px;
        }}

        /* ── folder copy button (!F-*.json) ─────────────────────────── */
        #folderCopyBtn {{
            background: transparent;
            border: 1px solid rgba(0,212,160,0.22);
            border-radius: 2px;
            color: rgba(0,212,160,0.55);
            font-size: 8px;
            font-weight: 600;
            letter-spacing: 0.3px;
            padding: 1px 6px 0px 6px;
        }}
        #folderCopyBtn:hover {{
            background: {ACCENT_DIM};
            border-color: {ACCENT_MID};
            color: {ACCENT};
        }}
        #folderCopyBtn:pressed {{
            background: rgba(0,212,160,0.08);
        }}

        /* ── thumbnail card ─────────────────────────────────────────── */
        #cardImage {{
            background: {BG_SURFACE};
            border-radius: 7px;
        }}
        #cardName {{
            font-size: 10px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── card context menu ───────────────────────────────────────── */
        QMenu#cardMenu {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 3px;
        }}
        QMenu#cardMenu::item {{
            padding: 4px 14px;
            font-size: 11px;
            border-radius: 3px;
            color: rgba(220,220,220,0.88);
        }}
        QMenu#cardMenu::item:selected {{
            background: {ACCENT_DIM};
            color: {ACCENT};
        }}
        QMenu#cardMenu::separator {{
            height: 1px;
            background: rgba(255,255,255,0.07);
            margin: 3px 4px;
        }}

        /* ── app menus ───────────────────────────────────────────────── */
        QMenu {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px;
            padding: 3px;
        }}
        QMenu::item          {{ padding: 4px 14px; font-size: 11px; border-radius: 3px; color: rgba(220,220,220,0.88); }}
        QMenu::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        QMenu::separator     {{ height: 1px; background: rgba(255,255,255,0.07); margin: 3px 4px; }}

        /* ── shadow layer behind dialog frame ───────────────────────── */
        #dialogShadow {{
            background: rgba(0,0,0,0.55);
            border-radius: 10px;
        }}

        /* ── shared dialog frame ─────────────────────────────────────── */
        #editDialogFrame {{
            background: {BG_BASE};
            border: 1px solid rgba(255,255,255,0.13);
            border-radius: 8px;
        }}

        /* ── dialog accent header ────────────────────────────────────── */
        #editDialogHeader {{
            background: {BG_RAISED};
            border-bottom: 1px solid rgba(255,255,255,0.07);
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
        }}
        #editDialogHeader QLabel, #editDialogHeader QWidget {{
            background: transparent;
        }}
        #editDialogDot {{
            background: {ACCENT};
            border-radius: 3px;
        }}
        #editDialogTitle {{
            font-size: 11px; font-weight: 600;
            color: {ACCENT}; letter-spacing: 0.4px;
            background: transparent;
        }}
        #dbDialogClose {{
            background: transparent; border: none;
            color: {TEXT_DIM}; font-size: 10px; border-radius: 3px;
        }}
        #dbDialogClose:hover {{ background: rgba(255,107,107,0.18); color: rgba(255,107,107,0.9); }}

        /* ── separator ───────────────────────────────────────────────── */
        #dbDialogSep {{
            background: rgba(255,255,255,0.07);
            max-height: 1px; border: none;
        }}

        /* ── database list ───────────────────────────────────────────── */
        #dbDialogList {{
            background: transparent; border: none;
        }}
        #dbDialogList::item {{
            padding: 5px 12px; border-radius: 3px;
            font-size: 11px; color: rgba(220,220,220,0.85);
        }}
        #dbDialogList::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
        #dbDialogList::item:hover    {{ background: rgba(255,255,255,0.025); }}

        /* ── dialog footer ───────────────────────────────────────────── */
        #editDialogFooter {{ background: transparent; }}

        /* Cancel - ghost */
        #editCancelBtn {{
            background: transparent;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 4px; padding: 3px 12px;
            font-size: 11px; color: {TEXT_DIM};
        }}
        #editCancelBtn:hover {{ background: rgba(255,255,255,0.04); color: {TEXT_SEC}; }}

        /* Remove DB - red, disabled when nothing selected */
        #dbRemoveBtn {{
            background: rgba(139,51,51,0.18);
            border: 1px solid rgba(197,79,79,0.35);
            border-radius: 4px; padding: 3px 12px;
            font-size: 11px; font-weight: 600;
            color: rgba(255,107,107,0.90);
        }}
        #dbRemoveBtn:hover  {{ background: rgba(197,79,79,0.30); border-color: rgba(197,79,79,0.60); color: rgb(255,107,107); }}
        #dbRemoveBtn:pressed {{ background: rgba(139,51,51,0.10); }}
        #dbRemoveBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.18); }}

        /* Save / Open - accent */
        #editSaveBtn {{
            background: {ACCENT_DIM};
            border: 1px solid {ACCENT_MID};
            border-radius: 4px; padding: 3px 16px;
            font-size: 11px; font-weight: 600;
            color: {ACCENT};
        }}
        #editSaveBtn:hover  {{ background: rgba(0,212,160,0.26); }}
        #editSaveBtn:pressed {{ background: rgba(0,212,160,0.10); }}

        /* ── JSON editor ─────────────────────────────────────────────── */
        #editEditorWrap, #dbListWrap {{
            background: transparent;
        }}
        #jsonEditor {{
            background: {BG_SURFACE};
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 5px;
            color: {TEXT_PRI};
            font-family: "Consolas", "Courier New", monospace;
            font-size: 11px;
            selection-background-color: {ACCENT};
            padding: 4px;
        }}

        /* ── loading overlay ─────────────────────────────────────────── */
        #loadingOverlay {{
            background: rgba(10,10,10,0.88);
        }}
        #loadingDots {{
            font-size: 28px;
            color: {ACCENT};
        }}
        #loadingMsg {{
            font-size: 12px;
            color: {TEXT_SEC};
        }}

        /* ── startup scripts +/- buttons ────────────────────────────── */
        #scriptAddBtn {{
            background: rgba(0,212,160,0.18);
            border: 1px solid rgba(0,212,160,0.35);
            border-radius: 4px;
            color: rgba(40,225,180,0.90);
            font-size: 14px;
            font-weight: 600;
        }}
        #scriptAddBtn:hover  {{ background: rgba(0,212,160,0.30); border-color: rgba(0,212,160,0.60); color: rgb(60,235,190); }}
        #scriptAddBtn:pressed {{ background: rgba(0,212,160,0.10); }}

        #scriptRemoveBtn {{
            background: rgba(139,51,51,0.15);
            border: 1px solid rgba(180,80,80,0.28);
            border-radius: 4px;
            color: rgba(197,79,79,0.70);
            font-size: 14px;
            font-weight: 600;
        }}
        #scriptRemoveBtn:hover  {{ background: rgba(180,80,80,0.28); border-color: rgba(139,51,51,0.55); color: rgb(255,120,120); }}
        #scriptRemoveBtn:pressed {{ background: rgba(139,51,51,0.10); }}
        #scriptRemoveBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        #scriptEditBtn {{
            background: rgba(232,184,75,0.15);
            border: 1px solid rgba(232,184,75,0.28);
            border-radius: 4px;
            color: rgba(232,196,100,0.70);
            font-size: 13px;
            font-weight: 600;
        }}
        #scriptEditBtn:hover   {{ background: rgba(232,184,75,0.30); border-color: rgba(216,180,85,0.55); color: rgb(240,205,110); }}
        #scriptEditBtn:pressed {{ background: rgba(232,184,75,0.10); }}
        #scriptEditBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        #scriptOrderBtn {{
            background: rgba(0,212,160,0.10);
            border: 1px solid rgba(0,212,160,0.22);
            border-radius: 4px;
            color: rgba(0,212,160,0.55);
            font-size: 13px;
            font-weight: 600;
        }}
        #scriptOrderBtn:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; color: {ACCENT}; }}
        #scriptOrderBtn:pressed {{ background: rgba(0,212,160,0.08); }}
        #scriptOrderBtn:disabled {{ background: transparent; border-color: rgba(255,255,255,0.06); color: rgba(255,255,255,0.15); }}

        /* ── add script dialog notice ────────────────────────────────── */
        #scriptNotice {{
            font-size: 10px;
            color: rgba(200,200,200,0.45);
            background: transparent;
        }}
        #scriptArgsEdit {{
            background: {BG_SURFACE};
            border: 1px solid {BG_BORDER};
            border-radius: 5px;
            padding: 2px 8px;
            font-size: 11px;
            color: {TEXT_PRI};
        }}
        #scriptArgsEdit:focus {{ border-color: {ACCENT_MID}; background: {BG_RAISED}; }}

        /* ── remove-confirm dialog labels ────────────────────────────── */
        #confirmMainLbl {{
            font-size: 12px; font-weight: 600;
            color: {TEXT_PRI};
            background: transparent;
        }}
        #confirmInfoLbl {{
            font-size: 11px;
            color: {TEXT_SEC};
            background: transparent;
        }}

        /* ── generic fallback ────────────────────────────────────────── */
        QDialog {{ background: {BG_SURFACE}; }}
        QPushButton {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.09);
            border-radius: 5px; padding: 5px 12px;
            color: {TEXT_PRI};
        }}
        QPushButton:hover   {{ background: {ACCENT_DIM}; border-color: {ACCENT_MID}; }}
        QPushButton:pressed {{ background: rgba(255,255,255,0.03); }}

        QListWidget {{
            background: transparent;
            border: 1px solid {BG_BORDER};
            border-radius: 6px;
        }}
        QListWidget::item          {{ padding: 5px 10px; border-radius: 3px; }}
        QListWidget::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}

        /* ── scrollbar ───────────────────────────────────────────────── */
        QScrollBar:vertical         {{ background: transparent; width: 4px; margin: 0; }}
        QScrollBar::handle:vertical {{
            background: rgba(255,255,255,0.12); border-radius: 2px; min-height: 24px;
        }}
        QScrollBar::handle:vertical:hover {{ background: rgba(0,212,160,0.40); }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical     {{ height: 0; }}

        /* ── image viewer overlay ────────────────────────────────────── */
        #viewerCloseBtn {{
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(255,255,255,0.10);
            color: rgba(220,220,220,0.50);
            font-size: 15px;
            font-weight: 400;
            border-radius: 16px;
        }}
        #viewerCloseBtn:hover {{
            background: rgba(139,51,51,0.28);
            border-color: rgba(197,79,79,0.50);
            color: rgba(255,107,107,0.95);
        }}
        #viewerCloseBtn:pressed {{
            background: rgba(139,51,51,0.18);
        }}

        #viewerName {{
            color: rgba(210,210,210,0.70);
            font-size: 12px;
            background: transparent;
        }}

        #viewerNavBtn {{
            background: rgba(18,18,18,0.72);
            border: 1px solid rgba(255,255,255,0.14);
            border-radius: 8px;
            color: rgba(220,220,220,0.82);
            font-size: 30px;
            font-weight: 300;
        }}
        #viewerNavBtn:hover {{
            background: rgba(30,30,30,0.90);
            border-color: rgba(0,212,160,0.55);
            color: rgba(80,240,195,1.0);
        }}
        #viewerNavBtn:pressed {{
            background: rgba(22,22,22,0.85);
            color: rgba(20,220,170,0.90);
        }}
        #viewerNavBtn:disabled {{
            background: rgba(10,10,10,0.30);
            border-color: rgba(255,255,255,0.05);
            color: rgba(255,255,255,0.12);
        }}

        #viewerImage {{
            background: transparent;
        }}
    """)


# ── Entry point ────────────────────────────────────────────────────────────────


def _migrate_legacy_app_dir() -> None:
    """
    One-time migration for people upgrading from "Asset Indexer": the local
    data folder was renamed from ~/.asset_indexer to ~/.vael_indexer as part
    of the vael. rebrand. Nothing inside the folder changes -- databases,
    prefs.json, notes.json, startup_scripts.json, roots.json, and the file
    caches all move across as-is.

    Safe to call on every launch:
      • If the new folder already exists, this is a no-op (already migrated,
        or a fresh install that never had the old folder).
      • If the old folder doesn't exist, this is a no-op (fresh install).
      • Only when the new folder is absent AND the old folder is present do
        we rename old -> new. A rename (not copy+delete) is used so this is
        a single atomic filesystem operation with no risk of partial data.
    """
    try:
        if APP_DIR.exists() or not _LEGACY_APP_DIR.exists():
            return
        _LEGACY_APP_DIR.rename(APP_DIR)
    except Exception:
        pass  # never block startup on a failed migration


def main() -> int:
    # Must run before anything else touches APP_DIR (e.g. DatabaseManager,
    # which creates APP_DIR on construction -- if that happened first, the
    # migration check above would see APP_DIR already existing and skip).
    _migrate_legacy_app_dir()

    # ── Windows: tell the taskbar to group under our own AppUserModelID ──────
    # This must happen BEFORE QApplication is created.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_ORG}.{APP_NAME}"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    if ICON_PATH.exists():
        icon = QIcon(str(ICON_PATH))
        app.setWindowIcon(icon)

    apply_style(app)
    db_manager = DatabaseManager()
    win = MainWindow(db_manager)
    win.show()
    win.run_startup_scripts()
    code = app.exec()
    return code


if __name__ == "__main__":
    raise SystemExit(main())