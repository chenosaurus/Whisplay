#!/usr/bin/env python3
"""
Simple WM8960 microphone VU meter.

Opens an input device at 48 kHz and renders a live CLI level meter.

Usage:
  uv run python example/wm8960_uv_meter.py
  uv run python example/wm8960_uv_meter.py --list-devices
  uv run python example/wm8960_uv_meter.py --device wm8960
  uv run python example/wm8960_uv_meter.py --device 2 --channels 2
"""

import argparse
import math
import queue
import shutil
import sys
import time

import numpy as np
import sounddevice as sd


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_DEVICE_NAME = "wm8960"
DEFAULT_CHANNELS = 2
DEFAULT_BLOCK_MS = 20
DEFAULT_UPDATE_HZ = 20


def dbfs(value):
    if value <= 0.0:
        return -120.0
    return 20.0 * math.log10(value)


def meter_bar(value, width):
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "#" * filled + "-" * (width - filled)


def list_devices():
    devices = sd.query_devices()
    default_input = sd.default.device[0]

    print("Available input devices:")
    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        marker = " (default)" if index == default_input else ""
        print(
            f"  [{index}] {device['name']}{marker} - "
            f"{device['max_input_channels']}ch @ {device['default_samplerate']:.0f}Hz"
        )


def find_input_device(requested):
    devices = sd.query_devices()
    default_input = sd.default.device[0]

    if requested:
        if requested.isdigit():
            index = int(requested)
            device = sd.query_devices(index, "input")
            return index, device

        lowered = requested.lower()
        for index, device in enumerate(devices):
            if device["max_input_channels"] > 0 and lowered in device["name"].lower():
                return index, device
        raise SystemExit(f"Input device containing '{requested}' not found")

    for index, device in enumerate(devices):
        if device["max_input_channels"] > 0 and DEFAULT_DEVICE_NAME in device["name"].lower():
            return index, device

    if default_input is None or default_input < 0:
        raise SystemExit("No default input device and no WM8960 input device found")
    return default_input, sd.query_devices(default_input, "input")


def int16_to_float(samples):
    return samples.astype(np.float32) / 32768.0


def run_meter(args):
    device_index, device = find_input_device(args.device)
    max_channels = int(device["max_input_channels"])
    channels = args.channels or min(DEFAULT_CHANNELS, max_channels)
    if channels < 1:
        raise SystemExit("Input device reports no capture channels")
    if channels > max_channels:
        raise SystemExit(
            f"Requested {channels} channels, but [{device_index}] {device['name']} "
            f"only has {max_channels}"
        )
    if args.channel is not None and args.channel >= channels:
        raise SystemExit(f"--channel must be less than opened channel count ({channels})")

    blocksize = max(1, int(args.sample_rate * args.block_ms / 1000))
    levels = queue.Queue(maxsize=4)

    def audio_callback(indata, frames, callback_time, status):
        del frames, callback_time
        if status and args.verbose:
            print(f"\nPortAudio status: {status}", file=sys.stderr)

        samples = int16_to_float(indata)
        if args.channel is not None:
            samples = samples[:, args.channel]

        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0

        try:
            levels.put_nowait((rms, peak))
        except queue.Full:
            try:
                levels.get_nowait()
            except queue.Empty:
                pass
            levels.put_nowait((rms, peak))

    print(
        f"Opening [{device_index}] {device['name']} at "
        f"{args.sample_rate} Hz, {channels} channel(s)"
    )
    print("Press Ctrl+C to stop.\n")

    update_interval = 1.0 / args.update_hz
    smoothed_rms = 0.0
    last_peak = 0.0
    last_render = 0.0

    try:
        with sd.InputStream(
            device=device_index,
            channels=channels,
            samplerate=args.sample_rate,
            dtype="int16",
            blocksize=blocksize,
            callback=audio_callback,
        ):
            while True:
                try:
                    rms, peak = levels.get(timeout=0.25)
                except queue.Empty:
                    rms, peak = 0.0, 0.0

                smoothed_rms = (smoothed_rms * 0.75) + (rms * 0.25)
                last_peak = max(peak, last_peak * 0.85)

                now = time.monotonic()
                if now - last_render < update_interval:
                    continue
                last_render = now

                columns = shutil.get_terminal_size((80, 20)).columns
                bar_width = max(12, min(50, columns - 43))
                clipping = " CLIP" if last_peak >= 0.99 else "     "
                line = (
                    f"\rRMS [{meter_bar(smoothed_rms, bar_width)}] "
                    f"{dbfs(smoothed_rms):6.1f} dBFS  "
                    f"Peak {dbfs(last_peak):6.1f} dBFS{clipping}"
                )
                print(line[: columns - 1], end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopped.")


def build_parser():
    parser = argparse.ArgumentParser(description="Open a WM8960 mic input and show a CLI VU meter.")
    parser.add_argument("--list-devices", action="store_true", help="List input devices and exit.")
    parser.add_argument(
        "-d",
        "--device",
        help="Input device index or name substring. Defaults to the first WM8960 input.",
    )
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Capture sample rate in Hz.",
    )
    parser.add_argument(
        "-c",
        "--channels",
        type=int,
        default=None,
        help="Channels to open. Defaults to 2 when available.",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=None,
        help="Display only one zero-based channel from the opened input.",
    )
    parser.add_argument(
        "--block-ms",
        type=float,
        default=DEFAULT_BLOCK_MS,
        help="Audio callback block size in milliseconds.",
    )
    parser.add_argument(
        "--update-hz",
        type=float,
        default=DEFAULT_UPDATE_HZ,
        help="CLI meter refresh rate.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print PortAudio callback statuses.")
    return parser


def main():
    args = build_parser().parse_args()
    if args.list_devices:
        list_devices()
        return
    if args.sample_rate <= 0:
        raise SystemExit("--sample-rate must be positive")
    if args.channels is not None and args.channels <= 0:
        raise SystemExit("--channels must be positive")
    if args.channel is not None and args.channel < 0:
        raise SystemExit("--channel must be zero or greater")
    if args.block_ms <= 0:
        raise SystemExit("--block-ms must be positive")
    if args.update_hz <= 0:
        raise SystemExit("--update-hz must be positive")

    run_meter(args)


if __name__ == "__main__":
    main()
