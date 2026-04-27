#!/usr/bin/env python3
"""
Publish the Whisplay / WM8960 microphone to a LiveKit room and play room audio.

Uses LiveKit's MediaDevices helper for microphone capture, output playback, and
audio processing features such as AEC, noise suppression, and AGC.

Usage:
  LIVEKIT_URL=wss://... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \\
    uv run python example/publish_mic.py

  uv run python example/publish_mic.py --list-devices
  uv run python example/publish_mic.py --input-device wm8960 --output-device wm8960
  uv run python example/publish_mic.py --input-device 2 --output-device 2 --channel 0
  uv run python example/publish_mic.py --no-playback
"""

import argparse
import asyncio
import logging
import math
import os
import queue
import shutil
import time

try:
    from livekit import api, rtc  # pyright: ignore[reportMissingImports]
except ImportError:
    api = None
    rtc = None


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 1
DEFAULT_DEVICE_NAME = "wm8960"
DEFAULT_ROOM_NAME = "whisplay-mic"
DEFAULT_IDENTITY = "whisplay-publish-mic"
DEFAULT_INPUT_QUEUE_CAPACITY = 100


def require_livekit():
    if api is None or rtc is None:
        raise RuntimeError("LiveKit Python packages are required. Install with: uv sync")
    if not hasattr(rtc, "MediaDevices"):
        raise RuntimeError(
            "This example requires a recent livekit package with rtc.MediaDevices. "
            "Upgrade with: uv sync --upgrade-package livekit"
        )


def dbfs(value):
    if value <= 0.0:
        return -120.0
    return 20.0 * math.log10(value)


def meter_bar(value, width):
    value = max(0.0, min(1.0, value))
    filled = int(round(value * width))
    return "#" * filled + "-" * (width - filled)


def calculate_level(samples):
    if not samples:
        return 0.0, 0.0
    peak = max(abs(int(sample)) for sample in samples) / 32767.0
    square_sum = sum(int(sample) * int(sample) for sample in samples)
    rms = math.sqrt(square_sum / len(samples)) / 32767.0
    return max(0.0, min(1.0, rms)), max(0.0, min(1.0, peak))


def list_audio_devices():
    require_livekit()
    devices = rtc.MediaDevices()
    default_input = devices.default_input_device()
    default_output = devices.default_output_device()

    print("Available audio input devices:")
    inputs = devices.list_input_devices()
    if not inputs:
        print("   no input devices found")
    for dev in inputs:
        marker = " (default)" if dev["index"] == default_input else ""
        print(
            f"  [{dev['index']}] {dev['name']}{marker} - "
            f"{dev['max_input_channels']}ch @ {dev['default_samplerate']:.0f}Hz"
        )

    print("\nAvailable audio output devices:")
    outputs = devices.list_output_devices()
    if not outputs:
        print("   no output devices found")
    for dev in outputs:
        marker = " (default)" if dev["index"] == default_output else ""
        print(
            f"  [{dev['index']}] {dev['name']}{marker} - "
            f"{dev['max_output_channels']}ch @ {dev['default_samplerate']:.0f}Hz"
        )


def device_name_for_index(listing, index):
    for dev in listing:
        if dev["index"] == index:
            return dev["name"]
    return "system default" if index is None else f"device {index}"


def select_device(devices, direction, requested):
    listing = devices.list_input_devices() if direction == "input" else devices.list_output_devices()
    default_index = (
        devices.default_input_device() if direction == "input" else devices.default_output_device()
    )

    def find_by_name(name):
        lowered = name.lower()
        for dev in listing:
            if lowered in dev["name"].lower():
                return dev
        return None

    if requested:
        if requested.isdigit():
            index = int(requested)
            for dev in listing:
                if dev["index"] == index:
                    logging.info("using requested %s device [%s] %s", direction, index, dev["name"])
                    return index, dev["name"]
            raise RuntimeError(f"{direction} device index {index} not found")

        dev = find_by_name(requested)
        if not dev:
            raise RuntimeError(f"{direction} device containing '{requested}' not found")
        logging.info("using requested %s device [%s] %s", direction, dev["index"], dev["name"])
        return dev["index"], dev["name"]

    dev = find_by_name(DEFAULT_DEVICE_NAME)
    if dev:
        logging.info("auto-selected WM8960 %s device [%s] %s", direction, dev["index"], dev["name"])
        return dev["index"], dev["name"]

    logging.warning("WM8960 %s device not found; using system default", direction)
    return default_index, device_name_for_index(listing, default_index)


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


async def monitor_mic_meter(track, meter_queue, sample_rate, running):
    stream = rtc.AudioStream(track, sample_rate=sample_rate, num_channels=DEFAULT_CHANNELS)
    try:
        async for event in stream:
            if not running.is_set():
                break

            rms, peak = calculate_level(list(event.frame.data))
            try:
                meter_queue.put_nowait((rms, peak))
            except queue.Full:
                try:
                    meter_queue.get_nowait()
                except queue.Empty:
                    pass
                meter_queue.put_nowait((rms, peak))
    finally:
        await stream.aclose()


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
            f"\rMic [{meter_bar(smoothed_rms, bar_width)}] "
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
    devices = rtc.MediaDevices(
        input_sample_rate=args.sample_rate,
        output_sample_rate=args.sample_rate,
        num_channels=DEFAULT_CHANNELS,
    )
    room = rtc.Room()
    meter_queue = queue.Queue(maxsize=4)
    running = asyncio.Event()
    running.set()

    input_device, input_device_name = select_device(devices, "input", args.input_device)
    output_device = None
    output_device_name = "playback disabled"
    if not args.no_playback:
        output_device, output_device_name = select_device(devices, "output", args.output_device)

    player = None
    mic = None
    meter_task = None
    render_task = None
    remote_tracks = set()

    async def add_remote_track(track, participant_identity):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        logging.info("subscribed to audio from %s", participant_identity)
        if player is not None:
            await player.add_track(track)
            remote_tracks.add(getattr(track, "sid", str(id(track))))

    async def remove_remote_track(track, participant_identity):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        logging.info("unsubscribed from audio of %s", participant_identity)
        if player is not None:
            await player.remove_track(track)
            remote_tracks.discard(getattr(track, "sid", str(id(track))))

    @room.on("participant_connected")
    def on_participant_connected(participant):
        logging.info("participant connected: %s", participant.identity)

    @room.on("participant_disconnected")
    def on_participant_disconnected(participant):
        logging.info("participant disconnected: %s", participant.identity)

    @room.on("track_subscribed")
    def on_track_subscribed(track, _publication, participant):
        asyncio.create_task(add_remote_track(track, participant.identity))

    @room.on("track_unsubscribed")
    def on_track_unsubscribed(track, _publication, participant):
        asyncio.create_task(remove_remote_track(track, participant.identity))

    try:
        mic = devices.open_input(
            enable_aec=args.echo_cancellation,
            noise_suppression=args.noise_suppression,
            high_pass_filter=args.high_pass_filter,
            auto_gain_control=args.auto_gain_control,
            input_device=input_device,
            queue_capacity=args.input_queue_capacity,
            input_channel_index=args.channel,
        )
        if output_device is not None:
            player = devices.open_output(output_device=output_device)

        logging.info("connecting to LiveKit room '%s' as '%s'", args.room_name, args.identity)
        await room.connect(url, token, options=rtc.RoomOptions(auto_subscribe=True))
        logging.info("connected to room %s", room.name)

        track = rtc.LocalAudioTrack.create_audio_track("mic", mic.source)
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
        publication = await room.local_participant.publish_track(track, publish_options)
        logging.info("published microphone track %s", publication.sid)

        if player is not None:
            await player.start()
            logging.info("room audio playback started")

        print(f"Mic: {input_device_name} @ {args.sample_rate} Hz")
        print(f"Speaker: {output_device_name}")
        print(
            "Audio processing: "
            f"AEC={'on' if args.echo_cancellation else 'off'}, "
            f"NS={'on' if args.noise_suppression else 'off'}, "
            f"HPF={'on' if args.high_pass_filter else 'off'}, "
            f"AGC={'on' if args.auto_gain_control else 'off'}"
        )
        print("Press Ctrl+C to stop.\n")

        meter_task = asyncio.create_task(
            monitor_mic_meter(track, meter_queue, args.sample_rate, running)
        )
        render_task = asyncio.create_task(render_meter(meter_queue, running, args.update_hz))

        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        running.clear()
        tasks = [task for task in (meter_task, render_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if mic is not None:
            await mic.aclose()
        if player is not None:
            await player.aclose()
        await room.disconnect()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Publish a WM8960 microphone to LiveKit and play room audio."
    )
    parser.add_argument("--list-devices", action="store_true", help="List input/output devices and exit.")
    parser.add_argument("--url", help="LiveKit server URL. Can also be set with LIVEKIT_URL.")
    parser.add_argument("--token", help="LiveKit access token. Can also be set with LIVEKIT_TOKEN.")
    parser.add_argument("--api-key", help="LiveKit API key. Can also be set with LIVEKIT_API_KEY.")
    parser.add_argument("--api-secret", help="LiveKit API secret. Can also be set with LIVEKIT_API_SECRET.")
    parser.add_argument("--room-name", default=DEFAULT_ROOM_NAME, help="LiveKit room name.")
    parser.add_argument("--identity", default=DEFAULT_IDENTITY, help="LiveKit participant identity.")
    parser.add_argument(
        "-i",
        "--input-device",
        help="Input device index or name substring. Defaults to the first WM8960 input.",
    )
    parser.add_argument(
        "-o",
        "--output-device",
        help="Output device index or name substring. Defaults to the first WM8960 output.",
    )
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Capture/playback sample rate in Hz.",
    )
    parser.add_argument(
        "--channel",
        type=int,
        default=0,
        help="Zero-based input channel to capture as mono.",
    )
    parser.add_argument(
        "--input-queue-capacity",
        type=int,
        default=DEFAULT_INPUT_QUEUE_CAPACITY,
        help="Max queued mic frames before dropping.",
    )
    parser.add_argument("--no-playback", action="store_true", help="Disable remote room audio playback.")
    parser.add_argument("--echo-cancellation", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--noise-suppression", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--high-pass-filter", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--auto-gain-control", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--update-hz",
        type=float,
        default=20.0,
        help="CLI meter refresh rate.",
    )
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = build_parser().parse_args()

    if args.list_devices:
        list_audio_devices()
        return
    if args.sample_rate <= 0:
        raise SystemExit("--sample-rate must be positive")
    if args.channel is not None and args.channel < 0:
        raise SystemExit("--channel must be zero or greater")
    if args.input_queue_capacity <= 0:
        raise SystemExit("--input-queue-capacity must be positive")
    if args.update_hz <= 0:
        raise SystemExit("--update-hz must be positive")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
