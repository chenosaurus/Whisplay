import argparse
import asyncio
import logging
import os
import signal
import threading
import time

from PIL import ImageDraw

from avatar import (
    AVATAR_DIRTY_RECT,
    EMULATED_SPEECH_LEVELS,
    FACE_SEQUENCE,
    RobotAvatar,
    create_board,
    cycle_emotion,
    rgb_to_rgb565be,
)

try:
    from livekit import api, rtc  # pyright: ignore[reportMissingImports]
except ImportError:
    api = None
    rtc = None


DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_CHANNELS = 1
DEFAULT_WM8960_NAME = "wm8960soundcard"


class SpeechLevels:
    def __init__(self):
        self._levels = {}
        self._lock = threading.Lock()

    def set(self, key, level):
        with self._lock:
            self._levels[key] = max(0.0, min(1.0, float(level)))

    def remove(self, key):
        with self._lock:
            self._levels.pop(key, None)

    def current(self):
        with self._lock:
            level = max(self._levels.values(), default=0.0)
        return level if level > 0.02 else None


class ClientStatus:
    def __init__(self):
        self._lock = threading.Lock()
        self._mic_device_name = "audio starting"
        self._mic_level = 0.0

    def set_mic_device(self, name):
        with self._lock:
            self._mic_device_name = name or "system default"

    def set_mic_level(self, level):
        with self._lock:
            self._mic_level = max(0.0, min(1.0, float(level)))

    def snapshot(self):
        with self._lock:
            return self._mic_device_name, self._mic_level


def calculate_speech_level(samples):
    if not samples:
        return 0.0
    average = sum(abs(int(sample)) for sample in samples) / len(samples)
    return max(0.0, min(1.0, average / 32767.0 * 4.0))


def level_bar(level, width=24):
    filled = round(max(0.0, min(1.0, level)) * width)
    return "#" * filled + "-" * (width - filled)


def short_label(text, max_chars):
    return text if len(text) <= max_chars else text[: max_chars - 1] + "~"


def draw_mic_overlay(frame, status):
    mic_device, mic_level = status.snapshot()
    draw = ImageDraw.Draw(frame)
    x0, y0 = 24, 190
    x1, y1 = frame.width - 24, 216
    bar_x0, bar_y0 = x0 + 8, y0 + 18
    bar_x1, bar_y1 = x1 - 8, y1 - 5
    fill_width = int((bar_x1 - bar_x0) * max(0.0, min(1.0, mic_level)))

    draw.rounded_rectangle((x0, y0, x1, y1), radius=6, fill=(0, 12, 22), outline=(16, 92, 118))
    draw.text((x0 + 8, y0 + 4), f"Mic: {short_label(mic_device, 27)}", fill=(73, 238, 246))
    draw.rectangle((bar_x0, bar_y0, bar_x1, bar_y1), outline=(16, 92, 118), fill=(1, 4, 12))
    if fill_width > 0:
        draw.rectangle((bar_x0, bar_y0, bar_x0 + fill_width, bar_y1), fill=(73, 238, 246))


def require_livekit():
    if api is None or rtc is None:
        raise RuntimeError(
            "LiveKit Python packages are required. Install with: "
            "uv sync"
        )
    if not hasattr(rtc, "MediaDevices"):
        raise RuntimeError(
            "This example requires a recent livekit package with rtc.MediaDevices. "
            "Upgrade with: uv sync --upgrade-package livekit"
        )


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
            f"{dev['max_input_channels']}ch @ {dev['default_samplerate']}Hz"
        )

    print("\nAvailable audio output devices:")
    outputs = devices.list_output_devices()
    if not outputs:
        print("   no output devices found")
    for dev in outputs:
        marker = " (default)" if dev["index"] == default_output else ""
        print(
            f"  [{dev['index']}] {dev['name']}{marker} - "
            f"{dev['max_output_channels']}ch @ {dev['default_samplerate']}Hz"
        )


def device_name_for_index(listing, index):
    for dev in listing:
        if dev["index"] == index:
            return dev["name"]
    return "system default" if index is None else f"device {index}"


def select_device(devices, direction, requested):
    listing = (
        devices.list_input_devices()
        if direction == "input"
        else devices.list_output_devices()
    )
    default_index = (
        devices.default_input_device()
        if direction == "input"
        else devices.default_output_device()
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
                    print(
                        f"Selecting requested audio {direction} device "
                        f"[{index}] {dev['name']}"
                    )
                    return index, dev["name"]
            raise RuntimeError(f"{direction} device index {index} not found")

        dev = find_by_name(requested)
        if not dev:
            raise RuntimeError(f"{direction} device containing '{requested}' not found")
        print(
            f"Selecting requested audio {direction} device "
            f"[{dev['index']}] {dev['name']}"
        )
        return dev["index"], dev["name"]

    dev = find_by_name(DEFAULT_WM8960_NAME)
    if dev:
        print(
            f"Auto-selected WM8960 audio {direction} device "
            f"[{dev['index']}] {dev['name']}"
        )
        return dev["index"], dev["name"]

    print(f"WM8960 audio {direction} not found; using system default")
    return default_index, device_name_for_index(listing, default_index)


async def monitor_remote_audio(track, speech_levels, running, sample_rate):
    key = getattr(track, "sid", str(id(track)))
    stream = rtc.AudioStream(track, sample_rate=sample_rate, num_channels=DEFAULT_CHANNELS)
    frame_count = 0
    last_log_at = time.monotonic()
    try:
        async for event in stream:
            if not running.is_set():
                break

            samples = list(event.frame.data)
            level = calculate_speech_level(samples)
            speech_levels.set(key, level)

            frame_count += 1
            now = time.monotonic()
            if now - last_log_at >= 2.0:
                logging.info(
                    "Remote audio frame #%s: [%s] %.3f",
                    frame_count,
                    level_bar(level),
                    level,
                )
                last_log_at = now
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("remote audio monitor failed")
    finally:
        speech_levels.remove(key)
        await stream.aclose()


async def monitor_mic_audio(track, status, running, sample_rate):
    stream = rtc.AudioStream(track, sample_rate=sample_rate, num_channels=DEFAULT_CHANNELS)
    frame_count = 0
    last_log_at = time.monotonic()
    try:
        async for event in stream:
            if not running.is_set():
                break

            level = calculate_speech_level(list(event.frame.data))
            status.set_mic_level(level)

            frame_count += 1
            now = time.monotonic()
            if now - last_log_at >= 2.0:
                logging.info(
                    "Mic audio frame #%s: [%s] %.3f",
                    frame_count,
                    level_bar(level),
                    level,
                )
                last_log_at = now
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("mic audio monitor failed")
    finally:
        status.set_mic_level(0.0)
        await stream.aclose()


async def run_audio_client(args, speech_levels, status, running):
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
    player = None
    mic = None
    mic_monitor_task = None
    remote_tasks = {}

    input_device, input_device_name = select_device(devices, "input", args.input_device)
    status.set_mic_device(input_device_name)
    output_device = None
    if not args.no_playback:
        output_device, _output_device_name = select_device(devices, "output", args.output_device)

    async def add_remote_track(track, participant_identity):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        logging.info("subscribed to audio from %s", participant_identity)
        if player is not None:
            await player.add_track(track)
        key = getattr(track, "sid", str(id(track)))
        remote_tasks[key] = asyncio.create_task(
            monitor_remote_audio(track, speech_levels, running, args.sample_rate)
        )

    async def remove_remote_track(track, participant_identity):
        logging.info("unsubscribed from audio of %s", participant_identity)
        if player is not None:
            await player.remove_track(track)
        key = getattr(track, "sid", str(id(track)))
        task = remote_tasks.pop(key, None)
        if task:
            task.cancel()

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
            high_pass_filter=False,
            auto_gain_control=args.auto_gain_control,
            input_device=input_device,
            input_channel_index=args.channel,
        )
        if output_device is not None:
            player = devices.open_output(output_device=output_device)
        else:
            logging.warning("audio playback disabled; avatar still reacts to subscribed audio")

        logging.info("connecting to LiveKit room '%s' as '%s'", args.room_name, args.identity)
        await room.connect(url, token, options=rtc.RoomOptions(auto_subscribe=True))
        logging.info("connected to room %s", room.name)

        track = rtc.LocalAudioTrack.create_audio_track("whisplay-microphone", mic.source)
        publish_options = rtc.TrackPublishOptions()
        publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
        publication = await room.local_participant.publish_track(track, publish_options)
        logging.info("published microphone audio track %s", publication.sid)
        mic_monitor_task = asyncio.create_task(
            monitor_mic_audio(track, status, running, args.sample_rate)
        )

        if player is not None:
            await player.start()

        while running.is_set():
            await asyncio.sleep(0.1)
    finally:
        if mic_monitor_task is not None:
            mic_monitor_task.cancel()
            await asyncio.gather(mic_monitor_task, return_exceptions=True)
        for task in remote_tasks.values():
            task.cancel()
        if remote_tasks:
            await asyncio.gather(*remote_tasks.values(), return_exceptions=True)
        if mic is not None:
            await mic.aclose()
        if player is not None:
            await player.aclose()
        try:
            await room.disconnect()
        except Exception:
            pass


def run_audio_thread(args, speech_levels, status, running):
    try:
        asyncio.run(run_audio_client(args, speech_levels, status, running))
    except Exception:
        status.set_mic_device("audio unavailable")
        status.set_mic_level(0.0)
        logging.exception("audio client failed")
        if args.emulated:
            logging.error("keeping emulator open after audio failure")
        else:
            running.clear()


def run_client_avatar(args, speech_levels, status, running):
    board = create_board(emulated=args.emulated, emulator_scale=args.emulator_scale)
    avatar = RobotAvatar(board.LCD_WIDTH, board.LCD_HEIGHT)
    emotion = FACE_SEQUENCE[0]
    emulated_speech_key = None
    emulated_speech_level = None
    last_cycle = time.monotonic()
    start = time.monotonic()
    frame_delay = 1.0 / max(1, args.fps)
    next_frame_at = time.monotonic()
    dirty_x0, dirty_y0, dirty_x1, dirty_y1 = AVATAR_DIRTY_RECT
    dirty_width = dirty_x1 - dirty_x0
    dirty_height = dirty_y1 - dirty_y0
    frame_buffer = bytearray(dirty_width * dirty_height * 2)

    def stop(_signum=None, _frame=None):
        running.clear()

    def next_emotion():
        nonlocal emotion, last_cycle
        emotion = cycle_emotion(emotion)
        last_cycle = time.monotonic()
        print(f"Emotion: {emotion}")

    def set_emulated_speech_level(key, _key_code=None):
        nonlocal emulated_speech_key, emulated_speech_level
        if key in EMULATED_SPEECH_LEVELS:
            emulated_speech_key = key
            emulated_speech_level = EMULATED_SPEECH_LEVELS[key]
            print(f"Speech level: {key} ({emulated_speech_level:.2f})")

    def clear_emulated_speech_level(key, _key_code=None):
        nonlocal emulated_speech_key, emulated_speech_level
        if key == emulated_speech_key:
            emulated_speech_key = None
            emulated_speech_level = None

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    board.on_button_press(next_emotion)
    if args.emulated and hasattr(board, "on_key_press") and hasattr(board, "on_key_release"):
        board.on_key_press(set_emulated_speech_level)
        board.on_key_release(clear_emulated_speech_level)

    board.set_rgb(0, 0, 0)
    board.set_backlight(100)
    background_buffer = rgb_to_rgb565be(avatar.background)
    board.draw_image(0, 0, board.LCD_WIDTH, board.LCD_HEIGHT, background_buffer)

    if args.emulated:
        print("Running avatar client. Space changes emotion, hold 1-5 for speech levels, Esc exits.")
    else:
        print("Running avatar client. Press the WhisPlay button to change emotion, Ctrl+C to exit.")

    try:
        while running.is_set():
            now = time.monotonic()
            if not args.no_auto_cycle and now - last_cycle > 5.0:
                next_emotion()

            shared_level = (
                emulated_speech_level
                if emulated_speech_level is not None
                else speech_levels.current()
            )
            frame = avatar.draw_frame(
                emotion,
                speaking=args.speaking or shared_level is not None,
                t=now - start,
                speech_level=shared_level,
            )
            draw_mic_overlay(frame, status)
            rgb_to_rgb565be(frame.crop(AVATAR_DIRTY_RECT), frame_buffer)
            board.draw_image(dirty_x0, dirty_y0, dirty_width, dirty_height, frame_buffer)

            next_frame_at += frame_delay
            sleep_for = next_frame_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_frame_at = time.monotonic()
    finally:
        running.clear()
        board.set_backlight(0)
        board.cleanup()


def build_parser():
    parser = argparse.ArgumentParser(description="WhisPlay avatar LiveKit audio client.")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available audio devices and exit.",
    )
    parser.add_argument("-i", "--input-device", help="Input device index or name substring.")
    parser.add_argument("-o", "--output-device", help="Output device index or name substring.")
    parser.add_argument("--channel", type=int, default=0, help="Input channel index to capture.")
    parser.add_argument(
        "-r",
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Sample rate in Hz.",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=1.0,
        help="Reserved for parity with the Rust client.",
    )
    parser.add_argument(
        "--stream-delay-ms",
        type=int,
        default=50,
        help="Reserved for parity with the Rust client.",
    )
    parser.add_argument("--echo-cancellation", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--noise-suppression", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--auto-gain-control", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--no-audio", action="store_true", help="Run only the avatar display.")
    parser.add_argument("--no-playback", action="store_true", help="Disable remote room audio playback.")
    parser.add_argument("--url", help="LiveKit server URL. Can also be set with LIVEKIT_URL.")
    parser.add_argument("--token", help="LiveKit access token. Can also be set with LIVEKIT_TOKEN.")
    parser.add_argument("--api-key", help="LiveKit API key. Can also be set with LIVEKIT_API_KEY.")
    parser.add_argument("--api-secret", help="LiveKit API secret. Can also be set with LIVEKIT_API_SECRET.")
    parser.add_argument("--room-name", default="whisplay-avatar", help="LiveKit room name.")
    parser.add_argument("--identity", default="whisplay-avatar-client", help="LiveKit participant identity.")
    parser.add_argument("--fps", type=int, default=30, help="Avatar frames per second.")
    parser.add_argument("--speaking", action="store_true", help="Keep the avatar mouth in speech animation.")
    parser.add_argument("--no-auto-cycle", action="store_true", help="Disable automatic face cycling.")
    parser.add_argument("--emulated", action="store_true", help="Use the desktop display emulator.")
    parser.add_argument("--emulator-scale", type=int, default=None, help="Desktop emulator scale.")
    return parser


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = build_parser().parse_args()

    if not 0.0 <= args.volume <= 1.0:
        raise SystemExit("--volume must be between 0.0 and 1.0")

    if args.list_devices:
        list_audio_devices()
        return

    running = threading.Event()
    running.set()
    speech_levels = SpeechLevels()
    status = ClientStatus()
    audio_thread = None
    if not args.no_audio:
        audio_thread = threading.Thread(
            target=run_audio_thread,
            args=(args, speech_levels, status, running),
            daemon=True,
        )
        audio_thread.start()
    else:
        status.set_mic_device("audio off")

    try:
        run_client_avatar(args, speech_levels, status, running)
    finally:
        running.clear()
        if audio_thread is not None:
            audio_thread.join(timeout=5)


if __name__ == "__main__":
    main()
