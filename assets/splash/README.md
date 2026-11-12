# LuvBox startup splash

The picture `LuvBox.exe` shows while it prepares and opens the application:
a near-black card, a red glow and the icon's heart outline with the name.

`tools/draw_splash.py` draws three layers at twice the size they are shown:

- `card.png`: the rounded card and its soft shadow (the corners are transparent)
- `glow.png`: the red light behind the heart; the launcher varies its strength
- `mark.png`: the heart, the word LuvBox and the line under it

The launcher (`tools/launcher/Launcher.cs`) stacks them and draws the changing
lines, the progress bar and the version itself. Sizes, positions and all the
wording are in `tools/launcher/splash.ini`, read by both.

Redraw with `.venv/Scripts/python.exe tools/draw_splash.py`. Add
`--preview .test-output/splash/preview.png` to see the composed result.
