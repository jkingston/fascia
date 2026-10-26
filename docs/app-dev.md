# App development

Start from Counter inside this repository:

```sh
mkdir -p examples/my-app && cp examples/hello/{main.zig,build.zig,build.zig.zon,counter.desktop,counter.png} examples/my-app/
```

Rename `counter` in the filenames and `build.zig`; update `Name`, `Icon` and
`X-Fascia-Version` in the desktop entry. Edit `main.zig`: `view` declares the UI;
`update` handles touch and state. The SDK lives in `sdk/`.

```sh
mise install
cd examples/my-app
mise exec -- zig build dev             # desktop preview with rebuild/restart
mise exec -- zig build --release=fast  # zig-out/my-app.AppDir
```

Compile errors leave the previous preview running. Saved data survives restarts.
Mouse input acts as touch; Escape closes the preview. F12 or ⌘/Ctrl-S captures a
screenshot in `.local/preview/state/`. Linux preview needs X11 development headers.

From the repository root:

```sh
python3 tools/deploy.py examples/my-app/zig-out/my-app.AppDir fascia@fascia-m7k3p.local
```

Deployment installs and opens the app over SSH. The physical button returns Home.
`fascia app rollback my-app` restores the previous release;
`fascia app uninstall my-app` keeps saved data unless `--delete-state` is given.

## App contract

An AppDir contains executable `AppRun`, one `<id>.desktop`, `<id>.png` and `.DirIcon`
(a copy or relative link to the icon). Use `Type=Application`, `Exec=AppRun`,
`Name`, `Icon=<id>` and `X-Fascia-Version`; `Comment` is optional. IDs use lowercase
letters, digits and hyphens, up to 64 characters. Keep assets and libraries inside
the directory. `AppRun` is ARM Linux ELF or a script with an installed interpreter.

Any language can use fullscreen Wayland `xdg-shell`; the SDK is optional.
The working directory is the installed AppDir. Use the supplied absolute
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` paths.
Handle SIGTERM for orderly shutdown; after two seconds systemd kills remaining
processes. Open a window within eight seconds. App logs are in
`journalctl --user -u fascia-app-<id>.service`.

Apps run as `fascia` without a sandbox. Clock and Counter are removable examples,
installed once and excluded from OS updates.
