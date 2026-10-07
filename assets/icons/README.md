# XlikeSaver application icon

The user supplied a red heart with a blue download arrow. The requested edit
redraws those shapes with a darker blue outline, without a white background.
`xlikesaver.png` is the transparent RGBA master, generated in imagegen edit mode
with the supplied image as a reference. `xlikesaver.ico` preserves transparency
at 16, 24, 32, 48, 64, 128 and 256 pixels for Explorer, window and taskbar icons.

Rebuild the ICO with `.venv/Scripts/python.exe tools/build_icon.py`.
This conversion only resizes and packages the supplied master; it does not
change its artwork or transparency.

Generation prompt: Redraw the reference as a clean professional Windows icon:
a rounded red heart, a bright blue to indigo down arrow and U-shaped tray in
the lower right, thin deep-blue outlines, crisp simple geometry, 8 percent
padding; no white pixels or outlines, text, texture, glow or shadow; actual
transparent alpha background, including spaces between arrow and tray.
