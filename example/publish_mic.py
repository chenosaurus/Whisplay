#!/usr/bin/env python3
"""
Publish the Whisplay / WM8960 microphone to a LiveKit room.

Captures explicit 48 kHz PCM chunks with sounddevice, pushes them into a
LiveKit AudioSource, and publishes that source as a microphone track.

Usage:
  LIVEKIT_URL=wss://... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \\
    uv run python example/publish_mic.py

  uv run python example/publish_mic.py --list-devices
  uv run python example/publish_mic.py --device wm8960 --room-name whisplay
  uv run python example/publish_mic.py --device 2 --channel 0
"""

import argparse
import asyncio
import logging
import math
import os
import queue
import shutil
import sys
import time

import numpy as np
import sounddevice as sd

try:
    from livekit import api, rtc  # pyright: ignore[reportMissingImports]
except ImportError:
    api = None
    rtc = None


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_DEVICE_NAME = "wm8960"
DEFAULT_INPUT_CHANNELS = 2
DEFAULT_PUBLISH_CHANNELS = 1
DEFAULT_FRAME_MS = 20
DEFAULT_ROOM_NAME = "whisplay-mic"
DEFAULT_IDENTITY = "whisplay-publish-mic"


def require_livekit():
    if api is None or rtc is None:
        raise RuntimeError("LiveKit Python packages are required. Install with: uv sync")


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
            return index, sd.query_devices(index, "input")

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


def resolve_token(args):
    token = args.token or os.getenv("LIVEKIT_TOKEN")
    if token:
        return token

    api_key = args.api_key or os.getenv("LIVEKIT_API_KEY")
    api_secret = args.api_secret or os.getenv("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "provide --token/LIVEKIT_TOKEN, or provide "
            "--api-key/LIVEKIT_API_KEY and --api-secret/LIVEKIT_API_SECRET"
        )

    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(args.identity)
        .with_name(args.identity)
        .with_grants(api.VideoGrants(room_join=True, room=args.room_name))
        .to_jwt()
    )


def select_publish_samples(indata, publish_channels, channel, mix_mono):
    if publish_channels == 1:
        if mix_mono and indata.shape[1] > 1:
            mixed = np.mean(indata.astype(np.int32), axis=1)
            return np.clip(mixed, -32768, 32767).astype(np.int16)

        selected_channel = 0 if channel is None else channel
        return np.ascontiguousarray(indata[:, selected_channel])

    return np.ascontiguousarray(indata[:, :publish_channels])


def make_audio_callback(args, audio_queue, meter_queue):
    def audio_callback(indata, frames, callback_time, status):
        del frames, callback_time
        if status and args.verbose:
            print(f"\nPortAudio status: {status}", file=sys.stderr)

        samples = select_publish_samples(
            indata,
            publish_channels=args.publish_channels,
            channel=args.channel,
            mix_mono=args.mix_mono,
        )
        samples = np.ascontiguousarray(samples, dtype=np.int16)

        rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32) / 32768.0))))
        peak = float(np.max(np.abs(samples.astype(np.float32) / 32768.0)))

        chunk = samples.tobytes()
        try:
            audio_queue.put_nowait(chunk)
        except queue.Full:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            audio_queue.put_nowait(chunk)

        try:
            meter_queue.put_nowait((rms, peak))
        except queue.Full:
            pass

    return audio_callback


async def publish_audio_chunks(source, audio_queue, args, running):
    bytes_per_sample = np.dtype(np.int16).itemsize
    while running.is_set():
        chunk = await asyncio.to_thread(audio_queue.get)
        samples_per_channel = len(chunk) // (args.publish_channels * bytes_per_sample)
        if samples_per_channel == 0:
            continue

        frame = rtc.AudioFrame(
            chunk,
            sample_rate=args.sample_rate,
            num_channels=args.publish_channels,
            samples_per_channel=samples_per_channel,
        )
        await source.capture_frame(frame)


async def render_meter(meter_queue, running, update_hz):
    update_interval = 1.0 / update_hz
    smoothed_rms = 0.0
    last_peak = 0.0
    last_render = 0.0

    while running.is_set():
        try:
            rms, peak = await asyncio.to_thread(meter_queue.get, True, 0.25)
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


async def run(args):
    require_livekit()

    url = args.url or os.getenv("LIVEKIT_URL")
    if not url:
        raise RuntimeError("provide --url or LIVEKIT_URL")

    token = resolve_token(args)
    device_index, device = find_input_device(args.device)
    max_input_channels = int(device["max_input_channels"])
    input_channels = args.input_channels or min(DEFAULT_INPUT_CHANNELS, max_input_channels)

    if input_channels < 1:
        raise RuntimeError("Input device reports no capture channels")
    if input_channels > max_input_channels:
        raise RuntimeError(
            f"Requested {input_channels} input channels, but [{device_index}] "
            f"{device['name']} only has {max_input_channels}"
        )
    if args.publish_channels < 1:
        raise RuntimeError("--publish-channels must be positive")
    if args.publish_channels > input_channels:
        raise RuntimeError("--publish-channels cannot exceed opened --input-channels")
    if args.channel is not None and args.channel >= input_channels:
        raise RuntimeError(f"--channel must be less than opened input channel count ({input_channels})")

    blocksize = max(1, int(args.sample_rate * args.frame_ms / 1000))
    audio_queue = queue.Queue(maxsize=args.queue_capacity)
    meter_queue = queue.Queue(maxsize=4)
    running = asyncio.Event()
    running.set()

    room = rtc.Room()
    source = rtc.AudioSource(
        args.sample_rate,
        args.publish_channels,
        queue_size_ms=args.source_queue_ms,
    )
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)

    @room.on("participant_connected")
    def on_participant_connected(participant):
        logging.info("participant connected: %s", participant.identity)

    @room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        logging.info("participant disconnected: %s", participant.identity)

    logging.info("connecting to LiveKit room '%s' as '%s'", args.room_name, args.identity)
    await room.connect(url, token, options=rtc.RoomOptions(auto_subscribe=False))
    logging.info("connected to room %s", room.name)

    publish_options = rtc.TrackPublishOptions()
    publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
    publication = await room.local_participant.publish_track(track, publish_options)
    logging.info("published microphone track %s", publication.sid)

    print(
        f"Capturing [{device_index}] {device['name']} at {args.sample_rate} Hz, "
        f"{input_channels} input channel(s), publishing {args.publish_channels} channel(s)"
    )
    if args.publish_channels == 1 and not args.mix_mono:
        print(f"Publishing input channel {0 if args.channel is None else args.channel}")
    print("Press Ctrl+C to stop.\n")

    publish_task = asyncio.create_task(publish_audio_chunks(source, audio_queue, args, running))
    meter_task = asyncio.create_task(render_meter(meter_queue, running, args.update_hz))

    try:
        with sd.InputStream(
            device=device_index,
            channels=input_channels,
            samplerate=args.sample_rate,
            dtype="int16",
            blocksize=blocksize,
            callback=make_audio_callback(args, audio_queue, meter_queue),
        ):
            while True:
                await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        running.clear()
        publish_task.cancel()
        meter_task.cancel()
        for task in (publish_task, meter_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await room.disconnect()


def build_parser():
    parser = argparse.ArgumentParser(description="Publish a WM8960 microphone to a LiveKit room.")
    parser.add_argument("--list-devices", action="store_true", help="List input devices and exit.")
    parser.add_argument("--url", help="LiveKit server URL. Can also be set with LIVEKIT_URL.")
    parser.add_argument("--token", help="LiveKit access token. Can also be set with LIVEKIT_TOKEN.")
    parser.add_argument("--api-key", help="LiveKit API key. Can also be set with LIVEKIT_API_KEY.")
    parser.add_argument("--api-secret", help="LiveKit API secret. Can also be set with LIVEKIT_API_SECRET.")
    parser.add_argument("--room-name", default=DEFAULT_ROOM_NAME, help="LiveKit room name.")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help="LiveKit participant identity.")
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
        help="Capture and publish sample rate in Hz.",
    )
    parser.add_argument(
        "--input-channels",
        type=int,
        default=None,
        help="Channels to open from the input device. Defaults to 2 when available.",
    )
    parser.add_argument(
        "--publish-channels",
        type=int,
        default=DEFAULT_PUBLISH_CHANNELS,
        help="Number of channels to publish to LiveKit.",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=0,
        help="Zero-based input channel to publish when publishing mono.",
    )
    parser.add_argument(
        "--mix-mono",
        action="store_true",
        help="Mix all opened input channels down to mono instead of selecting --channel.",
    )
    parser.add_argument(
        "--frame-ms",
        type=float,
        default=DEFAULT_FRAME_MS,
        help="Audio frame size in milliseconds.",
    )
    parser.add_argument(
        "--source-queue-ms",
        type=int,
        default=100,
        help="LiveKit AudioSource queue size in milliseconds.",
    )
    parser.add_argument(
        "--queue-capacity",
        type=int,
        default=8,
        help="Max captured chunks queued between sounddevice and LiveKit.",
    )
    parser.add_argument(
        "--update-hz",
        type=float,
        default=20.0,
        help="CLI meter refresh rate.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print PortAudio callback statuses.")
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = build_parser().parse_args()

    if args.list_devices:
        list_devices()
        return
    if args.sample_rate <= 0:
        raise SystemExit("--sample-rate must be positive")
    if args.input_channels is not None and args.input_channels <= 0:
        raise SystemExit("--input-channels must be positive")
    if args.publish_channels <= 0:
        raise SystemExit("--publish-channels must be positive")
    if args.channel is not None and args.channel < 0:
        raise SystemExit("--channel must be zero or greater")
    if args.frame_ms <= 0:
        raise SystemExit("--frame-ms must be positive")
    if args.source_queue_ms <= 0:
        raise SystemExit("--source-queue-ms must be positive")
    if args.queue_capacity <= 0:
        raise SystemExit("--queue-capacity must be positive")
    if args.update_hz <= 0:
        raise SystemExit("--update-hz must be positive")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
