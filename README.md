# jp2-viewer

A lightweight JPEG 2000 viewer for Windows built in Python with PyQt6. Opens aerial JP2 tiles instantly where IrfanView, Global Mapper, and QGIS feel heavy.

<img width="759" height="434" alt="image" src="https://github.com/user-attachments/assets/d493e621-5064-44bb-94e7-6981b5f85d59" />


## Why

JPEG 2000 is a great format for aerial and satellite imagery but most viewers that handle it are GIS suites with multi-minute startup times, or image browsers with poor JP2 performance. This viewer does one thing well:

- **Sub-second opens** for 100–500 MB aerial tiles by exploiting JP2's built-in resolution pyramid
- **Smooth zoom and pan** via Qt's GraphicsView (GPU view transforms, not per-frame rebuilds)
- **Auto full-resolution upgrade** when you zoom in past 15% — no need to remember a keybinding
- **Geo readout** of cursor coordinates for GeoJP2 files with embedded GeoTIFF metadata
- **~60 MB** bundled as a standalone exe; starts in under half a second

Minimal UI so the image is the focus: toolbar, top-left zoom/filename overlay, top-right minimap, bottom-bar coord readout.

## Features

- Fast preview decode using `glymur`'s multi-resolution slicing (power-of-two strides into the JP2 pyramid)
- Multi-threaded OpenJPEG decode (uses half your CPU cores by default)
- Transparent upgrade from preview to full-resolution when zoom exceeds 15% of original
- Mouse drag to pan, Ctrl+wheel to zoom at cursor
- Keyboard shortcuts for everything the toolbar buttons do
- Drag-and-drop files onto the window
- Folder navigation with ←/→ (sorted by filename)
- Rotate 90° CW/CCW, flip horizontal/vertical
- Fullscreen mode (F or F11) that hides chrome for distraction-free viewing
- Minimap with yellow viewport indicator when zoomed in
- GeoJP2 coordinate readout (decimal degrees for geographic CRS, native units for projected CRS)

## Keyboard shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next file in folder |
| `F` / `F11` | Toggle fullscreen |
| `Esc` | Exit fullscreen |
| `Ctrl++` / `Ctrl+-` | Zoom in / out |
| `Ctrl+0` | Fit to window |
| `Ctrl+1` | 100% (full resolution) |
| `Ctrl+O` | Open file dialog |
| `R` / `Shift+R` | Rotate CW / CCW |
| `H` / `V` | Flip horizontal / vertical |
| `Ctrl+wheel` | Zoom at cursor |
| Mouse drag | Pan |

## Install

### Prerequisites

Python 3.11+ (tested on 3.13). If you have ArcGIS, QGIS, or other apps with bundled Python, install a fresh python.org Python to avoid interference — see the install guide below.

### Setup

```powershell
# Create a dedicated install folder
mkdir C:\Tools\Jp2Viewer
cd C:\Tools\Jp2Viewer

# Create a venv so dependencies don't collide with other Python installs
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

Then drop `jp2viewer.py` into `C:\Tools\Jp2Viewer\` and test:

```powershell
python jp2viewer.py "path\to\some.jp2"
```

If the viewer window opens, you're done. If not, see [Troubleshooting](#troubleshooting).

### Making it launch on double-click in Explorer

You want `.jp2` files to open with a double-click, without a console window flashing. The trick is to register the windowed Python launcher (`pythonw.exe`) with the script path baked in, because Windows's standard "Open With" dialog loses the script argument.

Run this in PowerShell (no admin needed):

```powershell
$py  = "C:\Tools\Jp2Viewer\venv\Scripts\pythonw.exe"
$scr = "C:\Tools\Jp2Viewer\jp2viewer.py"
$cmd = "`"$py`" `"$scr`" `"%1`""

New-Item -Path "HKCU:\Software\Classes\Jp2Viewer.File" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\Jp2Viewer.File" -Name "(Default)" -Value "JPEG 2000 Image"

New-Item -Path "HKCU:\Software\Classes\Jp2Viewer.File\DefaultIcon" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\Jp2Viewer.File\DefaultIcon" -Name "(Default)" -Value "`"$py`",0"

New-Item -Path "HKCU:\Software\Classes\Jp2Viewer.File\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\Jp2Viewer.File\shell\open\command" -Name "(Default)" -Value $cmd

New-Item -Path "HKCU:\Software\Classes\.jp2\OpenWithProgids" -Force | Out-Null
Set-ItemProperty -Path "HKCU:\Software\Classes\.jp2\OpenWithProgids" -Name "Jp2Viewer.File" -Value ([byte[]]@())
```

That registers the ProgID but doesn't make it the default. On Windows 11, the UserChoice key is protected by UCPD.sys — the Settings UI often can't see newly-registered ProgIDs either.

Use **[SetUserFTA](https://setuserfta.com/)** (free personal edition) to actually set the default:

```powershell
.\SetUserFTA.exe .jp2 Jp2Viewer.File
```

Verify:
```powershell
.\SetUserFTA.exe get | Select-String jp2
# should show: .jp2, Jp2Viewer.File
```

Double-click a .jp2 — viewer opens.

### Bundle as a single exe (optional)

```powershell
pip install pyinstaller
pyinstaller --onefile --windowed --name Jp2Viewer jp2viewer.py
```

Exe lands at `dist\Jp2Viewer.exe` (~60 MB). Update the registration above to point at the exe path instead of pythonw.exe.

## Performance tuning

Two constants in `jp2viewer.py` control the preview/zoom behavior:

- `TARGET_MAX_DIM = 4000` — maximum dimension for the initial preview decode. Higher means sharper previews at the cost of slower opens. Try 2500 for very large satellite scenes, 6000 if you have fast storage and want preview quality closer to full res.
- The auto-upgrade threshold of 0.15 in `_maybe_upgrade_resolution`. Lower means you see full-res pixels sooner when zooming (at the cost of a load pause happening earlier).

## Troubleshooting

### `ModuleNotFoundError: No module named 'packaging'`

Older versions of `glymur` forget to declare this dependency. `pip install packaging` fixes it.

### `Jp2k object has no attribute 'read'`

You're on glymur 0.13.5+ which removed `.read()` in favor of numpy-style slicing. This viewer uses the new API, so make sure you're on the version of `jp2viewer.py` from this repo.

### Opens slowly, title bar shows "(1:1)" or no suffix

Your JP2 files may be small enough that the preview pyramid isn't kicking in — `TARGET_MAX_DIM = 4000` means no downsampling for images ≤4000×4000 px. Also check:

```powershell
python -c "import glymur; print('openjpeg:', glymur.version.openjpeg_version)"
```

If that shows `0.0.0`, glymur can't find OpenJPEG and has fallen back to Pillow's much slower JP2 decoder. On Windows, `pillow` ships a usable OpenJPEG DLL — reinstalling it often fixes this.

### Geo coordinates not showing

Either the file isn't a GeoJP2 (no embedded GeoTIFF UUID box — some providers use `.j2w` sidecar files instead) or `tifffile` isn't installed. Check with:

```powershell
python -c "import tifffile; print(tifffile.__version__)"
```

## Companion: Explorer thumbnails

Pairs nicely with **[jp2-winthumb](https://github.com/YOUR_USERNAME/jp2-winthumb)** — a Rust WIC decoder that makes Windows Explorer render JP2 thumbnails in folder view and the preview pane.

## License

MIT. See [LICENSE](LICENSE).

## Built with

- [PyQt6](https://pypi.org/project/PyQt6/) — Qt bindings
- [glymur](https://glymur.readthedocs.io/) — Python bindings to OpenJPEG
- [Pillow](https://python-pillow.org/) — fallback image loading and JP2 decode
- [tifffile](https://pypi.org/project/tifffile/) — parsing embedded GeoTIFF UUID boxes in GeoJP2 files
