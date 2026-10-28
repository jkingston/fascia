# Immich Photos

Immich 3.2+. Open Connection, enter the server URL and a read-only API key:
`asset.read`, `asset.view`, `person.read`, `face.read`, `album.read`.

Choose albums and people. Include matches **any** or **all** selected people;
Exclude always wins. Turning **Other people allowed** off checks recognised faces, not
undetected people. Then & now alternates full-screen photos of the same person
at least a year apart, using Immich's dates unchanged.

**Fit** preserves the whole photo. **Fill** crops to the screen. **Smart fill**
keeps detected faces visible, falling back to Fit when necessary.
Choose switching intervals from 30 seconds to one hour in Display.

Tap the sides to browse; tap the centre for controls. Browsing pauses playback.
Resume continues the slideshow. Done saves settings and returns to photos. The power button returns Home.

```sh
mise exec -- zig build dev
mise exec -- zig build --release=fast
```

[Deploy](../../docs/app-dev.md). Settings and the API key stay in
`$XDG_CONFIG_HOME/settings.json`. Cached JPEG/PNG previews use at most 128 MiB.
Selections currently use bounded random samples. Cached
photos work offline. Original files and Immich metadata are never modified.

Tests: `zig build test`. With `XDG_CONFIG_HOME` pointing to private settings,
`zig build check-server` tests the API; `zig build check-ui` also writes private
screenshots and checks controls. `IMMICH_CHECK_OFFLINE=1` tests the saved cache.
