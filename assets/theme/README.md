# Control image bundles

`light.zip` and `dark.zip` contain the app's generated PNG controls. They remove
Python rasterization from first launch and the first appearance switch. Both
source and PyInstaller builds validate the manifest, exact image names and PNGs;
missing, stale or damaged bundles fall back to drawing and the installed cache.

Regenerate from the repository root:

```powershell
.\.venv\Scripts\python.exe tools\build_theme.py
.\.venv\Scripts\python.exe tools\build_theme.py --check
```

Palette and image specification parameters participate in the signature. When
changing pixel rendering, compositing or PNG encoding in `luvbox/theme.py`, bump
`_RENDERER_REVISION` before regenerating. The revision also invalidates an older
installed cache when a fix keeps the application version number.

`build.ps1` regenerates these two explicit files before PyInstaller collects
them. The archives have fixed timestamps and are deterministic.
