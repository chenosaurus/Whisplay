# WhisPlay Rust LCD Driver

Native Rust LCD driver for the PiSugar WhisPlay HAT, focused on fast RGB565
frame writes over Linux `spidev`.

## Supported Boards

- Raspberry Pi with 40-pin header
- Radxa ZERO 3W
- Radxa Cubie A7Z

The driver mirrors the Python platform detection in `Driver/WhisPlay.py` and
uses `/proc/device-tree/model` plus `/proc/device-tree/compatible` to choose
GPIO chip/line mappings and SPI bus settings.

## Examples

Run from this directory on the target device:

```bash
cargo run --release --example frame_bench -- 300
```

This pushes generated full-screen frames and reports average frame write time,
p95 write time, and FPS.

```bash
cargo run --release --example play_mp4 -- ../../example/data/whisplay_test.mp4
```

This streams raw `rgb565be` frames from `ffmpeg` directly to the LCD, matching
the Python `example/play_mp4.py` video path.

```bash
cargo run --release --example avatar -- --emotion happy --fps 30
```

This animates the robot avatar from `example/avatar.py`, uses dirty-rectangle
LCD updates, and cycles emotions from the WhisPlay button or every five seconds.
Add `--speaking` to keep the mouth in its speech animation.

```bash
cargo run --features emulator --example avatar -- --emulated --emulator-scale 2
```

This opens a `minifb` emulator window instead of using the hardware display.
Press Space to cycle emotions, Esc to exit, and hold 1-5 to preview speech
mouth levels. The `emulator` feature is intentionally opt-in so device builds
do not compile the desktop windowing dependencies.

These examples need access to `/dev/spidev*` and `/dev/gpiochip*`, so they may
need to run as root depending on device permissions.

Full-screen frames are split into 4096-byte SPI writes to work with the default
Linux spidev transfer-size limit.
