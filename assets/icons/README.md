# LuvBox application icon

A thin red heart outline on a near-black rounded square, drawn by
`tools/draw_icon.py` (1024 px, reduced to the 256 px `luvbox.png`). The
corners are transparent. `luvbox.ico` holds 16, 24, 32, 48, 64, 128 and 256
pixel images for Explorer, window and taskbar icons.

Redraw with `.venv/Scripts/python.exe tools/draw_icon.py`, then rebuild the ICO
with `.venv/Scripts/python.exe tools/build_icon.py`.
