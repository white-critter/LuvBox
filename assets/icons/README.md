# LuvBox application icon

A fine red heart outline on a near-black rounded square that fills the icon.
`tools/draw_icon.py` renders it at 1024 px and reduces it to `luvbox.png` at 256 px. The
corners are transparent. `luvbox.ico` holds 16, 24, 32, 48, 64, 128 and 256
pixel images for Explorer, window, taskbar and tray icons.

Redraw with `.venv/Scripts/python.exe tools/draw_icon.py`, then rebuild the ICO
with `.venv/Scripts/python.exe tools/build_icon.py`.
