# LuvBox startup splash

The picture `LuvBox.exe` shows while it prepares and opens the application:
a near-black card, a red glow and the icon's heart outline with the name.

`tools/draw_splash.py` draws three layers at twice the size they are shown:

- `card.png`: the rounded card and its soft shadow (the corners are transparent)
- `glow.png`: the red light behind the heart, at the strength it is shown (it does not move)
- `mark.png`: the heart, the word LuvBox and the line under it

The launcher (`tools/launcher/Launcher.cs`) stacks them once, draws one line and
the version, and then only fades the whole card in and out; nothing on it
moves. The line is chosen when the card appears and stays until the window
opens: "새 버전으로 업데이트하는 중" when this version is not unpacked yet and an
earlier one is, "보관함을 여는 중" for any other start. Sizes, positions and all
the wording are in `tools/launcher/splash.ini`, read by both.

Redraw with `.venv/Scripts/python.exe tools/draw_splash.py`. Add
`--preview .test-output/splash/preview.png` to see the composed result.
