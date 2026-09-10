"""
vael. cover (PySide6 edition) — single-file build
================================================================
Run:  python app.py
Deps: pip install -r requirements.txt   (PySide6, requests)

This file merges main.py, api.py, config.py, workflow_utils.py, and
style.qss (embedded as STYLE_QSS) into one script. No other files are
required to run the app; workflows_config.json and the outputs/ folder
are still created next to this script at runtime.
"""
import os
import sys
import copy
import time
import uuid
import json
import queue
import datetime
import requests
from pathlib import Path

from PySide6.QtCore import (
    Qt, QObject, QThread, Signal, QMimeData, QUrl, QSize, QRunnable, QThreadPool,
    QAbstractListModel, QModelIndex, QRect, QTimer, QPoint, QEvent,
)
from PySide6.QtGui import (
    QPixmap, QImage, QDrag, QDesktopServices, QShortcut, QKeySequence, QIcon, QAction,
    QColor, QPen, QPainter, QPainterPath,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QTabBar, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QScrollArea, QFrame, QListWidget, QListWidgetItem, QListView, QStyledItemDelegate,
    QStyle, QProgressBar, QToolButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog, QSpinBox, QDoubleSpinBox,
    QSizePolicy, QStackedWidget, QMenu, QCheckBox,
    QGraphicsDropShadowEffect, QGraphicsBlurEffect, QGraphicsScene, QGraphicsPixmapItem,
)

# ===========================================================================
# config.py — config persistence
# ===========================================================================
CONFIG_FILE = Path(__file__).resolve().with_name("workflows_config.json")
ICON_FILE = Path(__file__).resolve().with_name(
    "icon.ico" if sys.platform == "win32" else "icon.png"
)
DEFAULT_SERVER = "http://127.0.0.1:8188"
DEFAULT_OUTPUT_DIR = str(Path(__file__).resolve().with_name("outputs"))

# -- Image Selection soft limits (spec section 9, resolved) -----------------
# Purely advisory: crossing these never blocks anything, it just surfaces a
# warning so the user knows before things get unwieldy. See SettingsDialog
# (folder count) and FolderSection (recursion depth).
FOLDER_COUNT_WARN = 20
RECURSION_DEPTH_WARN = 5

# Sidebar "always-on" edge rail width (workflow sidebar left, outputs/queue
# sidebar right) -- kept identical on both sides so their open/close tabs
# match exactly, per the shared vael. sidebar design.
EDGE_TAB_WIDTH = 14

DEFAULTS = {
    "server": DEFAULT_SERVER,
    "output_dir": DEFAULT_OUTPUT_DIR,
    "tabs": [],
    "window_geometry": None,
    "sidebar_width": 340,
    "workflow_sidebar_width": 260,
    # -- Image Selection (spec sections 4 & 5) --------------------------
    "image_selection_folders": [],       # [{"path": str}, ...] -- always recursive
    # Folder-name ignore rules: folders whose name matches any of these are
    # skipped during scanning entirely (not shown, not recursed into).
    # [{"pattern": str, "mode": "starts_with" | "contains"}, ...]
    "ignore_folder_patterns": [],
    # Height (in px) of the bottom Input Roster pane within the center
    # splitter; the Image Browser gets the rest. None until the user drags
    # the handle for the first time, at which point it's remembered.
    "center_split_roster_height": None,
}


# Set by load_config()/save_config() instead of failing silently -- see
# MainWindow.__init__ (load) and MainWindow.persist_all (save) for where
# these get surfaced to the user.
_LAST_LOAD_WARNING = None
_LAST_SAVE_ERROR = None


def load_config():
    """Load workflows_config.json, merged with DEFAULTS for any missing
    keys. If the file exists but can't be parsed -- e.g. it was left
    truncated by a crash, or partially written by another process/sync
    tool -- the broken file is renamed aside as a timestamped backup
    instead of being silently discarded, and _LAST_LOAD_WARNING is set so
    the caller can tell the user what happened instead of quietly handing
    them back an empty, default configuration."""
    global _LAST_LOAD_WARNING
    _LAST_LOAD_WARNING = None
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config file does not contain a JSON object")
            merged = dict(DEFAULTS)
            merged.update(data)
            return merged
        except Exception as e:
            backup_path = CONFIG_FILE.with_name(
                CONFIG_FILE.stem + f".corrupted-{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}" + CONFIG_FILE.suffix
            )
            try:
                CONFIG_FILE.replace(backup_path)
            except Exception:
                backup_path = None
            _LAST_LOAD_WARNING = (
                "workflows_config.json couldn't be read (" + str(e) + ") and "
                "vael. cover has started fresh with default settings.\n\n"
                + (
                    "The unreadable file was kept, renamed to:\n" + str(backup_path)
                    if backup_path is not None else
                    "The unreadable file could not be renamed aside and was left in place; "
                    "it will be overwritten the next time settings are saved."
                )
            )
    return dict(DEFAULTS)


def save_config(config):
    """Write workflows_config.json atomically: the new content is written
    to a temp file next to it and then swapped into place with os.replace,
    so a crash, forced quit, or a sync tool touching the file mid-write can
    never leave a half-written/corrupted config behind (which load_config
    would otherwise have to discard on the next launch).

    Returns True on success. On failure the exception is recorded in
    _LAST_SAVE_ERROR instead of being silently swallowed, so callers (see
    MainWindow.persist_all) can actually tell the user their changes
    aren't being saved, rather than failing invisibly forever."""
    global _LAST_SAVE_ERROR
    tmp_path = CONFIG_FILE.with_name(CONFIG_FILE.name + f".tmp-{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.replace(tmp_path, CONFIG_FILE)
        _LAST_SAVE_ERROR = None
        return True
    except Exception as e:
        _LAST_SAVE_ERROR = str(e)
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return False

# ===========================================================================
# api.py — ComfyUI HTTP API client
# ===========================================================================
class ComfyAPI:
    def __init__(self, server):
        self.server = server.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def upload_image(self, filepath, dest_filename=None):
        # IMPORTANT: never fall back to the local file's own basename here.
        # We upload with overwrite=true (so re-running a workflow doesn't
        # pile up stale files on the server), and ComfyUI's /upload/image
        # honors that literally: if two *different* local files happen to
        # share a filename (e.g. "base_1.png" from two different source
        # folders -- very common with generic/incrementing generator
        # output names), the second upload silently overwrites the first
        # on disk. Since all of a run's uploads happen before the prompt
        # is queued, every LoadImage node that ended up pointing at that
        # shared name then reads whichever file was uploaded *last* --
        # not the distinct image the user actually assigned to that slot.
        # A per-upload UUID name sidesteps the collision entirely,
        # regardless of what the source files are called.
        if not dest_filename:
            ext = os.path.splitext(filepath)[1]
            dest_filename = f"{uuid.uuid4().hex}{ext}"
        with open(filepath, "rb") as f:
            files = {"image": (dest_filename, f)}
            data = {"overwrite": "true"}
            r = requests.post(f"{self.server}/upload/image", files=files, data=data, timeout=60)
        r.raise_for_status()
        result = r.json()
        return result.get("name"), result.get("subfolder", ""), result.get("type", "input")

    def queue_prompt(self, workflow):
        payload = {"prompt": workflow, "client_id": self.client_id}
        r = requests.post(f"{self.server}/prompt", json=payload, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"ComfyUI rejected the prompt ({r.status_code}): {r.text}")
        return r.json()["prompt_id"]

    def get_history(self, prompt_id):
        r = requests.get(f"{self.server}/history/{prompt_id}", timeout=30)
        r.raise_for_status()
        return r.json()

    def get_image_bytes(self, filename, subfolder, folder_type):
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        r = requests.get(f"{self.server}/view", params=params, timeout=120)
        r.raise_for_status()
        return r.content

# ===========================================================================
# workflow_utils.py — ComfyUI API-format workflow JSON helpers
# ===========================================================================
IMAGE_LOADER_CLASSES = {"LoadImage", "LoadImageMask"}
OUTPUT_CLASSES = {"SaveImage", "PreviewImage"}


def load_workflow_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("This does not look like an API-format ComfyUI workflow.")
    for node in data.values():
        if not isinstance(node, dict) or "class_type" not in node:
            raise ValueError(
                "This JSON doesn't look like an API-format workflow.\n"
                "In ComfyUI use 'Save (API Format)', not the regular 'Save'."
            )
    return data


def find_load_image_nodes(workflow):
    nodes = [nid for nid, n in workflow.items() if n.get("class_type") in IMAGE_LOADER_CLASSES]
    nodes.sort(key=lambda x: int(x) if str(x).isdigit() else str(x))
    return nodes


def find_node(workflow, identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    if identifier in workflow:
        return identifier
    low = identifier.lower()
    for nid, node in workflow.items():
        title = node.get("_meta", {}).get("title", "")
        if title.strip().lower() == low:
            return nid
    return None


def get_editable_inputs(node):
    editable = {}
    for key, value in node.get("inputs", {}).items():
        if isinstance(value, list):
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            editable[key] = (value, "int")
        elif isinstance(value, float):
            editable[key] = (value, "float")
        elif isinstance(value, str):
            editable[key] = (value, "string")
    return editable


def find_output_node_ids(workflow):
    ids = [nid for nid, n in workflow.items() if n.get("class_type") in OUTPUT_CLASSES]
    ids.sort(key=lambda x: int(x) if str(x).isdigit() else str(x))
    return ids


def node_label(workflow, node_id):
    node = workflow.get(node_id, {})
    title = node.get("_meta", {}).get("title")
    cls = node.get("class_type", "?")
    return f"{title} (#{node_id})" if title else f"{cls} (#{node_id})"

# ===========================================================================
# style.qss — embedded stylesheet (was a separate file)
# ===========================================================================
STYLE_QSS = """
/* ============================================================
   vael. cover — shares vael.'s design language (near-black
   surfaces, teal accent, frameless window chrome) with the
   rest of the vael. product line (see vael. indexer).
   ============================================================ */

/* ---- Global ---- */
QWidget {
    background-color: #0a0a0a;
    color: #e8e8e8;
    font-family: "Segoe UI", sans-serif;
    font-size: 12px;
}

QMainWindow {
    background-color: #0a0a0a;
}

/* Frameless top-level window must stay fully transparent so only the
   rounded #appShell surface underneath it is ever visible. */
#mainWindowFrameless {
    background: transparent;
}

/* ---- App shell / custom title bar ---- */
#appShell {
    background: #0a0a0a;
    border: 1px solid #5c5c5c;
    border-radius: 10px;
}
#appShell[maximized="true"] {
    border-radius: 0px;
    border: 1px solid rgba(255,255,255,0.14);
}
#appTitleBar {
    background: #0a0a0a;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.16);
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
}
#appShell[maximized="true"] #appTitleBar {
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
}
#brandLbl {
    background: transparent;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.2px;
}
#winMinBtn, #winMaxBtn, #winCloseBtn {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.11);
    border-radius: 4px;
    color: rgba(200,200,200,0.65);
    font-size: 13px;
    font-weight: 500;
}
#winMinBtn:hover, #winMaxBtn:hover {
    background: rgba(255,255,255,0.09);
    border-color: rgba(255,255,255,0.22);
    color: rgba(230,230,230,0.95);
}
#winMinBtn:pressed, #winMaxBtn:pressed {
    background: rgba(255,255,255,0.04);
}
#winCloseBtn:hover {
    background: rgba(197,79,79,0.75);
    border-color: rgba(220,100,100,0.85);
    color: white;
}
#winCloseBtn:pressed {
    background: rgba(160,60,60,0.85);
}

#appRoot {
    background: #0a0a0a;
    border-bottom-left-radius: 9px;
    border-bottom-right-radius: 9px;
}
#appShell[maximized="true"] #appRoot {
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
}

/* ---- Left sidebar (Workflows) ---- */
#workflowSidebar {
    background-color: #121212;
    border: 1px solid rgba(255,255,255,0.18);
    border-left: none;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}
#edgeTabChevron {
    color: rgba(220,220,220,0.85);
    font-weight: 700;
    font-size: 11px;
}
/* Small pill-shaped open/close handle -- this widget's footprint IS the
   handle (see _WorkflowEdgeTab / _OutputsEdgeTab), so nothing outside it
   is clickable or painted. */
#edgeTabPill {
    background-color: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.30);
}
#edgeTabPill:hover {
    background-color: rgba(0,212,160,0.30);
    border-color: rgba(0,212,160,0.75);
}
#edgeTabPill:hover #edgeTabChevron {
    color: #ffffff;
}
QListWidget#workflowList::item {
    padding: 8px 8px;
    border-radius: 6px;
    margin-bottom: 2px;
}
QListWidget#workflowList::item:selected {
    background-color: rgba(0,212,160,0.16);
    color: #e8e8e8;
    border-left: 2px solid #00d4a0;
}

/* ---- Buttons ---- */
QPushButton {
    background-color: rgba(255,255,255,0.03);
    color: #e8e8e8;
    border: 1px solid rgba(255,255,255,0.11);
    border-radius: 5px;
    padding: 5px 12px;
}
QPushButton:hover {
    background-color: rgba(0,212,160,0.18);
    border-color: rgba(0,212,160,0.38);
    color: #ffffff;
}
QPushButton:pressed {
    background-color: rgba(255,255,255,0.03);
}
QPushButton:disabled {
    color: rgba(200,200,200,0.25);
    border-color: rgba(255,255,255,0.06);
    background-color: transparent;
}

QPushButton#accentButton {
    background-color: rgba(0,212,160,0.18);
    border: 1px solid rgba(0,212,160,0.38);
    color: rgb(60,235,190);
    font-weight: 700;
}
QPushButton#accentButton:hover {
    background-color: rgba(0,212,160,0.30);
    border-color: rgba(0,212,160,0.65);
    color: #ffffff;
}
QPushButton#accentButton:pressed {
    background-color: rgba(0,212,160,0.12);
}
QPushButton#accentButton:disabled {
    background-color: transparent;
    border-color: rgba(255,255,255,0.06);
    color: rgba(200,200,200,0.25);
}

QPushButton#dangerButton {
    background-color: transparent;
    border-color: rgba(197,79,79,0.35);
    color: rgba(220,140,140,0.85);
}
QPushButton#dangerButton:hover {
    background-color: rgba(197,79,79,0.20);
    border-color: rgba(220,100,100,0.65);
    color: #ffffff;
}

QPushButton#modeToggle {
    background-color: transparent;
    border: 1px solid rgba(255,255,255,0.11);
    border-radius: 5px;
    padding: 4px 12px;
    color: rgba(200,200,200,0.55);
}
QPushButton#modeToggle:hover {
    color: #e8e8e8;
}
QPushButton#modeToggle:checked {
    background-color: rgba(0,212,160,0.14);
    color: #00d4a0;
    border-color: rgba(0,212,160,0.45);
    font-weight: 600;
}

/* Small flat icon-only buttons (settings, sidebar toggle) */
QToolButton#iconButton {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.11);
    border-radius: 5px;
    color: rgba(200,200,200,0.65);
    font-size: 14px;
    padding: 3px 9px;
}
QToolButton#iconButton:hover {
    background: rgba(0,212,160,0.18);
    border-color: rgba(0,212,160,0.38);
    color: #00d4a0;
}
QToolButton#iconButton:checked {
    background: rgba(0,212,160,0.22);
    border-color: rgba(0,212,160,0.55);
    color: #00d4a0;
}

/* ---- Inputs ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {
    background-color: #181818;
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 6px;
    padding: 5px 9px;
    color: #e8e8e8;
    selection-background-color: #00d4a0;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: rgba(0,212,160,0.45);
    background-color: #1e1e1e;
}

/* ---- Roster icon (bottom Input Roster) ---- */
QFrame#rosterIcon {
    background-color: #141414;
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 7px;
}
QFrame#rosterIcon:hover {
    border-color: rgba(0,212,160,0.35);
}
QFrame#rosterIcon[dragOver="true"] {
    border: 1px dashed #00d4a0;
}
QFrame#rosterIcon[armed="true"] {
    border: 2px solid #00d4a0;
    background-color: rgba(0,212,160,0.08);
}
QLabel#rosterIconCanvas {
    background: transparent;
    color: rgba(200,200,200,0.35);
    font-size: 16px;
    font-weight: 300;
}
/* Status text along the bottom strip of free space in the roster's
   viewport (see RosterBar._position_status_label) -- plain single-line
   text, no button/pill treatment. */
QLabel#rosterStatusLabel {
    background: transparent;
    border: none;
    color: rgba(200,200,200,0.45);
    font-size: 11px;
}
QLabel#rosterStatusLabel[error="true"] {
    color: rgba(224,110,100,0.85);
}
/* The run indicator itself (see _RunIndicator) is entirely custom-painted,
   not stylesheet-driven -- no QSS rule needed here. */

/* ---- Center — Image Browser ---- */
#imageBrowserPanel {
    background-color: #0d0d0d;
}
QTabWidget#browserTabs::pane {
    /* No border here on purpose -- the tab strip itself (below) already
       has its own bordered pill box. A border-top on the pane used to
       draw a second, unrelated straight line spanning the *entire* width
       of the panel (running clean through/above the settings icon too),
       independent of the tab strip's own rounded box, which read as a
       stray bar sitting above everything. */
    border: none;
    top: -1px;
}
/* Slim bordered pill for the tab strip itself — mirrors the indexer
   app's clean status bar so this row takes up minimal vertical space. */
QTabWidget#browserTabs QTabBar {
    background: #141414;
    border: 1px solid rgba(255,255,255,0.16);
    border-radius: 5px;
    padding: 2px 3px;
    margin: 2px 2px 4px 2px;
}
QTabWidget#browserTabs QTabBar::tab {
    background: transparent;
    color: rgba(200,200,200,0.55);
    font-size: 11px;
    padding: 4px 10px;
    margin-right: 2px;
    border-radius: 3px;
    border-bottom: 2px solid transparent;
}
QTabWidget#browserTabs QTabBar::tab:selected {
    color: #e8e8e8;
    background: rgba(255,255,255,0.05);
    border-bottom: 2px solid #00d4a0;
}
QTabWidget#browserTabs QTabBar::tab:hover:!selected {
    color: rgba(220,220,220,0.85);
}
/* Status bar housing the Settings (and restore-hidden) button. This row
   is always present, even with zero folders configured, so Settings is
   never unreachable on a fresh/empty setup. */
QWidget#browserStatusBar {
    background: transparent;
}
QToolButton#browserSettingsBtn {
    background: transparent;
    border: none;
    color: rgba(200,200,200,0.45);
    font-size: 14px;
    padding: 1px 2px;
    margin: -2px 0px 0px 0px;
}
QToolButton#browserSettingsBtn:hover {
    color: rgba(220,220,220,0.85);
}
QToolButton#browserSettingsBtn:pressed {
    color: #e8e8e8;
}

/* ---- Bulk "Load Selected Folders" overlay ----
   No panel box on purpose -- the blurred/dimmed backdrop itself (painted
   in _BulkLoadOverlay.paintEvent) already carries the "held up" meaning,
   so only the bare text/progress/button float in the middle. */
#bulkLoadPanel {
    background: transparent;
    border: none;
}
QProgressBar#bulkLoadProgress {
    background-color: rgba(255,255,255,0.10);
    border: none;
    border-radius: 3px;
}
QProgressBar#bulkLoadProgress::chunk {
    background-color: #00d4a0;
    border-radius: 3px;
}
QLabel#bulkLoadCount {
    color: #00d4a0;
    font-size: 15px;
    font-weight: 700;
}
QLabel#bulkLoadDetail {
    color: rgba(230,230,230,0.85);
    font-size: 12px;
}
/* ---- Queue count badge (red circle/pill, white digits) ---- */
QLabel#queueCountBadgeLabel {
    background: transparent;
    color: #ffffff;
    font-size: 10px;
    font-weight: 700;
}
/* ---- Folder section header (ported from vael. indexer's #sectionHeader:
   flat, text-only, no box at rest; depth conveyed by weight/color, not by
   a bordered frame; hover adds a thin left accent bar). ---- */
#folderSection     { background: transparent; }
#sectionHeaderWrap { background: transparent; }
#sectionBody       { background: transparent; }
#cardGrid           { background: transparent; }

QToolButton#sectionHeader {
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
}
QToolButton#sectionHeader[depth0="true"] {
    color: rgba(220,220,220,0.86);
    font-weight: 700;
    font-size: 12px;
}
QToolButton#sectionHeader[depth0="false"] {
    color: rgba(195,195,195,0.55);
    font-weight: 500;
}
QToolButton#sectionHeader:hover {
    background: rgba(255,255,255,0.045);
    border: 1px solid transparent;
    border-left: 2px solid #00d4a0;
    color: rgba(235,235,235,0.95);
    font-weight: 700;
}
/* Past the recommended recursion-depth guideline (soft warning only, spec
   section 9) — amber instead of teal, still fully functional. */
QToolButton#sectionHeader[deep="true"] {
    color: rgba(226,163,55,0.75);
}
QToolButton#sectionHeader[deep="true"]:hover {
    border-left: 2px solid rgba(226,163,55,0.65);
    background: rgba(226,163,55,0.05);
}
/* Multi-select (ctrl+click or rubber-band drag) — distinct from :hover so
   a selected-but-not-hovered folder still reads clearly as selected. */
QToolButton#sectionHeader[selected="true"] {
    background: rgba(0,212,160,0.16);
    border: 1px solid rgba(0,212,160,0.4);
    border-left: 2px solid #00d4a0;
    color: #00d4a0;
    font-weight: 700;
}
QToolButton#sectionHeader[selected="true"]:hover {
    background: rgba(0,212,160,0.22);
}
QLabel#headerCount {
    color: rgba(200,200,200,0.35);
    font-size: 10px;
}
QLabel#headerCount[state="error"] {
    color: rgba(224,110,100,0.85);
}

/* ---- Thumbnail card (ported from vael. indexer: image only, no
   filename caption, hover = a faint neutral border, nothing more). ---- */
#cardImage {
    background-color: #181818;
    border-radius: 7px;
}

/* ---- Bottom bar — Input Roster ----
   The actual depth cue is a real cast shadow (QGraphicsDropShadowEffect,
   see RosterBar.__init__) bleeding up from this widget onto the browser
   above it. Here we just add a crisp, slightly brighter top edge -- a
   thin "lip" highlight where the raised slab would catch light -- rather
   than stacking another gradient on top of the shadow. */
#rosterBar {
    background-color: #101010;
    border-top: 1px solid rgba(255,255,255,0.28);
}

/* ---- Splitter handle (Image Browser / Input Roster) ----
   No painted background here on purpose -- _CenterSplitterHandle draws
   its own small three-line "grip" directly in paintEvent, so the handle
   reads as a thin gap with a subtle drag affordance in the middle rather
   than a thick colored bar. */
QSplitter#centerSplitter::handle {
    background-color: transparent;
}

/* ---- Sidebar (Outputs / Queue) ---- */
#outputsSidebar {
    background-color: #121212;
    border: 1px solid rgba(255,255,255,0.18);
    border-right: none;
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}
#sidebarHandle {
    background-color: transparent;
}
#sidebarHandle:hover {
    background-color: rgba(0,212,160,0.45);
}

/* ---- Outputs sidebar footer action row (Refresh/Open Folder/Clear,
   Run Queue/Clear) -- kept separate from the Outputs/Queue mode toggle at
   the top so a narrow sidebar never has to squeeze 4-5 buttons into one
   row and truncate their labels. ---- */
#outputsFooter {
    background: transparent;
    border-top: 1px solid rgba(255,255,255,0.12);
}
#outputsFooter QPushButton {
    padding: 5px 8px;
}

/* ---- Lists (queue / outputs) ---- */
QListWidget {
    background-color: transparent;
    border: none;
    padding: 0;
}
QListWidget::item {
    padding: 6px;
    border-radius: 5px;
}
QListWidget::item:selected {
    background-color: rgba(0,212,160,0.16);
    color: #e8e8e8;
}
QListWidget::item:hover:!selected {
    background-color: rgba(255,255,255,0.045);
}

QScrollBar:vertical, QScrollBar:horizontal {
    background: transparent;
    width: 8px;
    height: 8px;
    margin: 0;
}
QScrollBar::handle {
    background: rgba(255,255,255,0.14);
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::handle:hover {
    background: #00d4a0;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}

QLabel#sectionTitle {
    font-size: 13px;
    font-weight: 700;
    color: #e8e8e8;
    padding: 2px 0 6px 0;
}

QLabel#hint {
    color: rgba(200,200,200,0.45);
    font-size: 11px;
}
QLabel#hint[state="warning"] {
    color: rgba(226,163,55,0.90);
}
QLabel#hint[state="error"] {
    color: rgba(224,110,100,0.85);
}
QLabel#hint[state="muted"] {
    color: rgba(200,200,200,0.30);
}

QToolTip {
    background-color: #1e1e1e;
    color: #e8e8e8;
    border: 1px solid rgba(255,255,255,0.11);
    padding: 4px;
}

QTableWidget {
    background-color: transparent;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    gridline-color: rgba(255,255,255,0.10);
}
QHeaderView::section {
    background-color: transparent;
    color: rgba(200,200,200,0.55);
    padding: 6px;
    border: none;
    border-bottom: 1px solid rgba(255,255,255,0.14);
    font-weight: 600;
}

QProgressBar {
    background-color: #181818;
    border: none;
    border-radius: 3px;
    text-align: center;
    color: #e8e8e8;
    max-height: 3px;
}
QProgressBar::chunk {
    background-color: #00d4a0;
    border-radius: 3px;
}

/* ---- Image viewer overlay (ported from vael. indexer) ---- */
#viewerCloseBtn {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.10);
    color: rgba(220,220,220,0.50);
    font-size: 15px;
    font-weight: 400;
    border-radius: 16px;
}
#viewerCloseBtn:hover {
    background: rgba(139,51,51,0.28);
    border-color: rgba(197,79,79,0.50);
    color: rgba(255,107,107,0.95);
}
#viewerCloseBtn:pressed {
    background: rgba(139,51,51,0.18);
}
#viewerName {
    color: rgba(210,210,210,0.70);
    font-size: 12px;
    background: transparent;
}
#viewerNavBtn {
    background: rgba(18,18,18,0.72);
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 8px;
    color: rgba(220,220,220,0.82);
    font-size: 30px;
    font-weight: 300;
}
#viewerNavBtn:hover {
    background: rgba(30,30,30,0.90);
    border-color: rgba(0,212,160,0.55);
    color: rgba(80,240,195,1.0);
}
#viewerNavBtn:pressed {
    background: rgba(22,22,22,0.85);
    color: rgba(20,220,170,0.90);
}
#viewerNavBtn:disabled {
    background: rgba(10,10,10,0.30);
    border-color: rgba(255,255,255,0.05);
    color: rgba(255,255,255,0.12);
}
#viewerImage {
    background: transparent;
}

/* ---- Context / app menus (ported from vael. indexer) ----
   Kept generic (plain QMenu selector) so it applies to every popup menu
   in the app — existing ones and any added later — without needing an
   objectName on each. ---- */
QMenu {
    background-color: #0a0a0a;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 5px;
    padding: 3px;
}
QMenu::item {
    padding: 4px 14px;
    font-size: 11px;
    border-radius: 3px;
    color: rgba(220,220,220,0.88);
}
QMenu::item:selected {
    background: rgba(0,212,160,0.16);
    color: #00d4a0;
}
QMenu::item:disabled {
    color: rgba(200,200,200,0.30);
}
QMenu::separator {
    height: 1px;
    background: rgba(255,255,255,0.10);
    margin: 3px 4px;
}
"""

# ===========================================================================
# main.py — application UI and logic
# ===========================================================================
import os
import sys
import copy
import time
import uuid
import math
import queue
import threading
import datetime
from pathlib import Path

from PySide6.QtCore import (
    Qt, QObject, QThread, Signal, QMimeData, QUrl, QSize, QEvent, QPoint, QPointF, QRect,
    QRectF, QPropertyAnimation, QEasingCurve, QRunnable, QThreadPool, QTimer,
)
from PySide6.QtGui import (
    QPixmap, QImage, QDrag, QDesktopServices, QShortcut, QKeySequence, QIcon, QAction,
    QPainter, QPen, QColor, QPalette, QGuiApplication,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QTabBar, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QSplitterHandle,
    QScrollArea, QFrame, QListWidget, QListWidgetItem, QProgressBar, QToolButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QDialog, QSpinBox, QDoubleSpinBox,
    QSizePolicy, QStackedWidget, QMenu, QSizeGrip, QCheckBox, QGridLayout, QComboBox,
)


APP_TITLE = "cover"
APP_BRAND_PREFIX = "vael. "
APP_BRAND_SUFFIX = "cover"
POLL_INTERVAL = 1.0
POLL_TIMEOUT = 600

HOTKEYS = [
    ("Ctrl+R", "Run the active workflow"),
    ("Ctrl+Shift+A", "Add current workflow (with current inputs) to the run queue"),
    ("Ctrl+Shift+R", "Run every queued workflow, one after another"),
    ("Ctrl+Shift+X", "Clear the run queue"),
    ("Ctrl+N", "Create a new workflow"),
    ("Ctrl+Tab", "Next workflow"),
    ("Ctrl+Shift+Tab", "Previous workflow"),
    ("Ctrl+O", "Toggle the Outputs / Queue sidebar"),
    ("Ctrl+Shift+W", "Toggle the Workflows sidebar"),
    ("Ctrl+,", "Open Settings"),
    ("Ctrl+Shift+O", "Open the outputs folder on disk"),
    ("F5", "Refresh the outputs list"),
]


# ---------------------------------------------------------------------------
# Workflow execution (runs on a background thread)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Workflow execution (runs on a background thread)
# ---------------------------------------------------------------------------
def execute_workflow_sync(server, raw_workflow, image_map, optional_node_id, param_values):
    api = ComfyAPI(server)
    wf = copy.deepcopy(raw_workflow)

    for node_id, path in image_map.items():
        if not path:
            raise RuntimeError(f"Missing image for node #{node_id}")
        name, subfolder, ftype = api.upload_image(path)
        wf[node_id]["inputs"]["image"] = name

    if optional_node_id and optional_node_id in wf:
        for key, val in (param_values or {}).items():
            if key in wf[optional_node_id].get("inputs", {}):
                wf[optional_node_id]["inputs"][key] = val

    prompt_id = api.queue_prompt(wf)
    start = time.time()
    while True:
        time.sleep(POLL_INTERVAL)
        if time.time() - start > POLL_TIMEOUT:
            raise TimeoutError("Timed out waiting for the workflow to finish.")
        hist = api.get_history(prompt_id)
        entry = hist.get(prompt_id)
        if not entry:
            continue
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            raise RuntimeError(f"ComfyUI reported an error: {status}")
        if status.get("completed"):
            # The app doesn't download or save the result itself -- the
            # workflow's own Save Image node is what persists the file (see
            # the note on MainWindow.output_dir). All that matters here is
            # confirming the run actually produced an image, so a failed
            # or misconfigured workflow doesn't silently report "Done".
            outputs = entry.get("outputs", {})
            has_image = any(node_out.get("images") for node_out in outputs.values())
            if not has_image:
                raise RuntimeError("Workflow finished but produced no image output.")
            return


class RunWorker(QObject):
    finished = Signal()
    error = Signal(str)

    def __init__(self, server, raw_workflow, image_map, optional_node_id, param_values):
        super().__init__()
        self.server = server
        self.raw_workflow = raw_workflow
        self.image_map = image_map
        self.optional_node_id = optional_node_id
        self.param_values = param_values

    def run(self):
        try:
            execute_workflow_sync(
                self.server, self.raw_workflow, self.image_map,
                self.optional_node_id, self.param_values,
            )
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Roster icon — one small square per image input on the *active* workflow,
# rendered left-to-right in the bottom "input roster" bar. This replaces the
# old big per-slot panel (ImageSlot): the panel content now belongs to the
# Image Browser (center), and this icon is just the compact roster target.
#
# Primary flow (spec 3.6): click an icon to "arm" it, then click a thumbnail
# in the Image Browser to assign it. The Image Browser isn't built yet
# (that's the bulk of section 3), so for now the icon also keeps the old
# manual fallback alive: double-click to browse via file dialog, or
# drag-and-drop a file from Explorer straight onto it. Dragging one icon
# onto another still reorders (grip) or swaps image content (canvas).
# ---------------------------------------------------------------------------
class RosterIcon(QFrame):
    reorderRequested = Signal(int, int)     # source_index, target_index (moves the whole slot)
    imageSwapRequested = Signal(int, int)   # source_index, target_index (swaps only image content)
    armToggled = Signal(int)                # this icon's index was clicked to arm/disarm
    changed = Signal()

    SIZE = 60
    # Once an image is loaded, the icon's *shape* follows the image's own
    # aspect ratio (mirrors how OS file explorers render thumbnails: a
    # square photo looks square, a portrait looks portrait, a landscape
    # looks landscape) instead of always being force-cropped into a fixed
    # square. Height stays pinned to the roster row's current height;
    # width is derived from the image and clamped to this range so an
    # extreme aspect ratio (e.g. a panorama) can't blow out the row, and
    # a very tall/narrow image doesn't collapse to nothing. The empty "+"
    # placeholder (no image assigned yet) always stays a plain square.
    MIN_WIDTH_RATIO = 0.4
    MAX_WIDTH_RATIO = 2.2

    def __init__(self, index, node_id, caption, parent=None, size=None):
        super().__init__(parent)
        self.setObjectName("rosterIcon")
        self.setProperty("armed", False)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.NoFrame)
        self.index = index
        self.node_id = node_id
        self.caption = caption
        self.filepath = None
        self._pixmap = None
        self._press_pos = None
        self._dragging = False
        # Instance-level size (defaults to the class constant) so the whole
        # roster row can grow/shrink together as the bottom bar is resized
        # (see RosterBar._sync_icon_size), instead of every icon staying
        # pinned at a fixed 60px.
        self._size = int(size) if size else self.SIZE
        self.setFixedSize(self._size, self._size)
        self.setCursor(Qt.PointingHandCursor)
        self._update_tooltip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = QLabel("+")
        self.canvas.setObjectName("rosterIconCanvas")
        self.canvas.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.canvas)

    def _update_tooltip(self):
        base = f"Input #{self.index + 1}: {self.caption}"
        hint = "\n\nClick to arm for the Image Browser.\nDouble-click or drag a file here to assign manually.\nDrag onto another icon to reorder or swap.\nRight-click to clear."
        self.setToolTip(base + hint)

    # -- drag & drop -----------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._dragging = False
        elif event.button() == Qt.RightButton:
            self.clear_image()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is not None and (event.buttons() & Qt.LeftButton) and not self._dragging:
            if (event.position().toPoint() - self._press_pos).manhattanLength() > QApplication.startDragDistance():
                self._dragging = True
                self._start_drag()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and not self._dragging:
            self.armToggled.emit(self.index)
        self._press_pos = None
        self._dragging = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.browse_image()
        super().mouseDoubleClickEvent(event)

    def _start_drag(self):
        mime = QMimeData()
        mime.setData("application/x-slot-reorder", str(self.index).encode())
        # Also register as an image-swap source when this icon already holds
        # an image, so dropping it onto another filled icon swaps content.
        if self.filepath:
            mime.setData("application/x-slot-image", str(self.index).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        if self._pixmap is not None:
            drag.setPixmap(self._pixmap.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        drag.exec(Qt.MoveAction)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasFormat("application/x-slot-reorder") or md.hasFormat("application/x-slot-image") or md.hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragOver", True)
            self.style().unpolish(self)
            self.style().polish(self)

    def dragLeaveEvent(self, event):
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, event):
        self.setProperty("dragOver", False)
        self.style().unpolish(self)
        self.style().polish(self)
        md = event.mimeData()
        if md.hasFormat("application/x-slot-image"):
            src = int(bytes(md.data("application/x-slot-image")).decode())
            if src != self.index:
                self.imageSwapRequested.emit(src, self.index)
            event.acceptProposedAction()
        elif md.hasFormat("application/x-slot-reorder"):
            src = int(bytes(md.data("application/x-slot-reorder")).decode())
            if src != self.index:
                self.reorderRequested.emit(src, self.index)
            event.acceptProposedAction()
        elif md.hasUrls():
            path = md.urls()[0].toLocalFile()
            if path:
                self.set_image_path(path)
            event.acceptProposedAction()

    # -- image handling ----------------------------------------------------
    def browse_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select input image (manual override)", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        )
        if path:
            self.set_image_path(path)

    def clear_image(self):
        self.filepath = None
        self._pixmap = None
        self._render()
        self.changed.emit()

    def set_image_path(self, path):
        pix = QPixmap(path)
        if pix.isNull():
            QMessageBox.warning(self, "Image error", f"Couldn't load image:\n{path}")
            return
        self.filepath = path
        self._pixmap = pix
        self._render()
        self.changed.emit()

    def set_armed(self, armed: bool):
        if self.property("armed") == armed:
            return
        self.setProperty("armed", armed)
        self.style().unpolish(self)
        self.style().polish(self)

    def _render(self):
        if self._pixmap is not None and not self._pixmap.isNull():
            height = self._size
            ratio = self._pixmap.width() / max(1, self._pixmap.height())
            ratio = max(self.MIN_WIDTH_RATIO, min(self.MAX_WIDTH_RATIO, ratio))
            width = max(24, round(height * ratio))
            self.setFixedSize(width, height)
            inner_w = max(1, width - 6)
            inner_h = max(1, height - 6)
            scaled = self._pixmap.scaled(inner_w, inner_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.canvas.setPixmap(scaled)
            self.canvas.setText("")
        else:
            # No image yet — stay a plain square, same as before.
            self.setFixedSize(self._size, self._size)
            self.canvas.setPixmap(QPixmap())
            self.canvas.setText("+")

    def set_index(self, new_index):
        self.index = new_index
        self._update_tooltip()

    def set_icon_size(self, size):
        """Resize this icon (and re-scale its thumbnail) to `size` px tall
        (width follows, see _render). Called by RosterBar when the bottom
        bar is resized."""
        size = int(size)
        if size == self._size:
            return
        self._size = size
        self._render()


# ---------------------------------------------------------------------------
# One workflow's data + run logic (no widget of its own any more). Under the
# revamped shell there's exactly one center Image Browser and one bottom
# roster shared by whichever workflow is active, so the old per-tab QWidget
# is replaced by a plain QObject the shared RosterBar renders against.
# ---------------------------------------------------------------------------
class WorkflowState(QObject):
    statusChanged = Signal(str, bool)     # text, is_error
    runStateChanged = Signal(bool)        # running
    slotsRebuilt = Signal()               # slot list replaced wholesale (e.g. on load)

    def __init__(self, main_window, data=None):
        super().__init__()
        self.main_window = main_window
        data = data or {}
        self.workflow_path = data.get("workflow_path")
        self.optional_identifier = data.get("optional_identifier", "")
        self.saved_slot_node_order = data.get("slot_node_order") or []
        self.saved_param_values = data.get("param_values") or {}
        self.name = Path(self.workflow_path).stem if self.workflow_path else "Workflow"

        self.raw_workflow = None
        self.slots = []            # list of {"node_id","caption","filepath"}, display order
        self.optional_node_id = None
        self.param_values = {}     # key -> current value for the optional node's editable inputs
        self.status_text = "Ready."
        self.status_error = False
        self.running = False
        self._thread = None
        self._worker = None

        if self.workflow_path and os.path.exists(self.workflow_path):
            try:
                self.load_workflow(self.workflow_path)
            except Exception as e:
                self._set_status(f"Couldn't load workflow: {e}", error=True)
        elif self.workflow_path:
            self._set_status("Workflow file not found - reconfigure this workflow.", error=True)

    # -- Workflow loading -------------------------------------------------
    def load_workflow(self, path):
        wf = load_workflow_file(path)
        self.raw_workflow = wf
        self.workflow_path = path
        self.name = Path(path).stem
        self.main_window.rename_workflow(self, self.name)

        image_nodes = find_load_image_nodes(wf)
        if self.saved_slot_node_order:
            ordered = [n for n in self.saved_slot_node_order if n in image_nodes]
            ordered += [n for n in image_nodes if n not in ordered]
            image_nodes = ordered

        self.slots = [
            {"node_id": nid, "caption": node_label(wf, nid), "filepath": None}
            for nid in image_nodes
        ]

        self.optional_node_id = find_node(wf, self.optional_identifier) if self.optional_identifier else None
        self.param_values = {}
        for key, (value, _type_name) in self.editable_params().items():
            # Restore whatever the user last set for this key (saved in the
            # config file), falling back to the workflow's own default the
            # first time a key is seen.
            self.param_values[key] = self.saved_param_values.get(key, value)

        self.slotsRebuilt.emit()
        self._set_status(f"Loaded. {len(self.slots)} image input(s) found.")

    def editable_params(self):
        if not self.optional_node_id or not self.raw_workflow:
            return {}
        return get_editable_inputs(self.raw_workflow.get(self.optional_node_id, {}))

    # -- Reordering / swapping (driven by the roster bar's icons) ---------
    def reorder_slot(self, src, tgt):
        if src == tgt or src >= len(self.slots) or tgt >= len(self.slots):
            return
        item = self.slots.pop(src)
        self.slots.insert(tgt, item)
        self.slotsRebuilt.emit()
        self.main_window.persist_all()

    def swap_slot_images(self, src, tgt):
        if src == tgt or src >= len(self.slots) or tgt >= len(self.slots):
            return
        self.slots[src]["filepath"], self.slots[tgt]["filepath"] = (
            self.slots[tgt]["filepath"], self.slots[src]["filepath"],
        )
        self.main_window.persist_all()

    def to_dict(self):
        return {
            "workflow_path": self.workflow_path,
            "optional_identifier": self.optional_identifier,
            "slot_node_order": [s["node_id"] for s in self.slots],
            "param_values": self.param_values,
        }

    # -- Running -------------------------------------------------------
    def _gather_image_map(self):
        return {s["node_id"]: s["filepath"] for s in self.slots}

    def _validate(self):
        if not self.raw_workflow:
            QMessageBox.warning(self.main_window, "No workflow", f"'{self.name}' has no valid workflow loaded.")
            return False
        missing = [s["caption"] for s in self.slots if not s["filepath"]]
        if missing:
            QMessageBox.warning(self.main_window, "Missing images", "Please fill in:\n- " + "\n- ".join(missing))
            return False
        return True

    def run_now(self):
        if not self._validate():
            return
        self.running = True
        self.runStateChanged.emit(True)
        self._set_status("Running...")
        self._thread = QThread()
        self._worker = RunWorker(
            self.main_window.server, copy.deepcopy(self.raw_workflow),
            self._gather_image_map(), self.optional_node_id, dict(self.param_values),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_run_finished)
        self._worker.error.connect(self._on_run_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_run_finished(self):
        self.running = False
        self.runStateChanged.emit(False)
        self._set_status("Done.")
        self.main_window.outputs_tab.refresh()

    def _on_run_error(self, message):
        self.running = False
        self.runStateChanged.emit(False)
        self._set_status(f"Error: {message}", error=True)
        QMessageBox.critical(self.main_window, "Run failed", message)

    def add_to_queue(self):
        if not self._validate():
            return
        self.main_window.queue_manager.add_item({
            "id": str(uuid.uuid4()),
            "tab_name": self.name,
            "server": self.main_window.server,
            "raw_workflow": copy.deepcopy(self.raw_workflow),
            "image_map": self._gather_image_map(),
            "optional_node_id": self.optional_node_id,
            "param_values": dict(self.param_values),
        })
        self._set_status("Added current inputs to the run queue.")

    def _set_status(self, text, error=False):
        self.status_text = text
        self.status_error = error
        self.statusChanged.emit(text, error)


# ---------------------------------------------------------------------------
# In-memory thumbnail cache + background loader — ported one-to-one from
# vael. indexer's image-handling approach: no disk thumb cache at all, just
# a process-lifetime dict of already-scaled QPixmaps, filled in by a single
# long-lived worker thread that drains a queue of (card, path) requests and
# delivers finished pixmaps back to the GUI thread via a signal.
# ---------------------------------------------------------------------------
IMAGE_FILE_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff",
)

import re as _re
_NATSORT_SPLIT_RE = _re.compile(r"(\d+)")


def _natural_sort_key(path_str):
    """Case-insensitive 'natural' sort key: splits the string on runs of
    digits and compares numeric runs by value rather than character-by-
    character, so 'img2.png' sorts before 'img10.png' the way a person
    (and Explorer/Finder) expects, instead of plain lexicographic order
    putting 'img10' before 'img2'. Applied to the full path, which is
    equivalent to sorting by filename since every entry in one scan
    shares the same parent directory prefix."""
    name = str(path_str).lower()
    return [int(part) if part.isdigit() else part for part in _NATSORT_SPLIT_RE.split(name)]

# 2:3 portrait thumbnail, identical crop-to-fill sizing to vael. indexer.
THUMB_W = 106
THUMB_H = 159

_PIXMAP_CACHE: dict[str, QPixmap] = {}


def _load_pixmap(path: str) -> QPixmap:
    """Return a cached thumbnail pixmap, or load+scale synchronously."""
    if path not in _PIXMAP_CACHE:
        pix = QPixmap(path)
        if not pix.isNull():
            scaled = pix.scaled(
                THUMB_W, THUMB_H,
                Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
            )
            x = (scaled.width() - THUMB_W) // 2
            y = (scaled.height() - THUMB_H) // 2
            pix = scaled.copy(x, y, THUMB_W, THUMB_H)
        _PIXMAP_CACHE[path] = pix
    return _PIXMAP_CACHE[path]


class PixmapWorker(QThread):
    """Loads and scales thumbnail pixmaps off the main thread, one shared
    long-lived instance for the whole app session (mirrors vael. indexer)."""

    pixmap_ready = Signal(object, QPixmap)   # (card, pixmap)

    def __init__(self):
        super().__init__()
        self._queue = queue.Queue()
        self._stop = False

    def submit(self, card, path):
        self._queue.put((card, path))

    def stop(self):
        """Ask the loop to exit at its next queue-poll and wake it up
        immediately with a no-op sentinel so shutdown doesn't wait out the
        full poll timeout. Called from MainWindow.closeEvent."""
        self._stop = True
        self._queue.put((None, None))

    def run(self):
        while not self._stop:
            try:
                card, path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if card is None:
                    continue
            except RuntimeError:
                continue
            self.pixmap_ready.emit(card, _load_pixmap(path))


PIXMAP_WORKER = PixmapWorker()
PIXMAP_WORKER.start()


# ---------------------------------------------------------------------------
# Rough-scan cache — a session-lifetime dict of folder_path -> (files,
# subdirs), shared by every FolderSection. Normally a section fills its own
# entry the moment it's expanded (see FolderSection.ensure_rough_pass). The
# "Load Selected Folders" bulk path (BulkFolderLoadManager, below) walks
# whole trees ahead of time and fills this cache for every folder it visits
# — together with pre-decoding every thumbnail into _PIXMAP_CACHE — so that
# actually expanding any of those folders afterwards is instant: no rescan,
# no per-thumbnail decode wait, because everything is already warm.
# ---------------------------------------------------------------------------
_ROUGH_SCAN_CACHE: dict[str, tuple[list, list]] = {}

# Bulk loading uses its own small, bounded thread pools, kept separate from
# ImageBrowser.thread_pool (used for single-folder interactive expand/scan)
# so that a huge "Load Selected Folders" run can't starve or stall ordinary
# click-to-expand browsing while it works through the backlog. One pool
# walks directories (I/O bound, cheap), the other decodes+scales images
# (CPU/I-O bound, the expensive part) — both capped at a small thread count
# so a "giant total amount of images" run degrades to "takes a while in the
# background" instead of spawning thousands of concurrent threads.
_BULK_SCAN_POOL = QThreadPool()
_BULK_SCAN_POOL.setMaxThreadCount(4)

_BULK_PIXMAP_POOL = QThreadPool()
_BULK_PIXMAP_POOL.setMaxThreadCount(max(2, min(6, os.cpu_count() or 4)))

# Images per background decode task. Batching (rather than one QRunnable per
# file) keeps task-scheduling overhead sane when a bulk load touches tens of
# thousands of images.
BULK_PRELOAD_CHUNK = 40
# Purely advisory (same spirit as FOLDER_COUNT_WARN / RECURSION_DEPTH_WARN
# above) — crossing this never stops or slows anything down, it just adds a
# note to the status line so the user understands why decoding is still
# trickling in after the folder scan itself has finished.
BULK_IMAGE_COUNT_WARN = 3000


class _BulkScanSignals(QObject):
    folder_ready = Signal(str, list, list)   # folder_path, files, subdirs
    error = Signal(str, str)                 # folder_path, message
    branch_done = Signal()                   # this root's whole subtree is exhausted


class _BulkScanWorker(QRunnable):
    """Recursively walks one selected folder's entire tree off the UI
    thread. Streams a folder_ready signal for every folder it visits (the
    root first, then every descendant, depth-first) so each one's result
    can be cached / absorbed by its FolderSection the moment it's ready,
    rather than waiting for the whole tree to finish."""

    def __init__(self, root_path, ignore_patterns, cancel_flag):
        super().__init__()
        self.root_path = root_path
        self.ignore_patterns = list(ignore_patterns)
        self.cancel_flag = cancel_flag
        self.signals = _BulkScanSignals()

    def run(self):
        try:
            self._walk(self.root_path)
        finally:
            self.signals.branch_done.emit()

    def _walk(self, path):
        if self.cancel_flag.is_set():
            return
        files, subdirs = [], []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if self.cancel_flag.is_set():
                        return
                    name = entry.name
                    try:
                        if entry.is_dir():
                            if not _folder_is_ignored(name, self.ignore_patterns):
                                subdirs.append(entry.path)
                        elif entry.is_file() and name.lower().endswith(IMAGE_FILE_EXTENSIONS):
                            files.append(entry.path)
                    except OSError:
                        continue
        except OSError as e:
            self.signals.error.emit(path, str(e))
            return
        files.sort(key=_natural_sort_key)
        subdirs.sort(key=_natural_sort_key)
        self.signals.folder_ready.emit(path, files, subdirs)
        for sub_path in subdirs:
            self._walk(sub_path)


class _BulkPixmapSignals(QObject):
    chunk_done = Signal(int)   # number of images processed in this chunk


class _BulkPixmapPreloadWorker(QRunnable):
    """Decodes+scales a batch of thumbnails straight into _PIXMAP_CACHE, on
    one of _BULK_PIXMAP_POOL's threads. Cheap no-op for anything already
    cached (e.g. a thumbnail the user already scrolled past normally)."""

    def __init__(self, filepaths, cancel_flag):
        super().__init__()
        self.filepaths = filepaths
        self.cancel_flag = cancel_flag
        self.signals = _BulkPixmapSignals()

    def run(self):
        done = 0
        for path in self.filepaths:
            if self.cancel_flag.is_set():
                break
            if path not in _PIXMAP_CACHE:
                try:
                    _load_pixmap(path)
                except Exception:
                    pass
            done += 1
        self.signals.chunk_done.emit(done)


class BulkFolderLoadManager(QObject):
    """Backs the "Load Selected Folders" context-menu action: recursively
    scans every selected folder tree and pre-decodes every thumbnail found
    in it, entirely off the UI thread, via the two bounded pools above.
    Once it's done (or even while it's still running), expanding any folder
    it touched is instant instead of triggering a fresh scan + per-image
    decode queue. Handles being pointed at a large number of folders and/or
    a huge total image count by streaming results through the pools in
    small batches rather than blocking on the whole thing at once, and
    supports folding a second selection into an already-running load
    instead of starting a competing one."""

    statusChanged = Signal(int, int, str)   # files_preloaded, files_queued, detail_text
    started = Signal()
    finished = Signal()

    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.active = False
        self._cancel_flag = threading.Event()
        self._pending_branches = 0
        self._roots = []
        self._folders_seen = 0
        self._files_seen = 0
        self._files_queued = 0
        self._files_preloaded = 0

    def start(self, root_paths):
        new_roots = [p for p in root_paths if p not in self._roots]
        if self.active:
            # Already running — fold the newly-requested roots into this
            # run instead of starting a second, overlapping one.
            self._roots.extend(new_roots)
            for p in new_roots:
                self._start_branch(p)
            return
        self.active = True
        self._cancel_flag = threading.Event()
        self._pending_branches = 0
        self._roots = list(root_paths)
        self._folders_seen = 0
        self._files_seen = 0
        self._files_queued = 0
        self._files_preloaded = 0
        self.started.emit()
        for p in self._roots:
            self._start_branch(p)
        self._emit_status()

    def cancel(self):
        self._cancel_flag.set()
        if self.active:
            # Don't wait for every in-flight worker to notice the flag and
            # report back (a straggling chunk that was interrupted
            # mid-batch would otherwise never bring _files_preloaded up to
            # _files_queued, leaving _maybe_finish() waiting forever) —
            # cancelling means "stop now" from the UI's point of view.
            self.active = False
            self.finished.emit()
            self.statusChanged.emit(
                self._files_preloaded, self._files_queued,
                f"Load canceled \u2014 {self._folders_seen} folder(s) scanned,\n"
                f"{self._files_seen} image(s) found are still cached."
            )

    def _start_branch(self, root_path):
        self._pending_branches += 1
        worker = _BulkScanWorker(root_path, self.browser.ignore_folder_patterns(), self._cancel_flag)
        worker.signals.folder_ready.connect(self._on_folder_ready)
        worker.signals.error.connect(self._on_scan_error)
        worker.signals.branch_done.connect(self._on_branch_done)
        _BULK_SCAN_POOL.start(worker)

    def _on_folder_ready(self, path, files, subdirs):
        if self._cancel_flag.is_set():
            return
        _ROUGH_SCAN_CACHE[path] = (files, subdirs)
        self._folders_seen += 1
        self._files_seen += len(files)
        # If a FolderSection already exists for this path (e.g. the user
        # had it partway expanded before triggering the bulk load), feed it
        # the fresh result right away instead of leaving it stale.
        section = self.browser.section_for_path(path)
        if section is not None:
            section.absorb_bulk_scan(files, subdirs)
        if files:
            self._files_queued += len(files)
            for i in range(0, len(files), BULK_PRELOAD_CHUNK):
                chunk = files[i:i + BULK_PRELOAD_CHUNK]
                worker = _BulkPixmapPreloadWorker(chunk, self._cancel_flag)
                worker.signals.chunk_done.connect(self._on_chunk_done)
                _BULK_PIXMAP_POOL.start(worker)
        self._emit_status()

    def _on_scan_error(self, path, message):
        if self._cancel_flag.is_set():
            return
        self._emit_status()

    def _on_chunk_done(self, n):
        if self._cancel_flag.is_set():
            return
        self._files_preloaded += n
        self._maybe_finish()
        self._emit_status()

    def _on_branch_done(self):
        if self._cancel_flag.is_set():
            return
        self._pending_branches -= 1
        self._maybe_finish()
        self._emit_status()

    def _maybe_finish(self):
        if not self.active:
            return
        scanning_done = self._pending_branches <= 0
        preloading_done = self._files_preloaded >= self._files_queued
        if scanning_done and preloading_done:
            self.active = False
            self.finished.emit()

    def _emit_status(self):
        scanning_done = self._pending_branches <= 0 and bool(self._roots)
        fully_done = scanning_done and self._files_preloaded >= self._files_queued
        if fully_done:
            self.statusChanged.emit(
                self._files_preloaded, self._files_queued,
                f"Loaded {self._folders_seen} folder(s), {self._files_seen} image(s)\n"
                f"across {len(self._roots)} selected folder(s)."
            )
            return
        warn = ""
        if self._files_seen > BULK_IMAGE_COUNT_WARN:
            warn = "\nLarge batch \u2014 thumbnails keep loading in the background."
        self.statusChanged.emit(
            self._files_preloaded, self._files_queued,
            f"Loading selected folders\u2026\n"
            f"{self._folders_seen} folder(s) scanned, {self._files_seen} image(s) found{warn}"
        )


# ---------------------------------------------------------------------------
# Folder scanning (still needed here, unlike indexer, since this app has no
# pre-built database of images — it walks the filesystem live). A single
# pass now always recurses into every subfolder (the "recursive" per-folder
# flag has been removed: everything configured in Settings is always fully
# recursive), skipping only folders that match a configured ignore pattern.
# ---------------------------------------------------------------------------
class _ScanSignals(QObject):
    done = Signal(str, list, list)   # section_path, files, subdirs
    failed = Signal(str, str)        # section_path, message


def _folder_is_ignored(name, ignore_patterns):
    for rule in ignore_patterns:
        pattern = (rule.get("pattern") or "").strip()
        if not pattern:
            continue
        mode = rule.get("mode", "starts_with")
        if mode == "contains":
            if pattern.lower() in name.lower():
                return True
        else:
            if name.lower().startswith(pattern.lower()):
                return True
    return False


class _RoughScanWorker(QRunnable):
    """Rough pass: list image filenames + immediate subfolders for one
    folder section. Fast — no image decoding at all. Always recurses (every
    subfolder is reported, to be turned into its own nested section) except
    folders matching a configured ignore pattern."""

    def __init__(self, section_path, ignore_patterns):
        super().__init__()
        self.section_path = section_path
        self.ignore_patterns = list(ignore_patterns)
        self.signals = _ScanSignals()

    def run(self):
        files, subdirs = [], []
        try:
            with os.scandir(self.section_path) as it:
                for entry in it:
                    name = entry.name
                    try:
                        if entry.is_dir():
                            if not _folder_is_ignored(name, self.ignore_patterns):
                                subdirs.append(entry.path)
                        elif entry.is_file() and name.lower().endswith(IMAGE_FILE_EXTENSIONS):
                            files.append(entry.path)
                    except OSError:
                        continue
        except FileNotFoundError:
            self.signals.failed.emit(self.section_path, "not_found")
            return
        except PermissionError:
            self.signals.failed.emit(self.section_path, "permission_denied")
            return
        except OSError as e:
            self.signals.failed.emit(self.section_path, f"error:{e}")
            return
        files.sort(key=_natural_sort_key)
        subdirs.sort(key=_natural_sort_key)
        self.signals.done.emit(self.section_path, files, subdirs)


# ---------------------------------------------------------------------------
# Thumbnail card — ported one-to-one from vael. indexer's ThumbnailCard:
# image only (no filename caption), a faint neutral border on hover and
# nothing more, and a drag source so a card can be dragged straight onto a
# roster icon below (or anywhere else that accepts a file URL drop).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# QScrollArea with middle-mouse-drag autopan, ported from vael. indexer's
# ResultsPanel: press the middle button anywhere in the viewport and drag
# up/down to scroll, exactly like indexer's image browser.
# ---------------------------------------------------------------------------
class _AutopanScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._autopan_active = False
        self._autopan_origin = QPoint()
        self._autopan_timer = QTimer(self)
        self._autopan_timer.setInterval(16)  # ~60 fps
        self._autopan_timer.timeout.connect(self._autopan_tick)
        self.viewport().setMouseTracking(True)
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.viewport():
            t = event.type()
            if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MiddleButton:
                self._autopan_active = True
                self._autopan_origin = event.position().toPoint()
                self.viewport().setCursor(Qt.CursorShape.SizeVerCursor)
                self._autopan_timer.start()
                return True
            if t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MiddleButton:
                self._stop_autopan()
                return True
        return super().eventFilter(obj, event)

    def _autopan_tick(self):
        if not self._autopan_active:
            return
        cursor_pos = self.viewport().mapFromGlobal(self.viewport().cursor().pos())
        delta_y = cursor_pos.y() - self._autopan_origin.y()
        if abs(delta_y) < 8:
            return
        speed = int((delta_y - (8 if delta_y > 0 else -8)) * 0.4)
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + speed)

    def _stop_autopan(self):
        self._autopan_active = False
        self._autopan_timer.stop()
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)


class ThumbnailCard(QWidget):
    view_requested = Signal(object)   # emits self on a plain left-click (no drag)
    # Right-click ("open context menu on an image") -- emits self plus the
    # global cursor position so the caller (FolderSection) can build and
    # show a menu listing every available roster/image input.
    context_menu_requested = Signal(object, QPoint)

    CARD_W = THUMB_W
    CARD_H = THUMB_H

    def __init__(self, filepath, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.setFixedSize(self.CARD_W, self.CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self._hovered = False
        self._drag_start_pos = None
        self._image_loaded = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._img_lbl = QLabel()
        self._img_lbl.setObjectName("cardImage")
        self._img_lbl.setFixedSize(THUMB_W, THUMB_H)
        self._img_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._img_lbl)

        PIXMAP_WORKER.pixmap_ready.connect(self._on_pixmap_ready)

    def request_image(self):
        """Called by FolderSection once the section is expanded. No-op if
        this card's image is already loaded/loading."""
        if self._image_loaded:
            return
        if self.filepath in _PIXMAP_CACHE:
            self._apply_pixmap(_PIXMAP_CACHE[self.filepath])
        else:
            PIXMAP_WORKER.submit(self, self.filepath)

    def _on_pixmap_ready(self, card, pix):
        if card is not self:
            return
        self._apply_pixmap(pix)
        try:
            PIXMAP_WORKER.pixmap_ready.disconnect(self._on_pixmap_ready)
        except RuntimeError:
            pass

    def _apply_pixmap(self, pix):
        self._image_loaded = True
        if not pix.isNull():
            rounded = QPixmap(THUMB_W, THUMB_H)
            rounded.fill(Qt.GlobalColor.transparent)
            p = QPainter(rounded)
            p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, THUMB_W, THUMB_H, 7, 7)
            p.setClipPath(path)
            p.drawPixmap(0, 0, pix)
            p.end()
            self._img_lbl.setPixmap(rounded)
        else:
            self._img_lbl.setText("?")

    # -- click / drag (mirrors indexer's card interactions) ---------------
    # NOTE: every one of these handlers explicitly accept()s left-button
    # events instead of falling through to QWidget's default implementation.
    # QWidget's base mouse handlers ignore() unhandled events so they can
    # bubble up to the parent -- which is exactly right for e.g. middle-
    # button autopan (see _AutopanScrollArea), but wrong here: this card
    # lives inside a FolderSection body, which itself lives inside a
    # _RubberBandArea (see ImageBrowser.reload_folders / FolderSection).
    # If a left-click on a thumbnail were left unaccepted, Qt would hand it
    # up to that ancestor's mousePressEvent, which grabs the mouse and
    # starts an Explorer-style folder rubber-band selection -- silently
    # stealing every subsequent move/release from this card and making it
    # impossible to drag an image out. Accepting the event here keeps the
    # whole press/move/release gesture on the card, where it belongs.
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.position().toPoint()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is not None and (event.buttons() & Qt.LeftButton):
            dist = (event.position().toPoint() - self._drag_start_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_start_pos = None
                self._start_drag()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_start_pos is not None:
            self._drag_start_pos = None
            self.view_requested.emit(self)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _start_drag(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self.filepath)])
        drag = QDrag(self)
        drag.setMimeData(mime)
        pix = _load_pixmap(self.filepath)
        if not pix.isNull():
            drag.setPixmap(pix.scaled(THUMB_W // 2, THUMB_H // 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        drag.exec(Qt.CopyAction)

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        self.context_menu_requested.emit(self, event.globalPos())
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hovered:
            # Subtle white-grey, almost-transparent highlight outline so
            # it's clear which thumbnail is under the cursor.
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            pen = QPen(QColor(235, 235, 240, 90))
            pen.setWidth(1)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(1, 1, THUMB_W - 2, THUMB_H - 2, 7, 7)
            p.end()


class _InertMouseArea(QWidget):
    """A plain container that deliberately swallows left-button mouse
    presses/moves/releases on its own blank space instead of letting them
    bubble up to an ancestor _RubberBandArea. Used for the thumbnail card
    grid of an *open* folder: without this, an unhandled click on the
    gaps between thumbnails would propagate to the folder header's rubber
    -band area and start a folder-selection drag, exactly the same failure
    mode a plain ThumbnailCard press would otherwise hit (see
    ThumbnailCard's mouse handlers). Right/middle clicks and anything that
    isn't a plain left-button press still pass through untouched, so
    middle-button autopan (see _AutopanScrollArea) keeps working."""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# One folder header, or (nested under it) one recursive sub-header — design
# ported one-to-one from vael. indexer's FolderSection: a flat QToolButton
# header with an arrow indicator, indent-by-depth, and a QGridLayout card
# grid that reflows its column count to the available width. Unlike
# indexer, this app has no pre-built index, so contents are still scanned
# lazily off the UI thread the first time a section is expanded — but every
# folder is now always fully recursive, and a top-level (depth 0) section
# auto-expands itself the moment its scan completes, since folders
# configured in Settings are guaranteed to hold only more folders, never
# images directly, so there's no reason to make the user click twice just
# to see the first level of real subfolders.
# ---------------------------------------------------------------------------
class _RubberBandArea(QWidget):
    """A plain container that starts a Windows-Explorer-style click-and-
    drag rectangle selection of folder headers when the drag begins on
    blank space inside it — i.e. not on a header button or a thumbnail
    card, both of which are child widgets that intercept the press first,
    exactly like clicking directly on a desktop icon doesn't start a
    rubber band in Explorer either. Used for every "background" area
    inside a browser tab that a drag might reasonably start from: the
    header row's empty stretch, the space below the last subfolder, and
    the top-level tab wrapper itself.

    No visual selection rectangle is drawn — as the drag crosses a folder
    header it's highlighted directly (via ImageBrowser.set_folder_selection
    / FolderSection.set_selected), which reads clearer than a rectangle
    that's mostly just passing over irrelevant blank space anyway."""

    def __init__(self, browser, parent=None):
        super().__init__(parent)
        self._browser = browser
        self._dragging = False
        self._origin_global = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._origin_global = event.globalPosition().toPoint()
            additive = bool(event.modifiers() & Qt.ControlModifier)
            self._browser.begin_rubber_band_drag(additive)
            self._dragging = True
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            now_global = event.globalPosition().toPoint()
            self._browser.update_rubber_band_drag(QRect(self._origin_global, now_global).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self.releaseMouse()
            self._dragging = False
            self._browser.end_rubber_band_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class FolderSection(QWidget):
    def __init__(self, browser, path, depth=0, closable=False):
        super().__init__()
        self.browser = browser
        self.path = path
        self.depth = depth
        self.closable = closable
        self.setObjectName("folderSection")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._expanded = False
        self._rough_done = False
        self._rough_started = False
        self._materialized = False
        self._files = []
        self._pending_subdirs = []
        self._cards = []
        self._child_sections = []
        self._current_cols = 0
        self._selected = False

        browser.register_section(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # _RubberBandArea (not plain QWidget) so a click-drag that starts on
        # the blank space beside the folder name can kick off a Windows-
        # style rectangle multi-select of folder headers (see
        # ImageBrowser.begin_rubber_band_drag and friends).
        header_wrap = _RubberBandArea(browser)
        header_wrap.setObjectName("sectionHeaderWrap")
        header_wrap.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        indent = depth * 14
        hw_lay = QHBoxLayout(header_wrap)
        hw_lay.setContentsMargins(indent, 0, 0, 0)
        hw_lay.setSpacing(4)

        is_deep = depth > RECURSION_DEPTH_WARN
        name_text = Path(path).name or path

        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setProperty("depth0", "true" if depth == 0 else "false")
        if is_deep:
            self.header.setProperty("deep", True)
        self.header.setArrowType(Qt.ArrowType.RightArrow)
        self.header.setText(name_text + (" \u26a0" if is_deep else ""))
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.header.setFixedHeight(26 if depth == 0 else 22)
        self.header.setCursor(Qt.PointingHandCursor)
        # Let right-clicks fall through to FolderSection.contextMenuEvent
        # (below) instead of the button/wrap trying to handle them.
        self.header.setContextMenuPolicy(Qt.NoContextMenu)
        header_wrap.setContextMenuPolicy(Qt.NoContextMenu)
        tooltip = path
        if is_deep:
            tooltip += (
                f"\n\u26a0 Nested {depth} levels deep \u2014 past the recommended "
                f"{RECURSION_DEPTH_WARN}-level guideline. Still fully functional, "
                f"just flagged for awareness."
            )
        tooltip += (
            "\n\nCtrl+click to multi-select, or drag a rectangle over several "
            "folders, then right-click for \u201cLoad Selected Folders\u201d."
        )
        self.header.setToolTip(tooltip)
        self.header.clicked.connect(self._on_header_clicked)
        hw_lay.addWidget(self.header, 0)

        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("headerCount")
        hw_lay.addWidget(self.count_lbl, 0)
        hw_lay.addStretch(1)

        if closable:
            close_btn = QToolButton()
            close_btn.setObjectName("iconButton")
            close_btn.setText("\u00d7")
            close_btn.setToolTip("Hide for this session (not removed from Settings)")
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.clicked.connect(self._close_for_session)
            hw_lay.addWidget(close_btn)

        outer.addWidget(header_wrap)

        self.body = QWidget()
        self.body.setObjectName("sectionBody")
        self.body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body_lay = QVBoxLayout(self.body)
        body_lay.setContentsMargins(indent + 8, 4, 4, 6)
        body_lay.setSpacing(6)

        # _InertMouseArea (not plain QWidget, not _RubberBandArea) — this
        # holds the actual thumbnail cards of an *open* folder. When a
        # folder is open the user's blank-space click-drag here is
        # virtually always an attempt to grab an image and drag it down to
        # the input roster, not to rubber-band-select folder headers, so
        # this area deliberately doesn't participate in folder selection at
        # all: presses on blank space here are swallowed and do nothing,
        # rather than bubbling up to the header's rubber-band area, leaving
        # ThumbnailCard's own drag handling untouched.
        self.card_widget = _InertMouseArea()
        self.card_widget.setObjectName("cardGrid")
        self.card_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.card_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.card_grid = QGridLayout(self.card_widget)
        self.card_grid.setContentsMargins(0, 0, 0, 0)
        self.card_grid.setSpacing(6)
        self.card_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        body_lay.addWidget(self.card_widget)

        self.children_container = _RubberBandArea(browser)
        self.children_lay = QVBoxLayout(self.children_container)
        self.children_lay.setContentsMargins(0, 0, 0, 0)
        self.children_lay.setSpacing(4)
        body_lay.addWidget(self.children_container)

        outer.addWidget(self.body)
        self.body.setVisible(False)

    def _close_for_session(self):
        self.browser.close_tab_for_session(self.path)

    # -- multi-select (ctrl+click / rubber-band drag) -----------------------
    def _on_header_clicked(self):
        if QApplication.keyboardModifiers() & Qt.ControlModifier:
            self.browser.toggle_folder_selection(self.path)
        else:
            if self.browser.has_folder_selection():
                self.browser.clear_folder_selection()
            self.toggle()

    def set_selected(self, selected):
        if selected == self._selected:
            return
        self._selected = selected
        self.header.setProperty("selected", "true" if selected else "false")
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)

    def contextMenuEvent(self, event):
        # Mirrors Explorer: right-clicking a folder that's already part of
        # the current multi-selection keeps the whole selection; right-
        # clicking one that isn't selected replaces the selection with just
        # that folder.
        if self.path not in self.browser.selected_folder_paths():
            self.browser.clear_folder_selection()
            self.browser.toggle_folder_selection(self.path)
        self.browser.show_folder_context_menu(self, event.globalPos())
        event.accept()

    def _on_card_view_requested(self, card):
        """A thumbnail was left-clicked (not dragged). If a roster slot is
        armed, clicking still assigns to it as before; otherwise open the
        full-size viewer, using this section's own cards as the prev/next
        list (mirrors vael. indexer's ImgViewerOverlay)."""
        main_window = self.browser.main_window
        if main_window.roster_bar.armed_index is not None:
            main_window._on_browser_thumbnail_clicked(card.filepath)
        else:
            main_window.open_image_viewer(card, self._cards)

    def _on_card_context_menu(self, card, global_pos):
        """Right-click on a thumbnail: offer a direct 'Add to <input name>'
        shortcut for every roster slot on the active workflow, so an image
        can be assigned to a specific input without arming it first."""
        roster_bar = self.browser.main_window.roster_bar
        if not roster_bar.icons:
            return
        menu = QMenu(self)
        for icon in roster_bar.icons:
            # Roster captions are "<title> (#<node id>)" (see node_label);
            # the menu should read just "Add to LEFT", not "Add to LEFT
            # (#1)", so strip the node-id suffix for display only.
            display_name = icon.caption.rsplit(" (#", 1)[0]
            action = menu.addAction(f"Add to {display_name}")
            action.triggered.connect(
                lambda _checked=False, idx=icon.index, fp=card.filepath: roster_bar.assign_to_slot(idx, fp)
            )
        menu.exec(global_pos)

    # -- expand / collapse -------------------------------------------------
    def toggle(self):
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded, persist=True):
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.header.setProperty("expanded", "true" if expanded else "false")
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)
        self.body.setVisible(expanded)
        if expanded:
            if not self._rough_done:
                self.ensure_rough_pass()
            else:
                if not self._materialized:
                    # A bulk "Load Selected Folders" run already scanned
                    # this folder while it was still collapsed (see
                    # absorb_bulk_scan) but deliberately held off building
                    # the actual card/child widgets until it's really
                    # needed — do that now that the user is opening it.
                    self._materialize()
                if self._cards:
                    self._current_cols = 0
                    QTimer.singleShot(0, self._relayout_cards)
                    for card in self._cards:
                        card.request_image()
        if persist:
            self.browser.note_expanded(self.path, expanded)

    def is_expanded(self):
        return self._expanded

    # -- lazy scanning ------------------------------------------------------
    def ensure_rough_pass(self):
        if self._rough_done or self._rough_started:
            return
        # A bulk load (or an earlier visit) may already have scanned this
        # exact folder — reuse that instead of scanning it all over again,
        # which is the whole point of "Load Selected Folders": expanding a
        # folder it already touched should feel instant.
        cached = _ROUGH_SCAN_CACHE.get(self.path)
        if cached is not None:
            self._on_rough_done(self.path, cached[0], cached[1])
            return
        self._rough_started = True
        self.count_lbl.setText("\u2026")
        self._set_count_state("normal")
        worker = _RoughScanWorker(self.path, self.browser.ignore_folder_patterns())
        worker.signals.done.connect(self._on_rough_done)
        worker.signals.failed.connect(self._on_rough_failed)
        self.browser.thread_pool.start(worker)

    def _on_rough_failed(self, path, message):
        if path != self.path:
            return
        self._rough_started = False
        labels = {
            "not_found": "(folder not found)",
            "permission_denied": "(permission denied)",
        }
        self.count_lbl.setText(labels.get(message, "(unreadable)"))
        self._set_count_state("error")

    def _set_count_state(self, state):
        self.count_lbl.setProperty("state", state)
        self.count_lbl.style().unpolish(self.count_lbl)
        self.count_lbl.style().polish(self.count_lbl)

    def _on_rough_done(self, path, files, subdirs):
        if path != self.path:
            return
        self._rough_done = True
        self._files = files
        self._pending_subdirs = subdirs
        _ROUGH_SCAN_CACHE[path] = (files, subdirs)
        self.count_lbl.setText(f"({len(files)})" if (files or subdirs) else "(empty)")
        self._set_count_state("normal" if (files or subdirs) else "muted")
        self._materialize()

        # A top-level folder is guaranteed (by convention — see Settings)
        # to hold only more folders, not images. Auto-expand it the moment
        # its scan finishes so the user lands straight on the first level
        # of real subfolders instead of clicking once just to reveal them.
        if self.depth == 0 and not self._expanded:
            self.set_expanded(True, persist=False)
        elif self._expanded and self._cards:
            self._current_cols = 0
            QTimer.singleShot(0, self._relayout_cards)
            for card in self._cards:
                card.request_image()

    def absorb_bulk_scan(self, files, subdirs):
        """Called by BulkFolderLoadManager when a background 'Load Selected
        Folders' scan reaches this exact folder. Unlike the interactive
        ensure_rough_pass()/_on_rough_done() path, this deliberately does
        NOT build ThumbnailCard/child-FolderSection widgets while the
        section is collapsed — a bulk load can touch thousands of folders
        at once, and eagerly materializing every one of them would dump an
        enormous widget tree on the UI thread. The header count still
        updates immediately; the rest is built lazily in set_expanded()
        the moment the user actually opens it, by which point the scan
        result and every thumbnail in it are already cached."""
        if self._rough_done:
            return  # already scanned interactively — don't duplicate it
        self._rough_done = True
        self._files = files
        self._pending_subdirs = subdirs
        self.count_lbl.setText(f"({len(files)})" if (files or subdirs) else "(empty)")
        self._set_count_state("normal" if (files or subdirs) else "muted")
        if self._expanded:
            # Rare (would mean the user opened it mid-bulk-load), but if it
            # really is visible right now, build it out immediately.
            self._materialize()
            self._current_cols = 0
            QTimer.singleShot(0, self._relayout_cards)
            for card in self._cards:
                card.request_image()
        elif self.depth == 0:
            self.set_expanded(True, persist=False)

    def _materialize(self):
        """Turn a completed (possibly cached) scan result into actual
        ThumbnailCard / child-FolderSection widgets. Idempotent."""
        if self._materialized:
            return
        self._materialized = True
        for filepath in self._files:
            card = ThumbnailCard(filepath)
            card.view_requested.connect(self._on_card_view_requested)
            card.context_menu_requested.connect(self._on_card_context_menu)
            i = len(self._cards)
            self._cards.append(card)
            self.card_grid.addWidget(card, i // 4, i % 4)

        for sub_path in self._pending_subdirs:
            child = FolderSection(self.browser, sub_path, depth=self.depth + 1)
            self.children_lay.addWidget(child)
            self._child_sections.append(child)
            if sub_path in self.browser.saved_expanded_paths:
                child.set_expanded(True, persist=False)
        self._pending_subdirs = []

    # -- reflow (ported from indexer's _relayout_cards) --------------------
    def _relayout_cards(self):
        avail_w = self.card_widget.width()
        if avail_w < THUMB_W:
            return
        cols = max(1, avail_w // (THUMB_W + 6))
        if cols == self._current_cols:
            return
        self._current_cols = cols
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self.card_grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._expanded and self._cards:
            self._relayout_cards()


# ---------------------------------------------------------------------------
# Full-size image viewer overlay — ported one-to-one from vael. indexer's
# ImgViewerOverlay, adapted for cover's ThumbnailCard (plain .filepath
# instead of an .asset dict, no per-card context menu to delegate to).
#
#   • Opens centred over the image browser on a plain left-click of any
#     ThumbnailCard (dragging a card down to the roster still works
#     exactly as before — that's a drag, not a click).
#   • Prev/Next arrows and the Left/Right keys navigate the cards within
#     the same folder section the clicked card came from.
#   • Click on the dim area outside the image, or the × / Esc, closes it.
# ---------------------------------------------------------------------------
class ImgViewerOverlay(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

        self._cards = []
        self._idx = 0
        self._current_card = None

        self._close_btn = QToolButton(self)
        self._close_btn.setText("\u2715")
        self._close_btn.setObjectName("viewerCloseBtn")
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.clicked.connect(self.close_viewer)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._img_lbl = QLabel(self)
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setObjectName("viewerImage")
        self._img_lbl.setScaledContents(False)

        self._name_lbl = QLabel(self)
        self._name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_lbl.setObjectName("viewerName")

        self._prev_btn = QToolButton(self)
        self._prev_btn.setText("\u2039")
        self._prev_btn.setObjectName("viewerNavBtn")
        self._prev_btn.setFixedSize(44, 80)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(self._go_prev)

        self._next_btn = QToolButton(self)
        self._next_btn.setText("\u203a")
        self._next_btn.setObjectName("viewerNavBtn")
        self._next_btn.setFixedSize(44, 80)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(self._go_next)

    # -- public API ---------------------------------------------------------
    def open_viewer(self, card, cards):
        self._cards = cards
        self._idx = cards.index(card) if card in cards else 0
        self._current_card = card
        self.resize(self.parent().size())
        self._load_current()
        self._update_nav_state()
        self.show()
        self.raise_()
        self.setFocus()

    def close_viewer(self):
        self.hide()
        self._current_card = None
        self._cards = []

    # -- navigation -----------------------------------------------------------
    def _go_prev(self):
        if self._idx > 0:
            self._idx -= 1
            self._current_card = self._cards[self._idx]
            self._load_current()
            self._update_nav_state()

    def _go_next(self):
        if self._idx < len(self._cards) - 1:
            self._idx += 1
            self._current_card = self._cards[self._idx]
            self._load_current()
            self._update_nav_state()

    def _update_nav_state(self):
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(self._cards) - 1)

    # -- image loading --------------------------------------------------------
    def _load_current(self):
        card = self._current_card
        if card is None:
            return
        self._name_lbl.setText(Path(card.filepath).name)

        pix = QPixmap(card.filepath)
        if pix.isNull():
            self._img_lbl.clear()
            self._img_lbl.setText("?")
        else:
            self._img_lbl.setProperty("_raw_pix", pix)
            self._scale_image()
        self._place_widgets()

    def _scale_image(self):
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

    def _image_area_size(self):
        w, h = self.width(), self.height()
        margin_x = 110
        margin_y = 100
        return max(200, w - margin_x * 2), max(200, h - margin_y)

    # -- widget placement -------------------------------------------------------
    def _place_widgets(self):
        ow, oh = self.width(), self.height()

        pad = 14
        self._close_btn.move(ow - self._close_btn.width() - pad, pad)

        img_w, img_h = self._img_lbl.width(), self._img_lbl.height()
        img_x = (ow - img_w) // 2
        img_y = max(50, (oh - img_h - 40) // 2)
        self._img_lbl.move(img_x, img_y)

        name_h = 28
        self._name_lbl.setFixedSize(min(600, ow - 40), name_h)
        name_x = (ow - self._name_lbl.width()) // 2
        name_y = img_y + img_h + 10
        self._name_lbl.move(name_x, name_y)

        btn_y = img_y + (img_h - self._prev_btn.height()) // 2
        left_edge = img_x - self._prev_btn.width() - 12
        right_edge = img_x + img_w + 12
        self._prev_btn.move(max(8, left_edge), btn_y)
        self._next_btn.move(min(ow - self._next_btn.width() - 8, right_edge), btn_y)

    # -- events -----------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible():
            self._scale_image()
            self._place_widgets()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 180))
        p.end()

    def mousePressEvent(self, event):
        """Click outside the image panel closes the viewer."""
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._img_lbl.geometry().contains(event.position().toPoint()):
                self.close_viewer()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_viewer()
        elif key == Qt.Key.Key_Left:
            self._go_prev()
        elif key == Qt.Key.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Blur overlay shown while a "Load Selected Folders" bulk scan is running.
# Rather than a boxed panel sitting on a dimmed backdrop, this blurs a
# snapshot of the *entire app* (sidebars, roster, everything -- it's
# parented to the app's content root, not just the image browser) to read
# as "the whole UI is held up right now", and floats only bare text, a
# progress bar, and a Cancel button in the middle -- no panel chrome.
# ---------------------------------------------------------------------------
class _BulkLoadOverlay(QWidget):
    BLUR_RADIUS = 22

    def __init__(self, parent, on_cancel):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._blurred_bg = None
        self.hide()

        self._panel = QWidget(self)
        self._panel.setObjectName("bulkLoadPanel")
        panel_lay = QVBoxLayout(self._panel)
        panel_lay.setContentsMargins(30, 22, 30, 22)
        panel_lay.setSpacing(10)

        self.progress = QProgressBar()
        self.progress.setObjectName("bulkLoadProgress")
        self.progress.setFixedWidth(280)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        panel_lay.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignHCenter)

        # Bold/bright "[done / total]" readout — deliberately its own,
        # more prominent label sitting at the very top of the text block.
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("bulkLoadCount")
        self.count_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_lay.addWidget(self.count_lbl)

        self.detail_lbl = QLabel("")
        self.detail_lbl.setObjectName("bulkLoadDetail")
        self.detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_lbl.setWordWrap(True)
        panel_lay.addWidget(self.detail_lbl)

        self.cancel_btn = QToolButton()
        self.cancel_btn.setObjectName("iconButton")
        self.cancel_btn.setText("Cancel load")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(on_cancel)
        panel_lay.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_progress(self, done, total, detail_text):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
            self.count_lbl.setText(f"{done} / {total}")
            self.count_lbl.show()
        else:
            self.progress.setRange(0, 0)
            self.count_lbl.hide()
        self.detail_lbl.setText(detail_text)
        self._place_panel()

    def show_overlay(self):
        self.resize(self.parent().size())
        self._capture_blurred_background()
        self._place_panel()
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.hide()
        self._blurred_bg = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_panel()

    def _place_panel(self):
        self._panel.adjustSize()
        pw, ph = self._panel.width(), self._panel.height()
        self._panel.move((self.width() - pw) // 2, (self.height() - ph) // 2)

    def _capture_blurred_background(self):
        """Grab whatever's currently behind this overlay (the whole app
        content) and blur it, so the backdrop itself reads as held-up
        rather than needing a separate panel to carry that meaning."""
        self.hide()
        snapshot = self.parent().grab()
        self.show()
        if snapshot.isNull():
            self._blurred_bg = None
            return
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(snapshot)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(self.BLUR_RADIUS)
        item.setGraphicsEffect(blur)
        scene.addItem(item)
        result = QPixmap(snapshot.size())
        result.fill(Qt.transparent)
        p = QPainter(result)
        scene.render(p)
        p.end()
        self._blurred_bg = result

    def paintEvent(self, event):
        p = QPainter(self)
        if self._blurred_bg is not None:
            p.drawPixmap(0, 0, self._blurred_bg)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        p.end()


# ---------------------------------------------------------------------------
# Generic dimming/blur backdrop reused behind the Settings and Hotkeys
# dialogs -- visually identical treatment to _BulkLoadOverlay above (same
# blurred snapshot + dark fill), just without any panel/progress content of
# its own, so it reads the same "you can't interact with the app right now"
# way a bulk folder load does.
# ---------------------------------------------------------------------------
class _ModalDimOverlay(QWidget):
    BLUR_RADIUS = 22

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._blurred_bg = None
        self.hide()

    def show_overlay(self):
        self.resize(self.parent().size())
        self._capture_blurred_background()
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.hide()
        self._blurred_bg = None

    def _capture_blurred_background(self):
        self.hide()
        snapshot = self.parent().grab()
        self.show()
        if snapshot.isNull():
            self._blurred_bg = None
            return
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(snapshot)
        blur = QGraphicsBlurEffect()
        blur.setBlurRadius(self.BLUR_RADIUS)
        item.setGraphicsEffect(blur)
        scene.addItem(item)
        result = QPixmap(snapshot.size())
        result.fill(Qt.transparent)
        p = QPainter(result)
        scene.render(p)
        p.end()
        self._blurred_bg = result

    def paintEvent(self, event):
        p = QPainter(self)
        if self._blurred_bg is not None:
            p.drawPixmap(0, 0, self._blurred_bg)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        p.end()


# ---------------------------------------------------------------------------
# Small red "count" badge used to show how many posts are currently queued
# (spec: Queue count indicator). Draws itself as a perfect circle for
# single-digit counts and widens into a pill/stadium shape once the number
# needs more horizontal room (double/triple digits+), so the digits never
# get squeezed into an undersized circle.
# ---------------------------------------------------------------------------
class _QueueCountBadge(QWidget):
    HEIGHT = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("queueCountBadge")
        self._count = 0
        self._label = QLabel(self)
        self._label.setObjectName("queueCountBadgeLabel")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(self.HEIGHT, self.HEIGHT)
        self.hide()

    def count(self):
        return self._count

    def set_count(self, count):
        self._count = max(0, int(count))
        if self._count <= 0:
            self.hide()
            return
        self._label.setText(str(self._count) if self._count < 1000 else "999+")
        self._resize_to_content()
        self.show()
        self.raise_()

    def _resize_to_content(self):
        fm = self._label.fontMetrics()
        text_w = fm.horizontalAdvance(self._label.text())
        w = max(self.HEIGHT, text_w + 12)
        self.setFixedSize(w, self.HEIGHT)
        self._label.setGeometry(0, 0, w, self.HEIGHT)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#e5484d"))
        radius = self.height() / 2
        p.drawRoundedRect(self.rect(), radius, radius)
        p.end()


# ---------------------------------------------------------------------------
# Center — Image Browser. A single, persistent, global instance shared by
# every workflow: one tab per configured top-level folder (always fully
# recursive), lazy scanning off the UI thread, and an in-memory-only
# thumbnail cache. Switching the active workflow never touches this widget
# — only the roster below it.
# ---------------------------------------------------------------------------
class ImageBrowser(QWidget):
    thumbnailClicked = Signal(str)   # filepath — MainWindow feeds this to the armed roster slot

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setObjectName("imageBrowserPanel")
        self.thread_pool = QThreadPool.globalInstance()
        self._closed_this_session = set()   # paths ×'d away — in-memory only, cleared on relaunch

        # Deliberately never loaded from (or saved to) config_data: the
        # explorer always starts on the first tab with nothing expanded,
        # regardless of whatever state it was left in last session. These
        # still track expand/tab state during the current run (e.g. so
        # reload_folders() can keep the same tab open after an in-session
        # Settings change), they just don't persist across restarts.
        self.saved_expanded_paths = set()
        self._saved_active_tab = None
        self._top_sections = []   # top-level FolderSection per tab index, in tab order

        # -- multi-select folders (ctrl+click / rubber-band drag) + bulk
        # "Load Selected Folders" background loading -----------------------
        self._sections_by_path = {}       # path -> FolderSection, every depth, current tabs only
        self._selected_folder_paths = set()
        self._rb_base_selection = set()   # selection snapshot at the start of the current drag
        self.bulk_loader = BulkFolderLoadManager(self)
        self.bulk_loader.started.connect(self._on_bulk_load_started)
        self.bulk_loader.statusChanged.connect(self._on_bulk_load_status)
        self.bulk_loader.finished.connect(self._on_bulk_load_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("browserTabs")
        self.tabs.setDocumentMode(True)
        # Qt's Fusion style draws its own native "tab bar base" line under
        # the tab bar regardless of any ::pane border in the stylesheet --
        # that's the stray straight line that used to run the full width
        # of the panel above the tab strip (and the settings icon), with
        # hard corners that didn't match the tab strip's own rounded pill.
        # Turning it off leaves only our own styled pill border.
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Settings row: floats *over* the top-right corner of the tab strip
        # instead of occupying its own full-width row above it -- that used
        # to leave a whole empty bar sitting on top of the tabs. It's a
        # free-floating child of the ImageBrowser itself (positioned/raised
        # in resizeEvent / showEvent below) so it stays reachable even when
        # the tabs/empty-hint stack underneath has zero folders configured
        # and hides itself.
        status_bar = QWidget(self)
        status_bar.setObjectName("browserStatusBar")
        status_lay = QHBoxLayout(status_bar)
        # A small, deliberate gap from the true right edge -- enough that
        # the icon doesn't feel glued to the corner, without stranding it
        # out in empty space (that was the overcorrection last time: a
        # full icon-width gap read as too much on its own).
        status_lay.setContentsMargins(0, 0, 8, 0)
        status_lay.setSpacing(2)

        # The Settings button used to live here; it now lives at the
        # bottom-left of the workflow sidebar instead (see WorkflowSidebar),
        # so this row only houses the restore-hidden-folders button now.
        self.restore_hidden_btn = QToolButton()
        self.restore_hidden_btn.setObjectName("iconButton")
        self.restore_hidden_btn.setText("\u21bb")
        self.restore_hidden_btn.setPopupMode(QToolButton.InstantPopup)
        self.restore_hidden_btn.setCursor(Qt.PointingHandCursor)
        self.restore_hidden_btn.hide()
        status_lay.addWidget(self.restore_hidden_btn)

        # Reload button — always the rightmost item in the bar. Re-scans
        # just the currently open (active-tab) top-level folder from disk,
        # without touching any other configured folder's state.
        self.reload_folder_btn = QToolButton()
        self.reload_folder_btn.setObjectName("browserSettingsBtn")
        self.reload_folder_btn.setText("\u27f3")
        self.reload_folder_btn.setToolTip("Reload the current folder")
        self.reload_folder_btn.setCursor(Qt.PointingHandCursor)
        self.reload_folder_btn.clicked.connect(self.reload_current_folder)
        self.reload_folder_btn.hide()
        status_lay.addWidget(self.reload_folder_btn)

        self._status_bar = status_bar
        status_bar.adjustSize()
        status_bar.raise_()

        layout.addWidget(self.tabs, 1)

        # Dimming/blur overlay for "Load Selected Folders" bulk scans.
        # Parented to the app's content root (not just this panel) so the
        # blur covers the whole app -- sidebars, roster, everything --
        # rather than just the image browser's own rectangle.
        self.bulk_overlay = _BulkLoadOverlay(main_window._content_root, self.bulk_loader.cancel)

        self.empty_hint = QLabel(
            "No folders configured yet.\n"
            "Open the \u2699 Settings button (bottom-left of the workflow "
            "sidebar) \u2192 Image Selection to add one."
        )
        self.empty_hint.setObjectName("hint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_hint, 1)

        self.reload_folders()
        self._position_status_bar()

    def ignore_folder_patterns(self):
        return self.main_window.config_data.get("ignore_folder_patterns", []) or []

    # -- folder registry (every FolderSection currently built, any depth) --
    def register_section(self, section):
        self._sections_by_path[section.path] = section

    def section_for_path(self, path):
        return self._sections_by_path.get(path)

    # -- multi-select (ctrl+click / rubber-band drag) -----------------------
    def selected_folder_paths(self):
        return set(self._selected_folder_paths)

    def has_folder_selection(self):
        return bool(self._selected_folder_paths)

    def toggle_folder_selection(self, path):
        section = self._sections_by_path.get(path)
        if section is None:
            return
        if path in self._selected_folder_paths:
            self._selected_folder_paths.discard(path)
            section.set_selected(False)
        else:
            self._selected_folder_paths.add(path)
            section.set_selected(True)

    def clear_folder_selection(self):
        for path in list(self._selected_folder_paths):
            section = self._sections_by_path.get(path)
            if section is not None:
                section.set_selected(False)
        self._selected_folder_paths.clear()

    def set_folder_selection(self, paths):
        paths = set(paths)
        if paths == self._selected_folder_paths:
            return
        for path, section in self._sections_by_path.items():
            section.set_selected(path in paths)
        self._selected_folder_paths = paths

    def selected_folder_sections(self):
        return [
            self._sections_by_path[p]
            for p in self._selected_folder_paths
            if p in self._sections_by_path
        ]

    # -- rubber-band drag-select (driven by _RubberBandArea) ----------------
    def begin_rubber_band_drag(self, additive):
        if not additive:
            self.clear_folder_selection()
        self._rb_base_selection = set(self._selected_folder_paths)

    def update_rubber_band_drag(self, global_rect):
        hits = self._sections_intersecting_global_rect(global_rect)
        self.set_folder_selection(self._rb_base_selection | hits)

    def end_rubber_band_drag(self):
        self._rb_base_selection = set()

    def _sections_intersecting_global_rect(self, global_rect):
        current_scroll = self.tabs.currentWidget()
        hits = set()
        for path, section in self._sections_by_path.items():
            if not section.isVisible():
                continue
            if current_scroll is not None and not current_scroll.isAncestorOf(section):
                continue
            top_left = section.header.mapToGlobal(QPoint(0, 0))
            rect = QRect(top_left, section.header.size())
            if rect.intersects(global_rect):
                hits.add(path)
        return hits

    # -- right-click context menu on a folder header -------------------------
    def show_folder_context_menu(self, origin_section, global_pos):
        selected_paths = self.selected_folder_paths()
        if not selected_paths:
            return
        menu = QMenu(origin_section)
        n = len(selected_paths)
        label = (
            "Load This Folder (Full, Recursively)"
            if n == 1 else
            f"Load {n} Selected Folders (Full, Recursively)"
        )
        load_action = menu.addAction(label)
        load_action.triggered.connect(lambda: self.start_bulk_load(list(selected_paths)))
        menu.addSeparator()
        clear_action = menu.addAction("Clear Selection")
        clear_action.triggered.connect(self.clear_folder_selection)
        menu.exec(global_pos)

    # -- "Load Selected Folders" bulk background load -----------------------
    def start_bulk_load(self, paths):
        self.bulk_loader.start(paths)

    def _on_bulk_load_started(self):
        self.bulk_overlay.show_overlay()

    def _on_bulk_load_status(self, done, total, text):
        self.bulk_overlay.set_progress(done, total, text)

    def _on_bulk_load_finished(self):
        # Leave the summary text ("Loaded N folders, M images...") up for a
        # few seconds so the user actually sees it, then quietly dismiss
        # the overlay.
        QTimer.singleShot(2500, self._hide_bulk_status_if_idle)

    def _hide_bulk_status_if_idle(self):
        if not self.bulk_loader.active:
            self.bulk_overlay.hide_overlay()

    # -- (re)building tabs from Settings -----------------------------------
    def reload_folders(self):
        prev_active = self._current_tab_path() or self._saved_active_tab
        self.tabs.clear()
        self._top_sections = []
        self._sections_by_path = {}
        self._selected_folder_paths = set()
        # A reload means the configured folder set (or ignore patterns)
        # changed, or the user explicitly asked to restore a hidden
        # folder — either way it should reflect what's on disk *now*, not
        # whatever an earlier scan (interactive or bulk) cached. Any
        # in-flight bulk load is scanning against a tab tree that's about
        # to be discarded anyway, so stop it too.
        self.bulk_loader.cancel()
        _ROUGH_SCAN_CACHE.clear()
        folders = self.main_window.config_data.get("image_selection_folders", [])
        configured_paths = {f.get("path", "") for f in folders}
        self._closed_this_session &= configured_paths

        restore_index = 0
        for folder_cfg in folders:
            path = folder_cfg.get("path", "")
            if path in self._closed_this_session:
                continue

            scroll = _AutopanScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            section = FolderSection(self, path, depth=0, closable=False)
            wrapper = _RubberBandArea(self)
            wrapper_lay = QVBoxLayout(wrapper)
            wrapper_lay.setContentsMargins(12, 12, 12, 12)
            wrapper_lay.addWidget(section)
            wrapper_lay.addStretch(1)
            scroll.setWidget(wrapper)

            name = Path(path).name or path
            self.tabs.addTab(scroll, name)
            self.tabs.setTabToolTip(self.tabs.count() - 1, path)
            self._top_sections.append(section)
            if path == prev_active:
                restore_index = self.tabs.count() - 1
            if path in self.saved_expanded_paths:
                section.set_expanded(True, persist=False)

        if self.tabs.count() > 0:
            self.tabs.setCurrentIndex(restore_index)
            self._trigger_rough_pass_for_tab(restore_index)

        self._update_empty_state()
        self._refresh_restore_hidden_button()

    # -- reload button: re-scan just the current tab's folder --------------
    def reload_current_folder(self):
        path = self._current_tab_path()
        if path:
            self._reload_folder_tab(path)

    def _reload_folder_tab(self, path):
        index = next(
            (i for i, section in enumerate(self._top_sections) if section.path == path),
            None,
        )
        if index is None:
            return

        # Drop cached scan results for this folder and everything nested
        # under it, so the rebuild below reflects what's on disk right
        # now instead of a stale rough-scan cache entry.
        prefix = path.rstrip(os.sep) + os.sep
        for cached_path in [p for p in _ROUGH_SCAN_CACHE if p == path or p.startswith(prefix)]:
            del _ROUGH_SCAN_CACHE[cached_path]
        self._purge_section_registry(path)

        old_section = self._top_sections[index]

        scroll = _AutopanScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        section = FolderSection(self, path, depth=0, closable=False)
        wrapper = _RubberBandArea(self)
        wrapper_lay = QVBoxLayout(wrapper)
        wrapper_lay.setContentsMargins(12, 12, 12, 12)
        wrapper_lay.addWidget(section)
        wrapper_lay.addStretch(1)
        scroll.setWidget(wrapper)

        name = Path(path).name or path
        self.tabs.removeTab(index)
        self.tabs.insertTab(index, scroll, name)
        self.tabs.setTabToolTip(index, path)
        self._top_sections[index] = section
        self.tabs.setCurrentIndex(index)
        old_section.deleteLater()

        if path in self.saved_expanded_paths:
            section.set_expanded(True, persist=False)

        self._trigger_rough_pass_for_tab(index)

    def _update_empty_state(self):
        has_tabs = self.tabs.count() > 0
        self.tabs.setVisible(has_tabs)
        self.empty_hint.setVisible(not has_tabs)
        self.reload_folder_btn.setVisible(has_tabs)
        if not has_tabs:
            if self._closed_this_session:
                self.empty_hint.setText(
                    "All configured folders are hidden for this session.\n"
                    "Click \u21bb above to restore them."
                )
            else:
                self.empty_hint.setText(
                    "No folders configured yet.\n"
                    "Open the \u2699 Settings button (bottom-left of the "
                    "workflow sidebar) \u2192 Image Selection to add one."
                )

    def _refresh_restore_hidden_button(self):
        if not self._closed_this_session:
            self.restore_hidden_btn.hide()
            return
        self.restore_hidden_btn.setToolTip(
            f"{len(self._closed_this_session)} folder(s) hidden for this session \u2014 click to restore"
        )
        menu = QMenu(self.restore_hidden_btn)
        for path in sorted(self._closed_this_session, key=str.lower):
            name = Path(path).name or path
            action = menu.addAction(name)
            action.setToolTip(path)
            action.triggered.connect(lambda checked=False, p=path: self._restore_closed_folder(p))
        menu.addSeparator()
        restore_all = menu.addAction("Restore all")
        restore_all.triggered.connect(self._restore_all_closed_folders)
        self.restore_hidden_btn.setMenu(menu)
        self.restore_hidden_btn.show()

    def _restore_closed_folder(self, path):
        self._closed_this_session.discard(path)
        self.reload_folders()

    def _restore_all_closed_folders(self):
        self._closed_this_session.clear()
        self.reload_folders()

    def _trigger_rough_pass_for_tab(self, index):
        if 0 <= index < len(self._top_sections):
            self._top_sections[index].ensure_rough_pass()

    def close_tab_for_session(self, path):
        self._closed_this_session.add(path)
        for i, section in enumerate(self._top_sections):
            if section.path == path:
                self.tabs.removeTab(i)
                del self._top_sections[i]
                self._purge_section_registry(path)
                section.deleteLater()
                break
        self._update_empty_state()
        self._refresh_restore_hidden_button()

    def _purge_section_registry(self, root_path):
        """Drop `root_path` and everything nested under it from the folder
        registry/multi-selection when its tab is closed for the session, so
        a stale reference to an now-orphaned FolderSection can't linger in
        a later rubber-band selection or "Load Selected Folders" context
        menu (it's still on disk, just no longer shown)."""
        prefix = root_path.rstrip(os.sep) + os.sep
        stale = [p for p in self._sections_by_path if p == root_path or p.startswith(prefix)]
        for p in stale:
            del self._sections_by_path[p]
            self._selected_folder_paths.discard(p)

    def note_expanded(self, path, expanded):
        if expanded:
            self.saved_expanded_paths.add(path)
        else:
            self.saved_expanded_paths.discard(path)

    def _current_tab_path(self):
        idx = self.tabs.currentIndex()
        if idx < 0:
            return None
        tooltip = self.tabs.tabToolTip(idx)
        return tooltip or None

    def _on_tab_changed(self, index):
        self._trigger_rough_pass_for_tab(index)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_status_bar()

    def _position_status_bar(self):
        """Float the settings row over the top-right corner of the tab
        strip instead of it occupying a separate row of its own."""
        bar = getattr(self, "_status_bar", None)
        if bar is None:
            return
        bar.adjustSize()
        tab_bar_height = self.tabs.tabBar().sizeHint().height()
        y = max(0, (tab_bar_height - bar.height()) // 2)
        bar.move(max(0, self.width() - bar.width()), y)
        bar.raise_()


# ---------------------------------------------------------------------------
# Bottom bar — the "input roster" (spec section 2.3), plus the run/queue
# controls and optional-parameter form for whichever workflow is active.
# Only this bar changes when the active workflow changes; it never resets
# the Image Browser above it.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Plain QScrollArea that emits a signal on resize, used by RosterBar to know
# exactly when its viewport height changes so the roster icons can be
# rescaled to fill it (see RosterBar._sync_icon_size).
# ---------------------------------------------------------------------------
class _ResizingScrollArea(QScrollArea):
    resized = Signal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


# ---------------------------------------------------------------------------
# Small "workflow is running" indicator for RosterBar. Deliberately not a
# native QProgressBar in indeterminate mode -- that animates far too fast
# and looks like a blocky, jittery marquee at this small a size. Instead a
# single soft pill glides smoothly back and forth along a slim track, at a
# slow, sine-eased pace, driven by a plain QTimer rather than
# QPropertyAnimation so there's no extra Property boilerplate for one
# self-contained decorative widget.
# ---------------------------------------------------------------------------
class _RunIndicator(QWidget):
    PERIOD_MS = 1600   # one full there-and-back glide
    FRAME_MS = 33      # ~30fps -- plenty smooth for a widget this small

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(28, 8)
        self._elapsed_ms = 0.0
        self._phase = 0.0  # 0..1, eased position of the pill along the track
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._elapsed_ms = 0.0
        self._phase = 0.0
        self._timer.start()
        self.update()

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self._elapsed_ms = (self._elapsed_ms + self.FRAME_MS) % self.PERIOD_MS
        frac = self._elapsed_ms / self.PERIOD_MS
        # Smooth sinusoidal ping-pong: 0 at the start, 1 at the midpoint,
        # back to 0 at the end -- no snap, no sudden reversal.
        self._phase = (1 - math.cos(2 * math.pi * frac)) / 2
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        radius = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(r, radius, radius)

        pill_w = max(6.0, r.width() * 0.4)
        travel = r.width() - pill_w
        x = travel * self._phase
        p.setBrush(QColor(0, 212, 160))
        p.drawRoundedRect(QRectF(x, 0, pill_w, r.height()), radius, radius)


class RosterBar(QWidget):
    # Roster icons scale with the bottom bar's height, but stay within a
    # sane range so they never disappear or take over the whole window.
    MIN_ICON_SIZE = 44
    MAX_ICON_SIZE = 180

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setObjectName("rosterBar")
        self.state = None
        self.icons = []
        self.armed_index = None
        self.param_widgets = {}
        self.icon_size = RosterIcon.SIZE

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 10)
        outer.setSpacing(6)

        toolbar = QHBoxLayout()
        roster_label = QLabel("INPUT ROSTER")
        roster_label.setObjectName("hint")
        toolbar.addWidget(roster_label)
        toolbar.addStretch(1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("dangerButton")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        toolbar.addWidget(self.clear_btn)
        self.queue_btn = QPushButton("+ Add to Queue")
        self.queue_btn.clicked.connect(self._on_queue_clicked)
        toolbar.addWidget(self.queue_btn)
        self.run_btn = QPushButton("\u25b6  Run")
        self.run_btn.setObjectName("accentButton")
        self.run_btn.clicked.connect(self._on_run_clicked)
        toolbar.addWidget(self.run_btn)
        outer.addLayout(toolbar)

        self.param_form_widget = QWidget()
        self.param_form = QFormLayout(self.param_form_widget)
        self.param_form.setContentsMargins(0, 4, 0, 4)
        outer.addWidget(self.param_form_widget)
        self.param_form_widget.hide()

        scroll = _ResizingScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # No fixed height any more: the roster row fills whatever vertical
        # space is left in the bar, and its own resize (driven by dragging
        # the center splitter handle) is what drives the icon rescale.
        scroll.setMinimumHeight(self.MIN_ICON_SIZE + 16)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.resized.connect(self._sync_icon_size)
        scroll.resized.connect(self._position_status_label)
        self._roster_scroll = scroll
        self.roster_row_widget = QWidget()
        self.roster_row = QHBoxLayout(self.roster_row_widget)
        self.roster_row.setContentsMargins(2, 2, 2, 2)
        self.roster_row.setSpacing(8)
        self.roster_row.addStretch(1)
        scroll.setWidget(self.roster_row_widget)
        outer.addWidget(scroll, 1)

        # Status text used to sit above the "INPUT ROSTER" header in its
        # own row; it now spans the free strip of space left below the
        # image-input icons instead (they cap out at MAX_ICON_SIZE long
        # before they'd fill a tall bar), right-aligned within it, rather
        # than costing the bar any extra height of its own.
        self.status_label = QLabel("No workflow selected.", scroll.viewport())
        self.status_label.setObjectName("rosterStatusLabel")
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status_label.setWordWrap(False)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.status_label.adjustSize()

        # A small "running" indicator that only appears while the active
        # workflow is executing, sitting right after the status text (which
        # shifts left to make just enough room for it -- see
        # _position_status_label). Deliberately narrow: just wide enough to
        # read as motion/activity, not a full-width progress bar of its own.
        # Custom-painted (see _RunIndicator) rather than a native
        # indeterminate QProgressBar, which animates too fast/jittery at
        # this size.
        self.run_indicator = _RunIndicator(scroll.viewport())
        self.run_indicator.hide()

        self.set_workflow(None)
        self._position_status_label()

    # -- dynamic icon sizing (spec: resizing the bottom bar grows/shrinks
    # the image inputs with it) ------------------------------------------
    def _sync_icon_size(self):
        avail = self._roster_scroll.viewport().height() - 4
        new_size = max(self.MIN_ICON_SIZE, min(self.MAX_ICON_SIZE, avail))
        if new_size == self.icon_size:
            return
        self.icon_size = new_size
        for icon in self.icons:
            icon.set_icon_size(new_size)

    def _position_status_label(self):
        """Keeps the status text pinned along the bottom strip of free
        space left below the image-input icons (they cap out at
        MAX_ICON_SIZE long before filling a tall bar), spanning the full
        width of the roster's viewport with the text right-aligned within
        it -- a plain single line, not a floating button/pill.

        While a run is in progress, the run indicator claims a small slice
        of that same strip on the far right, and the status text's own
        width is shrunk by just enough to make room for it (plus a small
        gap) -- reading as the text sliding left to hand off a bit of
        space, rather than the indicator overlapping it."""
        if not hasattr(self, "status_label"):
            return
        vp = self._roster_scroll.viewport()
        margin = 8
        gap = 6
        height = self.status_label.sizeHint().height()
        y = vp.height() - height - margin
        indicator = getattr(self, "run_indicator", None)
        reserved = (indicator.width() + gap) if (indicator is not None and indicator.isVisible()) else 0
        self.status_label.setGeometry(
            margin, y, max(0, vp.width() - 2 * margin - reserved), height
        )
        self.status_label.raise_()
        if indicator is not None:
            indicator.move(
                vp.width() - margin - indicator.width(),
                y + (height - indicator.height()) // 2,
            )
            indicator.raise_()

    # -- switching the active workflow --------------------------------------
    def set_workflow(self, state):
        if self.state is not None:
            try:
                self.state.statusChanged.disconnect(self._on_status_changed)
                self.state.runStateChanged.disconnect(self._on_run_state_changed)
                self.state.slotsRebuilt.disconnect(self._on_slots_rebuilt)
            except (TypeError, RuntimeError):
                pass

        self.state = state
        self.armed_index = None

        if state is not None:
            state.statusChanged.connect(self._on_status_changed)
            state.runStateChanged.connect(self._on_run_state_changed)
            state.slotsRebuilt.connect(self._on_slots_rebuilt)

        self._rebuild_icons()
        self._rebuild_params()

        self.queue_btn.setEnabled(state is not None)
        self.clear_btn.setEnabled(state is not None)
        if state is None:
            self.run_btn.setEnabled(False)
            self.status_label.setText("No workflow selected. Add one from the workflows sidebar.")
            self.status_label.setProperty("error", False)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
            self._set_running(False)
        else:
            self.run_btn.setEnabled(not state.running)
            self._on_status_changed(state.status_text, state.status_error)
            self._set_running(state.running)

    def _on_slots_rebuilt(self):
        self._rebuild_icons()
        self._rebuild_params()

    def _rebuild_icons(self):
        for icon in self.icons:
            self.roster_row.removeWidget(icon)
            icon.setParent(None)
            icon.deleteLater()
        self.icons = []
        while self.roster_row.count():
            self.roster_row.takeAt(0)

        if self.state is not None:
            for i, slot in enumerate(self.state.slots):
                icon = RosterIcon(i, slot["node_id"], slot["caption"], size=self.icon_size)
                icon.filepath = slot["filepath"]
                if icon.filepath:
                    pix = QPixmap(icon.filepath)
                    if not pix.isNull():
                        icon._pixmap = pix
                icon._render()
                icon.armToggled.connect(self._on_arm_toggled)
                icon.reorderRequested.connect(self._on_reorder)
                icon.imageSwapRequested.connect(self._on_swap)
                icon.changed.connect(lambda idx=i: self._on_icon_changed(idx))
                self.roster_row.addWidget(icon)
                self.icons.append(icon)
        self.roster_row.addStretch(1)

    def _rebuild_params(self):
        while self.param_form.rowCount():
            self.param_form.removeRow(0)
        self.param_widgets = {}
        editable = self.state.editable_params() if self.state is not None else {}
        if not editable:
            self.param_form_widget.hide()
            return
        for key, (default_value, type_name) in editable.items():
            value = self.state.param_values.get(key, default_value)
            if type_name == "int":
                w = QSpinBox()
                w.setRange(-2_147_483_648, 2_147_483_647)
                w.setValue(int(value))
                w.valueChanged.connect(lambda v, k=key: self._on_param_changed(k, v))
            elif type_name == "float":
                w = QDoubleSpinBox()
                w.setRange(-1e12, 1e12)
                w.setDecimals(4)
                w.setValue(float(value))
                w.valueChanged.connect(lambda v, k=key: self._on_param_changed(k, v))
            else:
                w = QLineEdit(str(value))
                w.textChanged.connect(lambda v, k=key: self._on_param_changed(k, v))
            self.param_form.addRow(key, w)
            self.param_widgets[key] = w
        self.param_form_widget.show()

    def _on_param_changed(self, key, value):
        if self.state is not None:
            self.state.param_values[key] = value
            self.main_window.persist_all()

    # -- roster interactions -------------------------------------------
    def _on_arm_toggled(self, index):
        self.armed_index = None if self.armed_index == index else index
        for icon in self.icons:
            icon.set_armed(icon.index == self.armed_index)

    def assign_armed(self, filepath):
        """Primary selection flow (spec 3.6): called when a thumbnail is
        clicked in the Image Browser while a roster slot is armed. Assigns
        the image, then auto-advances the armed state to the next empty
        slot so filling several inputs back-to-back doesn't need re-arming
        after every click."""
        if self.armed_index is None or self.armed_index >= len(self.icons):
            return
        self.icons[self.armed_index].set_image_path(filepath)
        next_empty = next(
            (i for i, icon in enumerate(self.icons) if not icon.filepath), None
        )
        self.armed_index = next_empty
        for icon in self.icons:
            icon.set_armed(icon.index == self.armed_index)

    def assign_to_slot(self, index, filepath):
        """Direct assignment (spec: right-click an image -> 'Add to <input
        name>'), independent of the arm/click flow -- assigns straight into
        the given roster slot regardless of what's currently armed."""
        if index < 0 or index >= len(self.icons):
            return
        self.icons[index].set_image_path(filepath)

    def _on_icon_changed(self, index):
        if self.state is None or index >= len(self.state.slots):
            return
        self.state.slots[index]["filepath"] = self.icons[index].filepath
        self.main_window.persist_all()

    def _on_reorder(self, src, tgt):
        if self.state is not None:
            self.state.reorder_slot(src, tgt)

    def _on_swap(self, src, tgt):
        if self.state is None:
            return
        self.state.swap_slot_images(src, tgt)
        a, b = self.icons[src], self.icons[tgt]
        a.filepath, b.filepath = b.filepath, a.filepath
        a._pixmap, b._pixmap = b._pixmap, a._pixmap
        a._render()
        b._render()

    # -- run / queue / clear ---------------------------------------------
    def _on_run_clicked(self):
        if self.state is not None:
            self.state.run_now()

    def _on_queue_clicked(self):
        if self.state is not None:
            self.state.add_to_queue()

    def _on_clear_clicked(self):
        """Spec 3.7: resets every filled roster slot for the active
        workflow back to empty. Never touches the Image Browser's own
        state (open tabs/headers, scroll position)."""
        if self.state is None:
            return
        for icon in self.icons:
            if icon.filepath:
                icon.clear_image()
        self.armed_index = None
        for icon in self.icons:
            icon.set_armed(False)

    def _on_status_changed(self, text, error):
        self.status_label.setText(text)
        self.status_label.setProperty("error", bool(error))
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._position_status_label()

    def _on_run_state_changed(self, running):
        self.run_btn.setEnabled(not running)
        self._set_running(running)

    def _set_running(self, running):
        """Shows/hides the small run indicator next to the status text
        (see _position_status_label) and immediately repositions both."""
        self.run_indicator.setVisible(running)
        if running:
            self.run_indicator.start()
        else:
            self.run_indicator.stop()
        self._position_status_label()


# ---------------------------------------------------------------------------
# New / edit workflow tab dialog
# ---------------------------------------------------------------------------
class WorkflowConfigDialog(QDialog):
    def __init__(self, parent, mode="create", tab=None):
        super().__init__(parent)
        self.setWindowTitle("New Workflow" if mode == "create" else "Edit Workflow")
        self.mode = mode
        self.tab = tab
        self.result = None
        self.workflow_path = tab.workflow_path if tab else None
        self.optional_identifier = tab.optional_identifier if tab else ""
        self._preview_workflow = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("ComfyUI workflow (.json, API format):"))
        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(self.workflow_path or "")
        self.path_edit.setReadOnly(True)
        path_row.addWidget(self.path_edit, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self.detected_lbl = QLabel("")
        self.detected_lbl.setWordWrap(True)
        layout.addWidget(self.detected_lbl)

        layout.addWidget(QLabel("Optional node (ID or title) for extra fields:"))
        self.identifier_edit = QLineEdit(self.optional_identifier)
        layout.addWidget(self.identifier_edit)
        hint = QLabel("Leave empty if you don't need extra parameters.")
        hint.setObjectName("hint")
        layout.addWidget(hint)

        btns = QHBoxLayout()
        btns.addStretch(1)
        if mode == "edit":
            del_btn = QPushButton("Delete Tab")
            del_btn.setObjectName("dangerButton")
            del_btn.clicked.connect(self._delete)
            btns.addWidget(del_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._cancel)
        btns.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("accentButton")
        save_btn.clicked.connect(self._save)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

        if self.workflow_path:
            self._validate_path(self.workflow_path)
        self.setMinimumWidth(420)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select ComfyUI workflow (API format)", "", "JSON (*.json)")
        if path:
            self.path_edit.setText(path)
            self._validate_path(path)

    def _validate_path(self, path):
        try:
            wf = load_workflow_file(path)
            self._preview_workflow = wf
            n_images = len(find_load_image_nodes(wf))
            self.detected_lbl.setText(f"\u2713 Valid workflow \u2014 {n_images} image input node(s) found.")
            self.detected_lbl.setStyleSheet("color:#5fd07c;")
        except Exception as e:
            self._preview_workflow = None
            self.detected_lbl.setText(f"\u26a0 {e}")
            self.detected_lbl.setStyleSheet("color:rgba(220,140,140,0.9);")

    def _delete(self):
        if QMessageBox.question(self, "Delete tab", f"Delete tab '{self.tab.name}'? This cannot be undone.") == QMessageBox.Yes:
            self.result = "delete"
            self.accept()

    def _cancel(self):
        self.result = "cancel"
        self.reject()

    def _save(self):
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "Missing workflow", "Please select a workflow .json file.")
            return
        if self._preview_workflow is None:
            self._validate_path(path)
            if self._preview_workflow is None:
                return
        self.workflow_path = path
        self.optional_identifier = self.identifier_edit.text().strip()
        self.result = "save"
        self.accept()


# ---------------------------------------------------------------------------
# Queue manager (global; not tied to any single tab)
# ---------------------------------------------------------------------------
class QueueManager(QObject):
    queueChanged = Signal()
    itemStarted = Signal(str)
    itemFinished = Signal(str, bool, str)   # id, success, message

    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.items = []
        self.running = False
        self._thread = None
        self._worker = None

    def add_item(self, item):
        item["status"] = "Waiting"
        self.items.append(item)
        self.queueChanged.emit()

    def remove_item(self, item_id):
        self.items = [i for i in self.items if i["id"] != item_id]
        self.queueChanged.emit()

    def clear(self):
        if self.running:
            QMessageBox.information(self.main_window, "Queue running", "Wait for the current run to finish first.")
            return
        self.items = []
        self.queueChanged.emit()

    def run_queue(self):
        if self.running or not self.items:
            return
        self.running = True
        self._run_next()

    def _run_next(self):
        pending = [i for i in self.items if i["status"] in ("Waiting", "Error")]
        if not pending:
            self.running = False
            self.queueChanged.emit()
            return
        item = pending[0]
        item["status"] = "Running"
        self.queueChanged.emit()
        self.itemStarted.emit(item["id"])

        thread = QThread()
        worker = RunWorker(
            item["server"], item["raw_workflow"], item["image_map"],
            item["optional_node_id"], item["param_values"],
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Keep references alive until Qt tells us the thread has actually
        # stopped (thread.quit() only *requests* a stop, it doesn't block).
        self._thread = thread
        self._worker = worker

        def on_finished(item=item):
            item["status"] = "Done"
            self.itemFinished.emit(item["id"], True, "Done")
            self.items.remove(item)
            self.queueChanged.emit()

        def on_error(message, item=item):
            item["status"] = "Error"
            self.itemFinished.emit(item["id"], False, message)
            self.queueChanged.emit()

        def on_thread_finished():
            # Runs only once the worker thread has fully stopped, so it's
            # safe to drop our references and start the next queue item.
            worker.deleteLater()
            thread.deleteLater()
            if self._thread is thread:
                self._thread = None
                self._worker = None
            self._run_next()

        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(on_thread_finished)
        thread.start()


# ---------------------------------------------------------------------------
# Outputs / Queue tab — one panel, two switchable modes (no separate window)
# ---------------------------------------------------------------------------
class OutputsTab(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # -- header: ONLY the Outputs/Queue mode toggle. The contextual
        # action buttons (Refresh/Open Folder/Clear All, Run Queue/Clear)
        # used to live in this same row and would truncate their labels
        # whenever the sidebar was narrow; they now live in a dedicated
        # footer row at the bottom instead (see below), so this header
        # never has more than two short labels to fit. --------------------
        header = QHBoxLayout()
        self.outputs_mode_btn = QPushButton("Outputs")
        self.outputs_mode_btn.setObjectName("modeToggle")
        self.outputs_mode_btn.setCheckable(True)
        self.outputs_mode_btn.setChecked(True)
        self.outputs_mode_btn.clicked.connect(lambda: self._set_mode(0))
        header.addWidget(self.outputs_mode_btn, 1)

        self.queue_mode_btn = QPushButton("Queue")
        self.queue_mode_btn.setObjectName("modeToggle")
        self.queue_mode_btn.setCheckable(True)
        self.queue_mode_btn.clicked.connect(lambda: self._set_mode(1))
        header.addWidget(self.queue_mode_btn, 1)

        # Queue count badge, sidebar-open position: sits just to the right
        # of the "Queue" tab label. Mirrored by MainWindow.queue_badge_
        # collapsed for when the sidebar is closed (see MainWindow).
        self.queue_badge = _QueueCountBadge()
        header.addWidget(self.queue_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        layout.addLayout(header)

        # -- stacked content --------------------------------------------
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self.outputs_list = QListWidget()
        self.outputs_list.setViewMode(QListWidget.IconMode)
        self.outputs_list.setIconSize(QSize(140, 140))
        self.outputs_list.setResizeMode(QListWidget.Adjust)
        self.outputs_list.setMovement(QListWidget.Static)
        self.outputs_list.setSpacing(10)
        self.outputs_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.outputs_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.outputs_list.itemDoubleClicked.connect(self._open_item)
        self.stack.addWidget(self.outputs_list)

        self.queue_list = QListWidget()
        self.queue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.queue_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stack.addWidget(self.queue_list)

        # -- footer: contextual actions for whichever mode is active.
        # Stacked full-width (instead of crammed into one horizontal row)
        # so a button's label is never clipped, no matter how narrow the
        # sidebar is dragged. Outputs-mode gets one row of two + a full-
        # width danger row; Queue-mode gets a full-width Run + a Clear. ---
        footer = QWidget()
        footer.setObjectName("outputsFooter")
        footer_lay = QVBoxLayout(footer)
        footer_lay.setContentsMargins(0, 8, 0, 0)
        footer_lay.setSpacing(6)

        # Outputs-mode actions
        outputs_row = QHBoxLayout()
        outputs_row.setSpacing(6)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        outputs_row.addWidget(self.refresh_btn)
        self.open_folder_btn = QPushButton("Open Folder")
        self.open_folder_btn.clicked.connect(self._open_folder)
        outputs_row.addWidget(self.open_folder_btn)
        self._outputs_row_wrap = QWidget()
        self._outputs_row_wrap.setLayout(outputs_row)
        footer_lay.addWidget(self._outputs_row_wrap)

        self.clear_outputs_btn = QPushButton("Clear All")
        self.clear_outputs_btn.setObjectName("dangerButton")
        self.clear_outputs_btn.clicked.connect(self._clear_all)
        footer_lay.addWidget(self.clear_outputs_btn)

        # Queue-mode actions
        self.run_queue_btn = QPushButton("\u25b6  Run Queue")
        self.run_queue_btn.setObjectName("accentButton")
        self.run_queue_btn.clicked.connect(main_window.queue_manager.run_queue)
        footer_lay.addWidget(self.run_queue_btn)
        self.clear_queue_btn = QPushButton("Clear")
        self.clear_queue_btn.setObjectName("dangerButton")
        self.clear_queue_btn.clicked.connect(main_window.queue_manager.clear)
        footer_lay.addWidget(self.clear_queue_btn)

        layout.addWidget(footer)

        main_window.queue_manager.queueChanged.connect(self._refresh_queue)
        main_window.queue_manager.queueChanged.connect(self._update_badge)
        self._set_mode(0)
        self.refresh()
        self._refresh_queue()
        self._update_badge()

    # -- mode switching ---------------------------------------------------
    def _set_mode(self, mode):
        """mode 0 = Outputs, 1 = Queue."""
        self.outputs_mode_btn.setChecked(mode == 0)
        self.queue_mode_btn.setChecked(mode == 1)
        self.stack.setCurrentIndex(mode)
        self._outputs_row_wrap.setVisible(mode == 0)
        self.clear_outputs_btn.setVisible(mode == 0)
        for w in (self.run_queue_btn, self.clear_queue_btn):
            w.setVisible(mode == 1)

    # -- outputs -----------------------------------------------------------
    def _output_dir(self):
        d = Path(self.main_window.output_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def refresh(self):
        self.outputs_list.clear()
        d = self._output_dir()
        files = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            pix = QPixmap(str(f))
            item = QListWidgetItem(QIcon(pix), f.name)
            item.setData(Qt.UserRole, str(f))
            self.outputs_list.addItem(item)

    def _open_item(self, item):
        path = item.data(Qt.UserRole)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_dir())))

    def _clear_all(self):
        d = self._output_dir()
        files = list(d.glob("*.png"))
        if not files:
            return
        if QMessageBox.question(
            self, "Clear all outputs", f"Delete all {len(files)} saved output image(s)? This cannot be undone."
        ) != QMessageBox.Yes:
            return
        for f in files:
            try:
                f.unlink()
            except Exception:
                pass
        self.refresh()

    # -- queue ---------------------------------------------------------
    def _refresh_queue(self):
        self.queue_list.clear()
        for item in self.main_window.queue_manager.items:
            text = f"[{item['status']}]  {item['tab_name']}"
            self.queue_list.addItem(QListWidgetItem(text))

    def _update_badge(self):
        self.queue_badge.set_count(len(self.main_window.queue_manager.items))


# ---------------------------------------------------------------------------
# Settings dialog (server address, output path, folder configuration)
# opened from the settings icon in the top-right corner of the window
# ---------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self.resize(620, 720)

        # The whole settings menu scrolls as one unit (form fields, folder
        # tables, and the Close/Save row all live inside), but the
        # scrollbar itself stays hidden -- the mouse wheel / trackpad still
        # scrolls it fine via QScrollArea's default wheel handling, it's
        # just not drawn. Hotkeys now live in their own dialog (see
        # HotkeysDialog) reachable from the "?" button in the workflow
        # sidebar.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)

        form = QFormLayout()
        self.server_edit = QLineEdit(main_window.server)
        form.addRow("ComfyUI server address:", self.server_edit)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(main_window.output_dir)
        out_row.addWidget(self.output_edit, 1)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(browse_btn)
        out_wrap = QWidget()
        out_wrap.setLayout(out_row)
        form.addRow("Output folder:", out_wrap)
        layout.addLayout(form)

        out_hint = QLabel(
            "The app doesn't save images itself -- your workflow's own "
            "Save Image node is what writes the file to disk. Point this "
            "at that same folder so the app can list and preview them here "
            "in the Outputs sidebar."
        )
        out_hint.setObjectName("hint")
        out_hint.setWordWrap(True)
        layout.addWidget(out_hint)

        # -- Image Selection (spec section 4) --------------------------
        img_title = QLabel("Image Selection")
        img_title.setObjectName("sectionTitle")
        layout.addWidget(img_title)

        self.folders = [dict(f) for f in main_window.config_data.get("image_selection_folders", [])]

        self.folder_table = QTableWidget(0, 2)
        self.folder_table.setHorizontalHeaderLabels(["Folder", ""])
        self.folder_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.folder_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.folder_table.verticalHeader().hide()
        self.folder_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.folder_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.folder_table.setMaximumHeight(150)
        layout.addWidget(self.folder_table)

        folder_btns = QHBoxLayout()
        add_folder_btn = QPushButton("Add Folder\u2026")
        add_folder_btn.clicked.connect(self._add_folder)
        folder_btns.addWidget(add_folder_btn)
        folder_btns.addStretch(1)
        up_btn = QPushButton("\u2191 Move Up")
        up_btn.clicked.connect(lambda: self._move_folder(-1))
        folder_btns.addWidget(up_btn)
        down_btn = QPushButton("\u2193 Move Down")
        down_btn.clicked.connect(lambda: self._move_folder(1))
        folder_btns.addWidget(down_btn)
        layout.addLayout(folder_btns)

        folder_hint = QLabel(
            "Order here sets the order of tabs in the Image Browser. Every folder\n"
            "is always searched fully recursively, however deeply nested it is."
        )
        folder_hint.setObjectName("hint")
        folder_hint.setWordWrap(True)
        layout.addWidget(folder_hint)

        # Soft warning only (spec section 9, resolved) — never blocks adding
        # more folders, just flags when things may get hard to navigate.
        self.folder_warning_lbl = QLabel("")
        self.folder_warning_lbl.setObjectName("hint")
        self.folder_warning_lbl.setProperty("state", "warning")
        self.folder_warning_lbl.setWordWrap(True)
        self.folder_warning_lbl.hide()
        layout.addWidget(self.folder_warning_lbl)

        self._refresh_folder_table()

        # -- Ignored folder names ---------------------------------------
        ignore_title = QLabel("Ignored Folder Names")
        ignore_title.setObjectName("sectionTitle")
        layout.addWidget(ignore_title)

        self.ignore_patterns = [
            dict(p) for p in main_window.config_data.get("ignore_folder_patterns", [])
        ]

        self.ignore_table = QTableWidget(0, 3)
        self.ignore_table.setHorizontalHeaderLabels(["Folder name", "Match", ""])
        self.ignore_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.ignore_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.ignore_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.ignore_table.verticalHeader().hide()
        self.ignore_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.ignore_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.ignore_table.setMaximumHeight(140)
        layout.addWidget(self.ignore_table)

        add_ignore_row = QHBoxLayout()
        self.ignore_pattern_edit = QLineEdit()
        self.ignore_pattern_edit.setPlaceholderText("Folder name, e.g. \"cache\"")
        add_ignore_row.addWidget(self.ignore_pattern_edit, 1)
        self.ignore_mode_combo = QComboBox()
        self.ignore_mode_combo.addItem("Starts with", "starts_with")
        self.ignore_mode_combo.addItem("Contains", "contains")
        add_ignore_row.addWidget(self.ignore_mode_combo)
        add_ignore_btn = QPushButton("Add\u2026")
        add_ignore_btn.clicked.connect(self._add_ignore_pattern)
        add_ignore_row.addWidget(add_ignore_btn)
        layout.addLayout(add_ignore_row)

        ignore_hint = QLabel(
            "Any folder whose name starts with, or contains, one of these is skipped\n"
            "entirely while scanning \u2014 it and everything inside it is never shown."
        )
        ignore_hint.setObjectName("hint")
        ignore_hint.setWordWrap(True)
        layout.addWidget(ignore_hint)

        self._refresh_ignore_table()

        layout.addStretch(1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(close_btn)
        save_btn = QPushButton("Save")
        save_btn.setObjectName("accentButton")
        save_btn.clicked.connect(self._save)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Select output folder", self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    # -- Image Selection folders -----------------------------------------
    def _refresh_folder_table(self):
        self.folder_table.setRowCount(len(self.folders))
        for row, f in enumerate(self.folders):
            path_item = QTableWidgetItem(f.get("path", ""))
            path_item.setToolTip(f.get("path", ""))
            self.folder_table.setItem(row, 0, path_item)

            remove_btn = QToolButton()
            remove_btn.setObjectName("iconButton")
            remove_btn.setText("\u00d7")
            remove_btn.setToolTip("Remove folder")
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.clicked.connect(lambda _, r=row: self._remove_folder(r))
            self.folder_table.setCellWidget(row, 1, remove_btn)

        if len(self.folders) > FOLDER_COUNT_WARN:
            self.folder_warning_lbl.setText(
                f"\u26a0 {len(self.folders)} folders configured \u2014 more than "
                f"~{FOLDER_COUNT_WARN} can get hard to navigate as tabs. Still fully "
                f"functional, just a heads-up."
            )
            self.folder_warning_lbl.show()
        else:
            self.folder_warning_lbl.hide()

    def _add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select image folder")
        if not path:
            return
        if any(f.get("path") == path for f in self.folders):
            return
        self.folders.append({"path": path})
        self._refresh_folder_table()

    def _remove_folder(self, row):
        if 0 <= row < len(self.folders):
            del self.folders[row]
            self._refresh_folder_table()

    def _move_folder(self, delta):
        row = self.folder_table.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if 0 <= new_row < len(self.folders):
            self.folders[row], self.folders[new_row] = self.folders[new_row], self.folders[row]
            self._refresh_folder_table()
            self.folder_table.selectRow(new_row)

    # -- Ignored folder names ----------------------------------------------
    def _refresh_ignore_table(self):
        self.ignore_table.setRowCount(len(self.ignore_patterns))
        for row, rule in enumerate(self.ignore_patterns):
            pattern_item = QTableWidgetItem(rule.get("pattern", ""))
            self.ignore_table.setItem(row, 0, pattern_item)

            mode_wrap = QWidget()
            mode_lay = QHBoxLayout(mode_wrap)
            mode_lay.setContentsMargins(0, 0, 0, 0)
            mode_lay.setAlignment(Qt.AlignCenter)
            combo = QComboBox()
            combo.addItem("Starts with", "starts_with")
            combo.addItem("Contains", "contains")
            idx = combo.findData(rule.get("mode", "starts_with"))
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(lambda _i, r=row, c=combo: self._set_ignore_mode(r, c))
            mode_lay.addWidget(combo)
            self.ignore_table.setCellWidget(row, 1, mode_wrap)

            remove_btn = QToolButton()
            remove_btn.setObjectName("iconButton")
            remove_btn.setText("\u00d7")
            remove_btn.setToolTip("Remove")
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.clicked.connect(lambda _, r=row: self._remove_ignore_pattern(r))
            self.ignore_table.setCellWidget(row, 2, remove_btn)

    def _add_ignore_pattern(self):
        pattern = self.ignore_pattern_edit.text().strip()
        if not pattern:
            return
        mode = self.ignore_mode_combo.currentData()
        if any(p.get("pattern") == pattern and p.get("mode") == mode for p in self.ignore_patterns):
            self.ignore_pattern_edit.clear()
            return
        self.ignore_patterns.append({"pattern": pattern, "mode": mode})
        self.ignore_pattern_edit.clear()
        self._refresh_ignore_table()

    def _remove_ignore_pattern(self, row):
        if 0 <= row < len(self.ignore_patterns):
            del self.ignore_patterns[row]
            self._refresh_ignore_table()

    def _set_ignore_mode(self, row, combo):
        if 0 <= row < len(self.ignore_patterns):
            self.ignore_patterns[row]["mode"] = combo.currentData()

    def _save(self):
        self.main_window.server = self.server_edit.text().strip() or DEFAULT_SERVER
        self.main_window.output_dir = self.output_edit.text().strip() or DEFAULT_OUTPUT_DIR
        self.main_window.config_data["image_selection_folders"] = self.folders
        self.main_window.config_data["ignore_folder_patterns"] = self.ignore_patterns
        self.main_window.persist_all()
        self.main_window.image_browser.reload_folders()
        self.accept()


# ---------------------------------------------------------------------------
# Hotkeys dialog — split out of Settings so it's reachable on its own via
# the "?" button in the workflow sidebar. Unlike Settings, there's nothing
# here to edit, so there's no outer QScrollArea wrapping the whole content:
# the hotkey table itself is the only thing that scrolls.
# ---------------------------------------------------------------------------
class HotkeysDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.setWindowTitle("Hotkeys")
        self.setMinimumWidth(480)
        self.resize(520, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Hotkeys")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        table = QTableWidget(len(HOTKEYS), 2)
        table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        for row, (seq, desc) in enumerate(HOTKEYS):
            table.setItem(row, 0, QTableWidgetItem(seq))
            table.setItem(row, 1, QTableWidgetItem(desc))
        layout.addWidget(table, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(close_btn)
        layout.addLayout(btns)


# ---------------------------------------------------------------------------
# Window chrome — vael.'s shared frameless title bar (copied from vael.
# indexer so every app in the vael. product line looks/behaves the same).
# ---------------------------------------------------------------------------
def _make_gear_icon(color: str = "#b4b4b4", size: int = 14) -> QIcon:
    """Hand-draw a small, simple line-art gear/cog icon for the Settings
    button, replacing the old emoji glyph so it matches the rest of the
    app's minimal, monochrome icon style."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.3)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx = cy = size / 2
    r_outer = size / 2 - 1.6
    r_inner = size / 2 - 3.8
    hole_r = size * 0.16
    p.drawEllipse(QPointF(cx, cy), hole_r, hole_r)
    p.drawEllipse(QPointF(cx, cy), r_inner, r_inner)
    teeth = 6
    for i in range(teeth):
        angle = (2 * math.pi / teeth) * i
        x1 = cx + r_inner * math.cos(angle)
        y1 = cy + r_inner * math.sin(angle)
        x2 = cx + r_outer * math.cos(angle)
        y2 = cy + r_outer * math.sin(angle)
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    p.end()
    return QIcon(pm)


def _make_win_icon(kind: str, color: str = "#b4b4b4", size: int = 10) -> QIcon:
    """Hand-draw a small crisp icon for the maximize/restore window button."""
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
    frameless chrome used by vael.'s other apps (e.g. vael. indexer).
    Dragging is delegated to the OS via QWindow.startSystemMove() so
    Snap zones / Snap Assist / Win+Arrow keep working.
    """

    def __init__(self, window: "MainWindow", parent=None):
        super().__init__(parent)
        self._window = window
        self._drag_offset = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            wh = self._window.windowHandle()
            started = bool(wh is not None and wh.startSystemMove())
            if not started:
                self._drag_offset = (
                    event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
                )
            event.accept()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            if self._window.isMaximized():
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


# ---------------------------------------------------------------------------
# Center container — holds the persistent Image Browser + the roster bar.
# Notifies MainWindow when resized so both overlay sidebars (workflows on
# the left, outputs/queue on the right) can be repositioned to match.
# ---------------------------------------------------------------------------
class _CenterSplitterHandle(QSplitterHandle):
    """Replaces the old thick, fully-colored splitter bar between the
    Image Browser and the Input Roster with a slim, mostly-invisible
    strip that only shows three short horizontal "grip" lines in the
    middle -- and only responds to a drag that actually starts on that
    small grip area, so the rest of the handle's width doesn't
    accidentally resize things."""

    GRIP_W = 26
    GRIP_HIT_W = 40
    GRIP_HIT_H = 14

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        # The cast shadow that used to sit on the roster bar itself now
        # lives on the drag handle instead, blurring down into the
        # roster below -- it marks the actual boundary the user drags to
        # resize, rather than reading as a stray bar of its own.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 200))
        self.setGraphicsEffect(shadow)

    def _grip_rect(self):
        r = self.rect()
        return QRect(
            r.center().x() - self.GRIP_HIT_W // 2,
            r.center().y() - self.GRIP_HIT_H // 2,
            self.GRIP_HIT_W, self.GRIP_HIT_H,
        )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        hovered = self._grip_rect().contains(self.mapFromGlobal(self.cursor().pos()))
        color = QColor("#00d4a0") if hovered else QColor(255, 255, 255, 90)
        pen = QPen(color)
        pen.setWidth(1)
        p.setPen(pen)
        cx, cy = self.rect().center().x(), self.rect().center().y()
        half = self.GRIP_W // 2
        for dy in (-3, 0, 3):
            p.drawLine(cx - half, cy + dy, cx + half, cy + dy)
        p.end()

    def mousePressEvent(self, event):
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self._grip_rect().contains(pos):
            super().mousePressEvent(event)
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        self.update()
        super().mouseMoveEvent(event)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


class _CenterSplitter(QSplitter):
    """Vertical splitter between the Image Browser and Input Roster, using
    _CenterSplitterHandle instead of the default full-width drag bar."""

    def createHandle(self):
        return _CenterSplitterHandle(self.orientation(), self)


class CenterContainer(QWidget):
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.main_window._position_sidebar()
        self.main_window._position_workflow_sidebar()


# ---------------------------------------------------------------------------
# Draggable handle used to resize an overlay sidebar. `sign` controls which
# drag direction grows the sidebar: -1 for a sidebar docked to the right
# edge (handle on its left, dragging left grows it), +1 for a sidebar
# docked to the left edge (handle on its right, dragging right grows it).
# ---------------------------------------------------------------------------
class _SidebarHandle(QWidget):
    def __init__(self, sidebar, sign=-1, parent=None):
        super().__init__(parent)
        self.sidebar = sidebar
        self.sign = sign
        self.setObjectName("sidebarHandle")
        self.setFixedWidth(5)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._dragging = False
        self._start_x = 0
        self._start_width = 0

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._start_x = event.globalPosition().toPoint().x()
            self._start_width = self.sidebar.width()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            dx = self.sign * (self._start_x - event.globalPosition().toPoint().x())
            self.sidebar.set_width(self._start_width + dx, persist=False)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.sidebar.persist_width()
            event.accept()


# ---------------------------------------------------------------------------
# Outputs / Queue sidebar — the right-hand counterpart to the workflow
# sidebar (spec 2.2). Functionally unchanged from before the revamp: it
# slides in/out over the center content, is draggable in width (remembered
# across launches), and is closed by default.
# ---------------------------------------------------------------------------
class OutputsSidebar(QWidget):
    MIN_WIDTH = 260
    MAX_WIDTH = 640

    def __init__(self, main_window):
        super().__init__(main_window.center_container)
        self.main_window = main_window
        self.setObjectName("outputsSidebar")
        self._width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(
            main_window.config_data.get("sidebar_width", 340)
        )))
        self._open = False
        self._anim = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.handle = _SidebarHandle(self, sign=-1)
        layout.addWidget(self.handle)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        self.outputs_panel = OutputsTab(main_window)
        content_layout.addWidget(self.outputs_panel)
        layout.addWidget(content, 1)

        # See matching comment in WorkflowSidebar.__init__ -- this one
        # floats over the content too, mirrored to its left edge.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(-6, 0)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.setGraphicsEffect(shadow)

        self.resize(self._width, self.height())
        self.hide()

    # -- width ------------------------------------------------------------
    def set_width(self, width, persist=True):
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(width)))
        if width == self._width:
            return
        self._width = width
        self.main_window._position_sidebar()
        if persist:
            self.persist_width()

    def persist_width(self):
        self.main_window.config_data["sidebar_width"] = self._width
        self.main_window.persist_all()

    # -- open / close -------------------------------------------------------
    def is_open(self):
        return self._open

    def toggle(self):
        self.set_open(not self._open)

    def set_open(self, open_):
        if open_ == self._open:
            return
        self._open = open_
        container = self.main_window.center_container
        start_rect = self.geometry()
        if open_:
            self.show()
            self.raise_()
            # Runs flush to the right edge -- no reserved gap for the edge
            # tab pill, which stays on top via raise_() below instead of
            # needing empty space carved out for it. A reserved gap there
            # let whatever sits behind the sidebar (the tab strip) show
            # through at that edge even while the sidebar was "open".
            end_rect = QRect(
                max(0, container.width() - self._width), 0,
                self._width, container.height(),
            )
        else:
            end_rect = QRect(container.width(), 0, self._width, container.height())

        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(180)
        anim.setStartValue(start_rect)
        anim.setEndValue(end_rect)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if not open_:
            anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim  # keep a reference alive for the duration
        edge = getattr(self.main_window, "outputs_edge_tab", None)
        if edge is not None:
            edge.raise_()
            edge.set_open_state(open_)


# ---------------------------------------------------------------------------
# Left sidebar — workflow switcher (spec 2.1). Replaces the old top tab
# bar entirely: this list is what makes a workflow "active", drag-reorders
# workflows, and is where "Add workflow" now lives. Mirrors OutputsSidebar's
# slide animation with the anchor edge flipped, and its handle reversed.
# ---------------------------------------------------------------------------
class WorkflowSidebar(QWidget):
    MIN_WIDTH = 200
    MAX_WIDTH = 460

    def __init__(self, main_window):
        super().__init__(main_window.center_container)
        self.main_window = main_window
        self.setObjectName("workflowSidebar")
        self._width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(
            main_window.config_data.get("workflow_sidebar_width", 260)
        )))
        self._open = False
        self._anim = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 8, 12)
        content_layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("WORKFLOWS")
        title.setObjectName("hint")
        header.addWidget(title)
        header.addStretch(1)
        self.add_btn = QToolButton()
        self.add_btn.setObjectName("iconButton")
        self.add_btn.setText("+")
        self.add_btn.setToolTip("Add workflow")
        self.add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: main_window._new_workflow_flow())
        header.addWidget(self.add_btn)
        content_layout.addLayout(header)

        self.list = QListWidget()
        self.list.setObjectName("workflowList")
        self.list.setDragDropMode(QListWidget.InternalMove)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(main_window._on_workflow_selected)
        self.list.itemDoubleClicked.connect(main_window._edit_workflow_by_item)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(main_window._on_workflow_context_menu)
        self.list.model().rowsMoved.connect(main_window._on_workflow_rows_moved)
        content_layout.addWidget(self.list, 1)

        self.empty_hint = QLabel('No workflows yet.\nClick "+" above to add a ComfyUI workflow.')
        self.empty_hint.setObjectName("hint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        content_layout.addWidget(self.empty_hint)

        # Settings lives here now, bottom-left, in the free space under the
        # workflow list -- same flat icon-button design/height as the "+"
        # add-workflow button above, just with the gear glyph instead.
        bottom_row = QHBoxLayout()
        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("iconButton")
        self.settings_btn.setIcon(_make_gear_icon())
        self.settings_btn.setIconSize(QSize(14, 14))
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(main_window._open_settings_from_workflow_sidebar)
        bottom_row.addWidget(self.settings_btn)
        bottom_row.addStretch(1)

        # Hotkeys lives in the opposite (bottom-right) corner of the
        # sidebar, mirroring Settings' bottom-left placement -- just a "?"
        # glyph since there's nothing here to configure, only to look up.
        self.hotkeys_btn = QToolButton()
        self.hotkeys_btn.setObjectName("iconButton")
        self.hotkeys_btn.setText("?")
        self.hotkeys_btn.setToolTip("Hotkeys")
        self.hotkeys_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hotkeys_btn.clicked.connect(main_window._open_hotkeys_from_workflow_sidebar)
        bottom_row.addWidget(self.hotkeys_btn)
        content_layout.addLayout(bottom_row)

        layout.addWidget(content, 1)
        self.handle = _SidebarHandle(self, sign=1)
        layout.addWidget(self.handle)

        # This sidebar floats *over* the app content rather than living in
        # its layout, so a real drop shadow reads correctly here (nothing
        # else needs to make room for it) -- it's the cue that this panel
        # is above the rest of the UI, not flush with it.
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(6, 0)
        shadow.setColor(QColor(0, 0, 0, 170))
        self.setGraphicsEffect(shadow)

        self.resize(self._width, self.height())
        self.hide()
        self.update_empty_hint()

    def update_empty_hint(self):
        empty = self.list.count() == 0
        self.empty_hint.setVisible(empty)
        self.list.setVisible(not empty)


    # -- width ------------------------------------------------------------
    def set_width(self, width, persist=True):
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(width)))
        if width == self._width:
            return
        self._width = width
        self.main_window._position_workflow_sidebar()
        if persist:
            self.persist_width()

    def persist_width(self):
        self.main_window.config_data["workflow_sidebar_width"] = self._width
        self.main_window.persist_all()

    # -- open / close -------------------------------------------------------
    def is_open(self):
        return self._open

    def toggle(self):
        self.set_open(not self._open)

    def set_open(self, open_):
        if open_ == self._open:
            return
        self._open = open_
        container = self.main_window.center_container
        start_rect = self.geometry()
        if open_:
            self.show()
            self.raise_()
            # Runs flush to the left edge (x=0) -- see matching comment in
            # OutputsSidebar.set_open for why no gap is reserved for the
            # edge tab pill here.
            end_rect = QRect(0, 0, self._width, container.height())
        else:
            end_rect = QRect(-self._width, 0, self._width, container.height())

        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(180)
        anim.setStartValue(start_rect)
        anim.setEndValue(end_rect)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if not open_:
            anim.finished.connect(self.hide)
        anim.start()
        self._anim = anim
        self.main_window._sync_edge_tab()
        edge = getattr(self.main_window, "workflow_edge_tab", None)
        if edge is not None:
            edge.raise_()


# ---------------------------------------------------------------------------
# Small always-visible open/closed indicator tab on the left edge of the
# center content, so the workflow sidebar stays discoverable even when
# collapsed (spec 2.1).
# ---------------------------------------------------------------------------
class _WorkflowEdgeTab(QWidget):
    """The open/close pill for the left (Workflows) sidebar. This widget
    *is* the pill -- fixed to its visible size -- rather than a full-
    height strip with the pill floating inside it, so only the pill's own
    footprint is clickable and the rest of the edge column is left
    completely alone for whatever's underneath (the image browser) to
    receive clicks normally instead of the sidebar toggle stealing them."""

    WIDTH = 14
    HEIGHT = 46

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setObjectName("edgeTabPill")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Workflows")
        self.setStyleSheet(
            "border-top-right-radius: 8px; border-bottom-right-radius: 8px; border-left: none;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._chevron = QLabel("\u203a")
        self._chevron.setObjectName("edgeTabChevron")
        self._chevron.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._chevron)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.main_window._toggle_workflow_sidebar()
        super().mousePressEvent(event)

    def set_open_state(self, open_):
        self._chevron.setText("\u2039" if open_ else "\u203a")


# ---------------------------------------------------------------------------
# Mirror image of _WorkflowEdgeTab, docked to the right edge of the center
# content for the Outputs / Queue sidebar — same always-visible open/close
# chevron design, just flipped: closed shows ‹ (pull left to open), open
# shows › (push right to close).
# ---------------------------------------------------------------------------
class _OutputsEdgeTab(QWidget):
    """Mirror image of _WorkflowEdgeTab for the right (Outputs / Queue)
    sidebar -- same "widget IS the pill, nothing more" sizing so it never
    overlaps app content above/below the pill itself."""

    WIDTH = EDGE_TAB_WIDTH
    HEIGHT = 46

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setObjectName("edgeTabPill")
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Outputs / Queue")
        self.setStyleSheet(
            "border-top-left-radius: 8px; border-bottom-left-radius: 8px; border-right: none;"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._chevron = QLabel("\u2039")
        self._chevron.setObjectName("edgeTabChevron")
        self._chevron.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._chevron)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.main_window._toggle_sidebar()
        super().mousePressEvent(event)

    def set_open_state(self, open_):
        self._chevron.setText("\u203a" if open_ else "\u2039")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if ICON_FILE.exists():
            self.setWindowIcon(QIcon(str(ICON_FILE)))
        self._is_windows = _IS_WINDOWS
        self._RESIZE_MARGIN = 6
        self._size_grip = None
        self._img_viewer = None

        if not self._is_windows:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setObjectName("mainWindowFrameless")
            self.setMouseTracking(True)
            QApplication.instance().installEventFilter(self)

        self.setMinimumSize(720, 520)
        self.resize(1280, 840)

        self.config_data = load_config()
        # No longer a supported key (the explorer never remembers its last
        # tab/expanded folders across restarts -- see ImageBrowser.__init__);
        # drop it so it doesn't linger forever in an old config file.
        self.config_data.pop("image_browser_state", None)
        self._config_save_failing = False
        if _LAST_LOAD_WARNING:
            # Deferred so it pops up once the window is actually on screen,
            # instead of appearing to block/delay startup before anything
            # is visible.
            _warning_text = _LAST_LOAD_WARNING
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(self, "Settings couldn't be read", _warning_text),
            )
        self.server = self.config_data.get("server", DEFAULT_SERVER)
        self.output_dir = self.config_data.get("output_dir", DEFAULT_OUTPUT_DIR)

        # ── Restore last window geometry ──────────────────────────────
        _geo = self.config_data.get("window_geometry")
        if isinstance(_geo, dict):
            x, y, w, h = _geo.get("x"), _geo.get("y"), _geo.get("width"), _geo.get("height")
            if all(isinstance(v, int) for v in (x, y, w, h)):
                self.resize(max(720, w), max(520, h))
                self.move(x, y)
            available = QGuiApplication.primaryScreen().virtualGeometry()
            title_bar_rect = self.frameGeometry()
            title_bar_rect.setHeight(min(100, title_bar_rect.height()))
            if not available.intersects(title_bar_rect):
                primary = QGuiApplication.primaryScreen().availableGeometry()
                self.move(
                    primary.x() + (primary.width() - self.width()) // 2,
                    primary.y() + (primary.height() - self.height()) // 2,
                )

        self.queue_manager = QueueManager(self)
        self.workflow_states = []
        self.active_workflow = None
        # Guards persist_all() against running before the saved workflow
        # tabs have actually been loaded into self.workflow_states (see
        # end of __init__). Several child widgets constructed below --
        # e.g. ImageBrowser restoring its last-active folder tab -- fire
        # signals that call persist_all() as a side effect. Without this
        # guard, that early call would serialize the still-empty
        # self.workflow_states into config_data["tabs"] and write it to
        # disk, permanently wiping out the saved tabs before the startup
        # loop below ever gets a chance to restore them.
        self._startup_complete = False

        # ── Outer shell: custom title bar on top, app content below ───────
        shell = QWidget()
        shell.setObjectName("appShell")
        shell.setProperty("maximized", "false")
        self._shell = shell
        self.setCentralWidget(shell)
        shell_lay = QVBoxLayout(shell)
        shell_lay.setContentsMargins(1, 1, 1, 1)
        shell_lay.setSpacing(0)

        title_bar = _TitleBar(self)
        title_bar.setObjectName("appTitleBar")
        title_bar.setFixedHeight(34)
        self._title_bar = title_bar
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(14, 0, 8, 0)
        tb_lay.setSpacing(2)

        brand_lbl = QLabel()
        brand_lbl.setObjectName("brandLbl")
        brand_lbl.setText(
            f'<span style="color:#e8e8e8;">{APP_BRAND_PREFIX}</span>'
            f'<span style="color:#00d4a0;">{APP_BRAND_SUFFIX}</span>'
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

        self._size_grip = QSizeGrip(shell)
        self._size_grip.setFixedSize(14, 14)
        if self._is_windows:
            self._size_grip.setVisible(False)

        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        # ── Center: persistent Image Browser + bottom Input Roster ────────
        self.center_container = CenterContainer(self)
        outer.addWidget(self.center_container, 1)

        center_lay = QVBoxLayout(self.center_container)
        center_lay.setContentsMargins(0, 0, 0, 0)
        center_lay.setSpacing(0)

        # The space between the browser and the roster is manually
        # adjustable by dragging the splitter handle between them, and the
        # chosen split is remembered across launches.
        self.center_splitter = _CenterSplitter(Qt.Orientation.Vertical)
        self.center_splitter.setObjectName("centerSplitter")
        self.center_splitter.setChildrenCollapsible(False)
        self.center_splitter.setHandleWidth(10)
        center_lay.addWidget(self.center_splitter, 1)

        self.image_browser = ImageBrowser(self)
        self.center_splitter.addWidget(self.image_browser)

        # ── Image viewer overlay (ported from vael. indexer) ───────────────
        # Parented to the content root so it covers browser + roster; a
        # plain left-click on any thumbnail opens it (see ThumbnailCard /
        # FolderSection._on_card_view_requested).
        self._img_viewer = ImgViewerOverlay(root)
        self._img_viewer.resize(root.size())

        # Dimming/blur backdrop shared by the Settings and Hotkeys dialogs
        # (see open_settings / open_hotkeys) -- same treatment as the bulk
        # folder-load overlay, so it's clear the app can't be interacted
        # with while either dialog is open.
        self._modal_dim_overlay = _ModalDimOverlay(root)

        self.roster_bar = RosterBar(self)
        self.center_splitter.addWidget(self.roster_bar)

        self.center_splitter.setStretchFactor(0, 1)
        self.center_splitter.setStretchFactor(1, 0)
        self.center_splitter.splitterMoved.connect(self._on_center_splitter_moved)
        # Give the roster a sane initial height (either the remembered one,
        # or its natural size-hint the very first time the app runs).
        saved_roster_h = self.config_data.get("center_split_roster_height")
        if saved_roster_h:
            total = max(self.height() - 120, 300)
            self.center_splitter.setSizes([max(120, total - saved_roster_h), saved_roster_h])

        # ── Left sidebar: workflow switcher (overlay, closed by default) ──
        self.workflow_sidebar = WorkflowSidebar(self)
        self.workflow_edge_tab = _WorkflowEdgeTab(self, self.center_container)
        self.workflow_edge_tab.raise_()

        # ── Right sidebar: Outputs / Queue (overlay, closed by default) ──
        # Mirrors the left sidebar exactly: an always-visible edge rail with
        # an open/close chevron, so the arrow never gets buried under the
        # sidebar the way it used to.
        self.outputs_sidebar = OutputsSidebar(self)
        self.outputs_tab = self.outputs_sidebar.outputs_panel
        self.outputs_edge_tab = _OutputsEdgeTab(self, self.center_container)
        self.outputs_edge_tab.raise_()
        self.queue_manager.itemFinished.connect(lambda *_: self.outputs_tab.refresh())

        # Queue count badge, sidebar-closed position: sits directly below
        # the edge-tab open button. Shown only while the sidebar itself is
        # closed -- once it's open, OutputsTab's own header badge (right of
        # the "Queue" label) takes over instead.
        self.queue_badge_collapsed = _QueueCountBadge(self.center_container)
        self.queue_manager.queueChanged.connect(self._update_collapsed_queue_badge)
        self._update_collapsed_queue_badge()

        for workflow_data in self.config_data.get("tabs", []):
            self._add_workflow(workflow_data, select=False)
        if self.workflow_states:
            self.workflow_sidebar.list.setCurrentRow(0)

        # Saved tabs are now restored into self.workflow_states -- it's
        # finally safe to let persist_all() write config_data to disk.
        self._startup_complete = True

        self._setup_hotkeys()
        self._position_sidebar()
        self._position_workflow_sidebar()


    # -- center splitter (Image Browser / Input Roster) --------------------
    def _on_center_splitter_moved(self, pos, index):
        sizes = self.center_splitter.sizes()
        if len(sizes) == 2:
            self.config_data["center_split_roster_height"] = sizes[1]
            self.persist_all()

    # -- sidebars ---------------------------------------------------------
    def _toggle_sidebar(self):
        self.outputs_sidebar.toggle()
        self._sync_outputs_edge_tab()
        self.outputs_edge_tab.raise_()
        self._position_collapsed_queue_badge()

    def _toggle_workflow_sidebar(self):
        self.workflow_sidebar.toggle()
        self.workflow_edge_tab.raise_()

    def _sync_edge_tab(self):
        # The edge tab is re-raised on every toggle (see _toggle_workflow_
        # sidebar / WorkflowSidebar.set_open), so its arrow always stays on
        # top of the sliding sidebar instead of disappearing underneath it.
        open_ = self.workflow_sidebar.is_open()
        self.workflow_edge_tab.set_open_state(open_)

    def _sync_outputs_edge_tab(self):
        open_ = self.outputs_sidebar.is_open()
        self.outputs_edge_tab.set_open_state(open_)

    # -- queue count badge (sidebar-closed position) ------------------------
    def _update_collapsed_queue_badge(self):
        badge = getattr(self, "queue_badge_collapsed", None)
        if badge is None:
            return
        badge.set_count(len(self.queue_manager.items))
        self._position_collapsed_queue_badge()

    def _position_collapsed_queue_badge(self):
        badge = getattr(self, "queue_badge_collapsed", None)
        edge = getattr(self, "outputs_edge_tab", None)
        sidebar = getattr(self, "outputs_sidebar", None)
        if badge is None or edge is None:
            return
        bx = edge.x() + (edge.width() - badge.width()) // 2
        by = edge.y() + edge.height() + 4
        badge.move(bx, by)
        badge.raise_()
        # Only ever visible while the sidebar is closed -- once it's open,
        # OutputsTab's own header badge takes over (see OutputsTab).
        if badge.count() <= 0 or (sidebar is not None and sidebar.is_open()):
            badge.hide()
        else:
            badge.show()

    def _position_sidebar(self):
        """Keep the outputs sidebar (and its always-visible edge tab)
        anchored to the right of the center content. The sidebar now runs
        flush to the edge when open (no gap reserved for the edge tab
        pill) -- the pill stays visible on top via raise_() instead."""
        sidebar = getattr(self, "outputs_sidebar", None)
        if sidebar is None:
            return
        container = self.center_container
        w = sidebar._width
        x = max(0, container.width() - w) if sidebar.is_open() else container.width()
        sidebar.setGeometry(x, 0, w, container.height())
        edge = getattr(self, "outputs_edge_tab", None)
        if edge is not None:
            edge.setGeometry(
                container.width() - EDGE_TAB_WIDTH,
                (container.height() - edge.HEIGHT) // 2,
                edge.WIDTH, edge.HEIGHT,
            )
            edge.raise_()
        self._position_collapsed_queue_badge()

    def _position_workflow_sidebar(self):
        """Same idea, mirrored: keeps the workflow sidebar (and its always-
        visible edge tab) anchored to the left of the center content. Runs
        flush to x=0 when open -- no gap reserved for the edge tab pill,
        which stays visible on top via raise_() instead."""
        sidebar = getattr(self, "workflow_sidebar", None)
        if sidebar is None:
            return
        container = self.center_container
        w = sidebar._width
        x = 0 if sidebar.is_open() else -w
        sidebar.setGeometry(x, 0, w, container.height())
        edge = getattr(self, "workflow_edge_tab", None)
        if edge is not None:
            edge.setGeometry(
                0, (container.height() - edge.HEIGHT) // 2,
                edge.WIDTH, edge.HEIGHT,
            )
            edge.raise_()

    # -- workflow bookkeeping -------------------------------------------
    def rename_workflow(self, state, name):
        if state not in self.workflow_states:
            return
        idx = self.workflow_states.index(state)
        item = self.workflow_sidebar.list.item(idx)
        if item is not None:
            item.setText(name)

    def _on_workflow_selected(self, row):
        if row < 0 or row >= len(self.workflow_states):
            self.active_workflow = None
            self.roster_bar.set_workflow(None)
            return
        self.active_workflow = self.workflow_states[row]
        self.roster_bar.set_workflow(self.active_workflow)
        if self.workflow_sidebar.is_open():
            self.workflow_sidebar.set_open(False)

    def _edit_workflow_by_item(self, item):
        row = self.workflow_sidebar.list.row(item)
        if 0 <= row < len(self.workflow_states):
            self._edit_workflow(self.workflow_states[row])

    def _on_workflow_context_menu(self, pos):
        """Right-clicking a workflow in the sidebar opens a per-row menu
        (spec 2.1) with quick Edit / Delete actions, replacing the old tab
        bar's context menu."""
        item = self.workflow_sidebar.list.itemAt(pos)
        if item is None:
            return
        row = self.workflow_sidebar.list.row(item)
        if not (0 <= row < len(self.workflow_states)):
            return
        state = self.workflow_states[row]

        menu = QMenu(self)
        edit_action = menu.addAction("Edit\u2026")
        delete_action = menu.addAction("Delete")
        chosen = menu.exec(self.workflow_sidebar.list.viewport().mapToGlobal(pos))
        if chosen is edit_action:
            self._edit_workflow(state)
        elif chosen is delete_action:
            self._delete_workflow(state)

    def _delete_workflow(self, state):
        if QMessageBox.question(
            self, "Delete workflow", f"Delete workflow '{state.name}'? This cannot be undone."
        ) != QMessageBox.Yes:
            return
        idx = self.workflow_states.index(state)
        self.workflow_sidebar.list.takeItem(idx)
        self.workflow_states.remove(state)
        self.workflow_sidebar.update_empty_hint()
        self.persist_all()

    def open_settings(self):
        self._modal_dim_overlay.show_overlay()
        try:
            dlg = SettingsDialog(self)
            dlg.exec()
        finally:
            self._modal_dim_overlay.hide_overlay()

    def _open_settings_from_workflow_sidebar(self):
        """Settings button lives at the bottom of the workflow sidebar;
        opening it auto-closes that sidebar (and only that one -- the
        outputs/queue sidebar is never touched by this)."""
        if self.workflow_sidebar.is_open():
            self.workflow_sidebar.set_open(False)
        self.open_settings()

    def open_hotkeys(self):
        self._modal_dim_overlay.show_overlay()
        try:
            dlg = HotkeysDialog(self)
            dlg.exec()
        finally:
            self._modal_dim_overlay.hide_overlay()

    def _open_hotkeys_from_workflow_sidebar(self):
        """Hotkeys button lives at the bottom of the workflow sidebar,
        opposite Settings; opening it auto-closes that sidebar the same
        way Settings does."""
        if self.workflow_sidebar.is_open():
            self.workflow_sidebar.set_open(False)
        self.open_hotkeys()

    def _on_workflow_rows_moved(self, parent, start, end, dest_parent, dest_row):
        # Drag-reordering in the sidebar list already moved the visual rows;
        # resync the python list (and persist) to match the new order.
        lw = self.workflow_sidebar.list
        self.workflow_states = [lw.item(i).data(Qt.UserRole) for i in range(lw.count())]
        self.persist_all()

    def _new_workflow_flow(self):
        dlg = WorkflowConfigDialog(self, mode="create")
        dlg.exec()
        if dlg.result == "save":
            self._add_workflow(
                {"workflow_path": dlg.workflow_path, "optional_identifier": dlg.optional_identifier}, select=True,
            )

    def _edit_workflow(self, state):
        dlg = WorkflowConfigDialog(self, mode="edit", tab=state)
        dlg.exec()
        if dlg.result == "delete":
            idx = self.workflow_states.index(state)
            self.workflow_sidebar.list.takeItem(idx)
            self.workflow_states.remove(state)
            self.workflow_sidebar.update_empty_hint()
        elif dlg.result == "save":
            if dlg.workflow_path != state.workflow_path:
                try:
                    state.load_workflow(dlg.workflow_path)
                except Exception as e:
                    QMessageBox.critical(self, "Workflow error", str(e))
                    return
            state.optional_identifier = dlg.optional_identifier
            state.optional_node_id = (
                find_node(state.raw_workflow, dlg.optional_identifier) if dlg.optional_identifier else None
            )
            state.param_values = {k: v for k, (v, _t) in state.editable_params().items()}
            state.slotsRebuilt.emit()
        self.persist_all()

    def _add_workflow(self, data, select=True):
        state = WorkflowState(self, data)
        item = QListWidgetItem(state.name)
        item.setData(Qt.UserRole, state)
        self.workflow_sidebar.list.addItem(item)
        self.workflow_states.append(state)
        self.workflow_sidebar.update_empty_hint()
        if select:
            self.workflow_sidebar.list.setCurrentRow(self.workflow_sidebar.list.count() - 1)
        self.persist_all()
        return state

    # -- Image Browser --------------------------------------------------
    def _on_browser_thumbnail_clicked(self, filepath):
        """Primary selection flow (spec 3.6): a thumbnail click assigns the
        currently armed roster slot, if any."""
        if self.roster_bar.armed_index is None:
            self.roster_bar.status_label.setText(
                "Arm a roster slot below first, then click a thumbnail to assign it."
            )
            self.roster_bar._position_status_label()
            return
        self.roster_bar.assign_armed(filepath)

    def open_image_viewer(self, card, cards):
        """Open the full-size viewer for card, with cards as the prev/next list."""
        if self._img_viewer is not None:
            self._img_viewer.resize(self._content_root.size())
            self._img_viewer.open_viewer(card, cards)

    # -- outputs ---------------------------------------------------------
    # The app no longer writes generated images itself. The workflow's own
    # Save Image node is what persists the file to disk; the output folder
    # configured in Settings is purely where the app *looks* to list and
    # preview those images in the Outputs sidebar (see OutputsPanel.refresh
    # and the Settings dialog hint) -- point it at wherever your workflows
    # actually save to.

    # -- persistence -------------------------------------------------------
    def persist_all(self):
        if not self._startup_complete:
            # Something fired during widget construction, before saved
            # tabs were restored into self.workflow_states -- writing now
            # would serialize an empty/partial state and clobber the real
            # data still sitting in config_data["tabs"] from disk. Once
            # startup finishes it flips this flag and calls persist_all()
            # itself if anything needs flushing.
            return
        self.config_data["tabs"] = [s.to_dict() for s in self.workflow_states]
        self.config_data["server"] = self.server
        self.config_data["output_dir"] = self.output_dir
        ok = save_config(self.config_data)
        if not ok:
            # Only pop the warning on the *transition* into a failing state
            # (not on every single save attempt, which would otherwise mean
            # a message box on every click while e.g. a sync tool is
            # temporarily locking the file) -- but never fail silently.
            if not self._config_save_failing:
                self._config_save_failing = True
                QMessageBox.warning(
                    self,
                    "Couldn't save settings",
                    "vael. cover couldn't write workflows_config.json, so your "
                    "workflows and other settings are NOT being saved right now.\n\n"
                    f"Reason: {_LAST_SAVE_ERROR}\n\n"
                    "This is usually a permissions problem, a read-only location, "
                    "or a sync tool (e.g. OneDrive) locking the file. Once that's "
                    "resolved, saving will resume automatically -- this warning "
                    "won't be shown again until it fails once more.",
                )
        elif self._config_save_failing:
            self._config_save_failing = False

    # -- frameless window chrome (copied from vael. indexer) ───────────────
    def nativeEvent(self, eventType, message):
        if self._is_windows and eventType in ("windows_generic_MSG", b"windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_NCCALCSIZE:
                if msg.wParam:
                    if self.isMaximized():
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
    def _edge_set(edges: "Qt.Edges") -> set:
        result = set()
        for e in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
            if edges & e:
                result.add(e)
        return result

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._size_grip is not None:
            self._size_grip.move(
                self.width() - self._size_grip.width() - 2,
                self.height() - self._size_grip.height() - 2,
            )
            self._size_grip.raise_()
        if self._img_viewer is not None:
            self._img_viewer.resize(self._content_root.size())
        bulk_overlay = getattr(getattr(self, "image_browser", None), "bulk_overlay", None)
        if bulk_overlay is not None and bulk_overlay.isVisible():
            bulk_overlay.resize(self._content_root.size())
        modal_dim = getattr(self, "_modal_dim_overlay", None)
        if modal_dim is not None and modal_dim.isVisible():
            modal_dim.resize(self._content_root.size())
        self._position_collapsed_queue_badge()
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
        is_max = self.isMaximized() or self.isFullScreen()
        prop = "true" if is_max else "false"
        if self._shell.property("maximized") != prop:
            self._shell.setProperty("maximized", prop)
            self._shell.style().unpolish(self._shell)
            self._shell.style().polish(self._shell)
        self._update_window_mask()

    def _update_window_mask(self) -> None:
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

    def closeEvent(self, event):
        if not self._is_windows:
            QApplication.instance().removeEventFilter(self)
        self.config_data["window_geometry"] = {
            "x": self.x(), "y": self.y(), "width": self.width(), "height": self.height(),
        }
        self.persist_all()
        # If a "Load Selected Folders" bulk scan/preload is still in
        # flight, ask it to stop rather than letting its worker threads
        # keep touching the (about-to-be-destroyed) UI/cache.
        try:
            for browser in self.findChildren(ImageBrowser):
                browser.bulk_loader.cancel()
        except Exception:
            pass
        # Cleanly stop the shared background pixmap-loading thread so Qt
        # doesn't warn (or abort) about a running QThread at interpreter exit.
        PIXMAP_WORKER.stop()
        PIXMAP_WORKER.wait(1500)
        # Same for the bulk-load pools: give queued/running tasks a bounded
        # window to notice the cancel flag and exit cleanly before we tear
        # the process down, instead of leaving Qt to abort mid-task.
        _BULK_SCAN_POOL.waitForDone(1500)
        _BULK_PIXMAP_POOL.waitForDone(1500)
        super().closeEvent(event)

    # -- hotkeys -------------------------------------------------------
    def _setup_hotkeys(self):
        self._shortcuts = []

        def bind(seq, func):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(func)
            self._shortcuts.append(sc)

        bind("Ctrl+R", self._hk_run_current)
        bind("Ctrl+Shift+A", self._hk_queue_current)
        bind("Ctrl+Shift+R", self.queue_manager.run_queue)
        bind("Ctrl+Shift+X", self.queue_manager.clear)
        bind("Ctrl+N", self._new_workflow_flow)
        bind("Ctrl+Tab", self._hk_next_workflow)
        bind("Ctrl+Shift+Tab", self._hk_prev_workflow)
        bind("Ctrl+O", self._toggle_sidebar)
        bind("Ctrl+Shift+W", self._toggle_workflow_sidebar)
        bind("Ctrl+,", self.open_settings)
        bind("Ctrl+Shift+O", self.outputs_tab._open_folder)
        bind("F5", self.outputs_tab.refresh)

    def _hk_run_current(self):
        if self.active_workflow:
            self.active_workflow.run_now()

    def _hk_queue_current(self):
        if self.active_workflow:
            self.active_workflow.add_to_queue()

    def _hk_next_workflow(self):
        lw = self.workflow_sidebar.list
        n = lw.count()
        if n:
            lw.setCurrentRow((lw.currentRow() + 1) % n)

    def _hk_prev_workflow(self):
        lw = self.workflow_sidebar.list
        n = lw.count()
        if n:
            lw.setCurrentRow((lw.currentRow() - 1) % n)


def apply_style(app: QApplication) -> None:
    app.setStyle("Fusion")

    if ICON_FILE.exists():
        app.setWindowIcon(QIcon(str(ICON_FILE)))

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

    app.setStyleSheet(STYLE_QSS)


def main():
    if _IS_WINDOWS:
        # Without this, Windows groups the taskbar entry under python.exe's
        # own AppUserModelID and shows the Python icon instead of ours, no
        # matter what setWindowIcon() is called with. Must run before the
        # QApplication (and thus the first window) is created.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "vael.cover.app.1"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    apply_style(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
