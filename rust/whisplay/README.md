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

Both examples need access to `/dev/spidev*` and `/dev/gpiochip*`, so they may
need to run as root depending on device permissions.

Full-screen frames are split into 4096-byte SPI writes to work with the default
Linux spidev transfer-size limit.
