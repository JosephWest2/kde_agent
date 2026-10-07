# Target applications

Notes on the applications agents test in the private desktop: how they start,
what gets in the way, and keyboard paths that worked. They come from the optional
application tests (`tests/integration/apps.py`, see [TESTING.md](TESTING.md#application-tests)),
run on Arch Linux with KWin 6, an AMD GPU and these versions:

| Application | Version | Toolkit | First window | Decorations |
| --- | --- | --- | --- | --- |
| gnome-text-editor | 50.1 | GTK4 / libadwaita | about 0.5s | client-side: `client` equals `frame`, header bar inside the client area |
| GIMP | 3.2.6 | GTK3 | about 5s | KWin title bar: `frame` is 36px taller than `client`, which starts below it |
| Blender | 5.2.2 LTS | GHOST (Wayland), OpenGL | about 0.3s | KWin title bar, as for GIMP |

All three run natively on Wayland in the private session; none needs X11 or
XWayland. Applications get the session's private HOME and XDG directories, so
their settings, caches and recent files never touch your own
([environment policy](ARTIFACTS.md)). They can still read and write any path you
give them, such as an export path.

General points they share:

- The output is 1280×720. Windows and dialogs can be larger than that or extend
  past its edge. Points outside the screen are refused, and `screenshot --window`
  is clipped to the screen, so use the keyboard for what you can't see.
- A dialog is a separate `window` row of the same app (see
  [modal dialogs](AGENT_RECIPES.md#modal-dialogs)). Find it by polling
  `windows --app` for its title, send keys to its ref, and confirm it closed with
  `wait --for gone`.
- KWin centers dialogs over their parent, which can put them on a half pixel
  (`"y": 45.5`). Client coordinates still work as usual.
- `type` only sends characters the US layout has. Others fail with
  `unsupported_input` before anything is sent.

## gnome-text-editor

```sh
$D launch --wait-window --cwd "$WORK" -- gnome-text-editor
```

- No first-run dialog. A new document's title is `New Document (Draft) - Text Editor`
  and, once you type, the first few words of the first line plus `(Draft)`.
- **Save:** `ctrl+s` opens a `Save a File` window (GTK file chooser, about
  1030×372) with the name field focused and the name selected. `ctrl+a`, then
  `type` an absolute path, then `return`. The title becomes
  `saved.txt (/the/folder) - Text Editor`. Later `ctrl+s` saves in place with no
  dialog.
- The saved file ends with a newline the editor doesn't show.
- Return keeps the current line's indentation, so typed text that starts lines
  with spaces gets extra indentation on the following lines.

## GIMP 3

```sh
printf '(config-version "3.2.6")\n(show-welcome-dialog no)\n(check-updates no)\n' > "$WORK/gimprc"
$D launch --wait-window --timeout 60 --cwd "$WORK" --env GIMP3_DIRECTORY="$WORK/gimp-profile" \
  -- gimp -n -s -c -g "$WORK/gimprc"
```

- `GIMP3_DIRECTORY` holds the whole profile (preferences, brushes, session,
  recent files). `-n` starts a new instance, `-s` skips the splash, `-c` sends
  messages to the console (the app's log) instead of message dialogs, and
  `-g FILE` reads that file as the user gimprc.
- Startup takes about 5s with a fresh profile. Give `launch` a longer `--timeout`.
- **Welcome dialog:** with a new profile (and after an upgrade) GIMP opens a
  `Welcome to GIMP` window 1–2s *after* the main window, and it takes focus, so
  input meant for the main window or another dialog can fail with `target_lost`
  or land in the welcome window. `escape` closes it. To avoid it, set `config-version` to the
  installed version (from `gimp --version`) and `show-welcome-dialog` to `no` in the
  gimprc, as above. `show-welcome-dialog no` on its own is not enough.
- **New image:** `ctrl+n` opens `Create a New Image` with the width field focused.
  `ctrl+a`, type the width, `tab`, `ctrl+a`, type the height, `return` (OK).
  The title then includes the size, for example `... 320x240 – GIMP`, and starts
  with `*` once the image is changed.
- The main window starts at 800×600. Opening a large image (the 1920×1080
  default) grows it to about 1083×691, past the bottom and right of the screen.
  Small images open at 100% zoom, centered in the canvas.
- **Painting:** the default tool is the paintbrush with a black foreground. One
  `click` on the image paints one dab (dark core about 12px with the default
  51px brush). There is no tool-free way to find the canvas: the test finds the
  white image in a window screenshot.
- **Export:** `ctrl+shift+e` opens `Export Image`, a GTK file chooser of about
  1351×996, larger than the screen, so its buttons are off screen. The name field
  is focused with the name selected: `ctrl+a`, `type` an absolute path ending in
  `.png`, `return`. Then `Export Image as PNG` opens; `return` exports. The title
  then includes `(exported)`.
- **Quitting:** exporting doesn't count as saving. `ctrl+q` (or `close`) opens
  `Quit GIMP` ("one image with unsaved changes"), and `close` times out
  (`timeout`, phase `exit_wait`) while it is open. `ctrl+d` in it discards and
  quits; `wait --for exit --app APP` confirms.
- A tooltip shows up as an untitled `popup` row when the pointer rests over a
  widget, for example over the quit dialog's image list.

## Blender

```sh
# Once per profile: a userpref.blend stops the first-run quick-setup splash.
BLENDER_USER_RESOURCES="$WORK/blender-user" blender -b --factory-startup \
  --python-expr 'import bpy; bpy.ops.wm.save_userpref()'
$D launch --wait-window --cwd "$WORK" --env BLENDER_USER_RESOURCES="$WORK/blender-user" \
  -- blender --factory-startup --offline-mode "$WORK/scene.blend"
```

- Blender uses its Wayland backend and the GPU: OpenGL through EGL on the render
  node (`/dev/dri/renderD128`, Mesa), not software rendering. The window gets KWin
  server-side decorations (libdecor isn't used) and fills the screen: client
  1280×684 at y 36. The first window appears in about 0.3s, titled
  `(Unsaved) - Blender 5.2.2 LTS` until a file given on the command line loads.
  `[ALSOFT] ... PipeWire` in its log is harmless (no audio in the session).
- `BLENDER_USER_RESOURCES` holds its config (preferences, recent files,
  bookmarks), scripts and extensions; caches go to the private
  `XDG_CACHE_HOME`. `--offline-mode` keeps it off the network.
- **Splash screens:** without a `userpref.blend` in the user config Blender shows
  the quick-setup splash, even with `--factory-startup` and even over a file opened
  from the command line. With one, the regular splash still shows unless a file
  was opened. Both are drawn inside the main window, so `windows` doesn't list
  them. `escape` closes them, but they appear a moment after the window, so prevent
  them as above rather than racing them.
- **Keys go to the area under the pointer**, not to a focused widget. `move` the
  pointer into the 3D viewport before viewport shortcuts.
- Menus, search and pop-ups (`shift+a`, F3) are drawn inside the window: no
  `popup` rows. A menu opens under the pointer and the item under the pointer is
  highlighted, so after typing a search, `return` picks whatever the pointer is
  on. The test uses `shift+d` (duplicate) then `return` (confirm the move), which
  doesn't depend on the pointer, instead of the Add menu.
- Titles: `scene [/path/scene.blend] - Blender 5.2.2 LTS`, with a leading `* `
  when there are unsaved changes.
- **Save As:** `ctrl+shift+s` opens `Blender File View`, its own window (client
  1060×600, frame 1060×636), in the current file's folder (the private HOME for a
  new file). Double-click the filename field along the bottom (about 40% across,
  21px from the bottom), `ctrl+a`, `type` a plain file name, and press `return`
  twice: the first ends the edit, the second saves. The name can't contain a
  folder (Blender turns `/` into `_`). To change folders, edit the path field at
  the top (about x 550, y 20; x 300 is the parent-folder button).
- **The return that closes the file view also reaches the main window,** and
  presses the button under the pointer there (in our run the timeline's jump to
  the end frame, which changed the scene). Move the pointer over the file list,
  which lies over the 3D viewport, before that last `return`.
- A saved file closes with `close` right away. With unsaved changes Blender asks
  inside its window first.
- To check a saved file without the GUI, run Blender in the background on it
  (about 0.4s). Put the file before `--python-expr`, or the expression runs on the
  default scene:

  ```sh
  blender -b --factory-startup "$WORK/edited.blend" \
    --python-expr 'import bpy; print(sorted(o.name for o in bpy.data.objects))'
  ```
