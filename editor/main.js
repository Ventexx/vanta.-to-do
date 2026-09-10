const { app, BrowserWindow, ipcMain, globalShortcut, dialog, nativeImage } = require('electron');
const path = require('path');
const fs = require('fs');

let win;

// Windows keys the taskbar icon/grouping off the app's AppUserModelID, not
// just the BrowserWindow `icon` option below -- without this, a dev run (or
// even some packaged installs) can silently fall back to the generic
// Electron icon in the taskbar even though icon.ico loads fine for the
// window itself. Matches the appId in package.json's build config. No-op
// on other platforms.
if (process.platform === 'win32') {
  app.setAppUserModelId('com.vael.editor');
}

function createWindow() {
  // Load icon
  const iconPath = path.join(__dirname, process.platform === 'win32' ? 'icon.ico' : 'icon.png');
  const icon = fs.existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : undefined;

  win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 500,
    backgroundColor: '#0a0a0a',
    frame: false,          // custom titlebar
    titleBarStyle: 'hidden',
    icon,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  win.loadFile('editor.html');

  globalShortcut.register('F12', () => win.webContents.toggleDevTools());

  // Let the renderer handle close confirmation
  win.on('close', e => {
    e.preventDefault();
    win.webContents.executeJavaScript('attemptClose()');
  });
}

// Window control IPC
ipcMain.on('win-minimize', () => win.minimize());
ipcMain.on('win-maximize', () => win.isMaximized() ? win.unmaximize() : win.maximize());
ipcMain.on('win-close',    () => { win.destroy(); app.quit(); });

// Open a whole folder of images at once (core workflow: batch-pixelate a shoot)
const IMG_EXT = new Set(['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif']);

// How big (long edge, px) the thumbnails we hand back to the renderer are.
// This only needs to comfortably cover the filmstrip thumbnail's max on-screen
// size (~160x60 CSS px at up to 2x devicePixelRatio == ~320px) — it does NOT
// need to be anywhere near full resolution, because the renderer no longer
// keeps every opened image's full-size pixels in memory (see editor.html's
// lazy-load/eviction code). Keeping this small is what makes it possible to
// import thousands of images without stalling or exhausting memory.
const THUMB_MAX_PX = 480;

// Used to keep the main process responsive (it also owns the window/dialogs)
// while walking a folder with hundreds or thousands of files: after this many
// files we yield back to the event loop for a tick before continuing.
const YIELD_EVERY = 25;
function yieldToEventLoop() {
  return new Promise(resolve => setImmediate(resolve));
}

// Reads every image directly inside `dir` — one directory, non-recursive —
// and returns lightweight entries — {name, path, w, h, thumbDataUrl} —
// instead of the full-resolution file bytes.
//
// This used to fs.readFileSync + base64-encode the FULL file for every image
// up front, all in one synchronous pass, and hand the entire batch back to
// the renderer in a single IPC message. That's fine for a couple dozen
// photos; for 10 folders totaling 1-2k images it means gigabytes of base64
// built up in memory and one enormous IPC payload — which is exactly what
// was crashing (or freezing, then getting killed as unresponsive) the app.
// Now we only ever produce small thumbnails here (via Electron's native
// nativeImage decoder/resizer, so no giant intermediate buffers), and the
// renderer fetches a given image's real bytes lazily, one at a time, only
// once it's actually opened or edited (see 'read-image-full' below).
async function readImagesInSingleDir(dir) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (e) {
    return null;
  }
  const names = entries
    .filter(e => e.isFile() && IMG_EXT.has(path.extname(e.name).toLowerCase()))
    .map(e => e.name)
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  const images = [];
  for (let i = 0; i < names.length; i++) {
    const name = names[i];
    const filePath = path.join(dir, name);
    try {
      const full = nativeImage.createFromPath(filePath);
      const { width, height } = full.getSize();
      if (!width || !height) continue; // unreadable/corrupt image, skip it
      const thumb = width > THUMB_MAX_PX || height > THUMB_MAX_PX
        ? full.resize(width >= height ? { width: THUMB_MAX_PX } : { height: THUMB_MAX_PX })
        : full;
      images.push({ name, path: filePath, w: width, h: height, thumbDataUrl: thumb.toDataURL() });
    } catch (e) { /* skip unreadable file */ }
    if (i % YIELD_EVERY === 0) await yieldToEventLoop();
  }
  return images;
}

// Shared by the dialog-based "Open Folder" button and by drag-and-drop of a
// folder from the OS file explorer. Reads images out of `dir` two layers
// deep: every image directly inside `dir` (first layer), followed by every
// image directly inside each of `dir`'s immediate subfolders (second layer,
// subfolders walked in name order) — but no deeper than that. The two
// layers are simply concatenated in that order into one flat list, so a
// dropped folder full of subfolders still becomes a single category, with
// its images ordered first-layer, then first subfolder, then second
// subfolder, etc. Each returned entry still carries the image's real,
// original absolute `path` on disk (from readImagesInSingleDir), so saving
// later writes back to wherever the file actually lives — the merging here
// is purely an in-app grouping and never moves anything on disk.
async function readImagesFromDir(dir) {
  const firstLayer = await readImagesInSingleDir(dir);
  if (firstLayer === null) return null; // dir itself unreadable/missing

  let subdirNames;
  try {
    subdirNames = fs.readdirSync(dir, { withFileTypes: true })
      .filter(e => e.isDirectory())
      .map(e => e.name)
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  } catch (e) {
    subdirNames = [];
  }

  let images = firstLayer;
  for (const subName of subdirNames) {
    const subImages = await readImagesInSingleDir(path.join(dir, subName));
    if (subImages && subImages.length) images = images.concat(subImages);
  }
  return images;
}

ipcMain.handle('open-folder', async () => {
  const { filePaths, canceled } = await dialog.showOpenDialog(win, {
    properties: ['openDirectory'],
    title: 'Open folder of images',
  });
  if (canceled || !filePaths || !filePaths[0]) return null;
  const dir = filePaths[0];
  const images = await readImagesFromDir(dir);
  if (images === null) return null;
  return { dir, images };
});

// Drag-and-drop of a folder from the OS: Electron gives every dropped File —
// folders included — a real absolute `.path`, but a plain browser File object
// can't be read as a directory. The renderer collects the dropped paths and
// hands them here; we stat each one and, for directories, read their images
// the same way "Open Folder" does, so a dropped folder becomes its own
// category with no further prompting. Multiple folders dropped at once
// (e.g. 10 at a time) are handled fine now since each only produces
// thumbnails, not full image data.
ipcMain.handle('inspect-dropped-paths', async (_, paths) => {
  const results = [];
  for (const p of paths || []) {
    let stat;
    try {
      stat = fs.statSync(p);
    } catch (e) {
      results.push({ path: p, isDirectory: false });
      continue;
    }
    if (stat.isDirectory()) {
      const images = (await readImagesFromDir(p)) || [];
      results.push({ path: p, isDirectory: true, name: path.basename(p), images });
    } else {
      results.push({ path: p, isDirectory: false });
    }
  }
  return results;
});

// Fetches one image's real full-resolution bytes as a data URL, on demand.
// The renderer calls this the moment it actually needs the pixels — an image
// gets opened/selected, edited, exported, etc. — instead of every image in
// an imported folder being fully decoded and held in memory up front. Reads
// asynchronously (not readFileSync) so a slow/large file never blocks the
// main process or the UI.
ipcMain.handle('read-image-full', async (_, filePath) => {
  const buf = await fs.promises.readFile(filePath);
  const ext = path.extname(filePath).slice(1).toLowerCase();
  const mime = ext === 'jpg' ? 'jpeg' : ext;
  return `data:image/${mime};base64,${buf.toString('base64')}`;
});

// Save-as via native dialog
ipcMain.handle('save-as', async (_, src, defaultName) => {
  const { filePath } = await dialog.showSaveDialog(win, {
    defaultPath: defaultName,
    filters: [{ name: 'Images', extensions: ['png', 'jpg', 'jpeg', 'webp'] }],
  });
  if (!filePath) return null;
  const base64 = src.replace(/^data:image\/\w+;base64,/, '');
  fs.writeFileSync(filePath, Buffer.from(base64, 'base64'));
  return filePath;
});

// Save to known path
ipcMain.handle('save', async (_, filePath, src) => {
  const base64 = src.replace(/^data:image\/\w+;base64,/, '');
  fs.writeFileSync(filePath, Buffer.from(base64, 'base64'));
  return true;
});

app.whenReady().then(createWindow);
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });