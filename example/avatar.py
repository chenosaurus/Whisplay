import argparse
import math
import os
import random
import signal
import sys
import time

from PIL import Image, ImageDraw


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

FACE_SEQUENCE = (
    "happy",
    "wink",
    "worried",
    "angry",
    "neutral",
    "love",
    "surprised",
    "bored",
    "joy",
    "sad",
)
EMOTION_ALIASES = {
    "curious": "surprised",
    "excited": "joy",
    "sleepy": "bored",
    "thinking": "neutral",
}
EMOTIONS = FACE_SEQUENCE + tuple(EMOTION_ALIASES)
AVATAR_DIRTY_RECT = (20, 44, 220, 220)
MOUTH_OVERLAY_RECT = (70, 132, 170, 192)
EMULATED_SPEECH_LEVELS = {
    "1": 0.15,
    "2": 0.35,
    "3": 0.55,
    "4": 0.75,
    "5": 1.0,
}
BOB_AMPLITUDE = 4.0
BOB_SPEED = 1.8


def create_board(emulated=False, emulator_scale=None):
    if emulated:
        from Driver.EmulatedWhisPlay import EmulatedWhisPlayBoard

        return EmulatedWhisPlayBoard(scale=emulator_scale)

    try:
        from Driver.WhisPlay import WhisPlayBoard
    except ImportError:
        print("Error: Library 'Driver/WhisPlay.py' not found.")
        sys.exit(1)

    return WhisPlayBoard()


def rgb_to_rgb565be(image, out=None):
    """Convert a PIL RGB image to the display's big-endian RGB565 bytes."""
    rgb_image = image if image.mode == "RGB" else image.convert("RGB")
    rgb = rgb_image.tobytes()
    expected_len = (len(rgb) // 3) * 2
    if out is None:
        out = bytearray(expected_len)
    elif len(out) != expected_len:
        raise ValueError(f"Output buffer must be {expected_len} bytes")

    j = 0
    for i in range(0, len(rgb), 3):
        r = rgb[i]
        g = rgb[i + 1]
        b = rgb[i + 2]
        color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[j] = (color >> 8) & 0xFF
        out[j + 1] = color & 0xFF
        j += 2

    return out


class RobotAvatar:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.blink_until = 0.0
        self.next_blink = time.monotonic() + random.uniform(1.5, 4.0)
        self.background = self._make_background()

    def maybe_blink(self, now):
        if now >= self.next_blink:
            self.blink_until = now + 0.10
            self.next_blink = now + random.uniform(2.0, 5.0)
        return now < self.blink_until

    def draw_frame(self, emotion, speaking=False, t=0.0, speech_level=None):
        img = self.background.copy()
        draw = ImageDraw.Draw(img)

        blink = self.maybe_blink(time.monotonic())
        self._draw_mouth(draw, emotion, t)
        if speaking or speech_level is not None:
            self._draw_speech_mouth_overlay(img, t, speech_level=speech_level)
            draw = ImageDraw.Draw(img)
        self._draw_eyes(draw, emotion, blink, t)

        return img

    def _make_background(self):
        img = Image.new("RGB", (self.width, self.height), (1, 4, 12))
        draw = ImageDraw.Draw(img)
        for y in range(self.height):
            shade = int(5 + 12 * y / self.height)
            blue = int(18 + 16 * y / self.height)
            draw.line((0, y, self.width, y), fill=(1, shade, blue))
        return img

    def _face_bg(self):
        return (1, 4, 12)

    def _bob(self, t):
        return math.sin(t * BOB_SPEED) * BOB_AMPLITUDE

    def _cyan(self):
        return (73, 238, 246)

    def _dim_cyan(self):
        return (16, 92, 118)

    def _pink(self):
        return (255, 76, 143)

    def _line(self, draw, points, color=None, width=5):
        draw.line(points, fill=color or self._cyan(), width=width, joint="curve")

    def _glow_dot(self, draw, cx, cy, radius=14, color=None):
        color = color or self._cyan()
        eye_w = radius * 2 - 6
        eye_h = radius * 2 + 10
        x0 = cx - eye_w // 2
        y0 = cy - eye_h // 2
        x1 = cx + eye_w // 2
        y1 = cy + eye_h // 2
        glow = (x0 - 5, y0 - 5, x1 + 5, y1 + 5)
        draw.rounded_rectangle(glow, radius=eye_w // 2 + 5, fill=self._dim_cyan())
        draw.rounded_rectangle((x0, y0, x1, y1), radius=eye_w // 2, fill=color)
        draw.rounded_rectangle((x0 + 8, y0 + 7, x0 + 15, y0 + 17), radius=4, fill=(225, 255, 255))

    def _flat_eye(self, draw, cx, cy, color=None):
        color = color or self._cyan()
        self._line(draw, (cx - 24, cy, cx + 24, cy), color=self._dim_cyan(), width=13)
        self._line(draw, (cx - 21, cy, cx + 21, cy), color=color, width=9)

    def _smile_eye(self, draw, cx, cy):
        draw.arc((cx - 27, cy - 18, cx + 27, cy + 33), 20, 160, fill=self._dim_cyan(), width=13)
        draw.arc((cx - 24, cy - 16, cx + 24, cy + 31), 20, 160, fill=self._cyan(), width=9)

    def _sad_eye(self, draw, cx, cy):
        draw.arc((cx - 27, cy - 9, cx + 27, cy + 34), 200, 340, fill=self._dim_cyan(), width=13)
        draw.arc((cx - 24, cy - 7, cx + 24, cy + 32), 200, 340, fill=self._cyan(), width=9)

    def _angry_eye(self, draw, cx, cy, direction):
        del direction
        color = self._pink()
        dim = (103, 21, 62)
        size = 16
        self._line(draw, (cx - size, cy - size, cx + size, cy + size), color=dim, width=13)
        self._line(draw, (cx - size, cy + size, cx + size, cy - size), color=dim, width=13)
        self._line(draw, (cx - size + 2, cy - size + 2, cx + size - 2, cy + size - 2), color=color, width=8)
        self._line(draw, (cx - size + 2, cy + size - 2, cx + size - 2, cy - size + 2), color=color, width=8)

    def _heart(self, draw, cx, cy):
        color = self._pink()
        draw.ellipse((cx - 25, cy - 20, cx - 1, cy + 4), fill=color)
        draw.ellipse((cx + 1, cy - 20, cx + 25, cy + 4), fill=color)
        draw.polygon(((cx - 24, cy - 6), (cx + 24, cy - 6), (cx, cy + 30)), fill=color)

    def _smile(self, draw, cx, cy, width=44):
        draw.arc((cx - width // 2, cy - 18, cx + width // 2, cy + 22), 15, 165, fill=self._dim_cyan(), width=9)
        draw.arc((cx - width // 2 + 2, cy - 17, cx + width // 2 - 2, cy + 20), 15, 165, fill=self._cyan(), width=5)

    def _frown(self, draw, cx, cy, width=40, color=None, dim_color=None):
        color = color or self._cyan()
        dim_color = dim_color or self._dim_cyan()
        draw.arc((cx - width // 2, cy + 2, cx + width // 2, cy + 34), 200, 340, fill=dim_color, width=8)
        draw.arc((cx - width // 2 + 2, cy + 4, cx + width // 2 - 2, cy + 32), 200, 340, fill=color, width=5)

    def _open_mouth(self, draw, cx, cy, radius=9):
        draw.ellipse((cx - radius - 4, cy - radius - 4, cx + radius + 4, cy + radius + 4), fill=self._dim_cyan())
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=self._cyan())
        draw.ellipse((cx - radius + 5, cy - radius + 5, cx + radius - 5, cy + radius - 4), fill=self._face_bg())

    def _draw_eyes(self, draw, emotion, blink, t):
        emotion = EMOTION_ALIASES.get(emotion, emotion)
        bob = self._bob(t)
        ly = 107 + bob
        ry = 107 + bob
        lx = 80
        rx = 160

        if blink and emotion in ("happy", "neutral", "surprised"):
            self._flat_eye(draw, lx, ly)
            self._flat_eye(draw, rx, ry)
            return

        if emotion == "happy":
            self._glow_dot(draw, lx, ly)
            self._glow_dot(draw, rx, ry)
        elif emotion == "wink":
            self._flat_eye(draw, lx, ly)
            self._glow_dot(draw, rx, ry)
        elif emotion == "worried":
            self._sad_eye(draw, lx, ly - 3)
            self._sad_eye(draw, rx, ry - 3)
            self._line(draw, (lx - 18, ly - 28, lx - 2, ly - 35), width=4)
            self._line(draw, (rx + 18, ry - 28, rx + 2, ry - 35), width=4)
        elif emotion == "angry":
            self._angry_eye(draw, lx, ly, 1)
            self._angry_eye(draw, rx, ry, -1)
        elif emotion == "neutral":
            self._glow_dot(draw, lx, ly)
            self._glow_dot(draw, rx, ry)
        elif emotion == "love":
            self._heart(draw, lx, ly)
            self._heart(draw, rx, ry)
        elif emotion == "surprised":
            self._glow_dot(draw, lx, ly, radius=16)
            self._glow_dot(draw, rx, ry, radius=16)
        elif emotion == "bored":
            self._flat_eye(draw, lx, ly - 2)
            self._flat_eye(draw, rx, ry - 2)
        elif emotion == "joy":
            self._smile_eye(draw, lx, ly - 2)
            self._smile_eye(draw, rx, ry - 2)
        elif emotion == "sad":
            self._glow_dot(draw, lx, ly + 4, radius=14)
            self._glow_dot(draw, rx, ry + 4, radius=14)
            self._line(draw, (lx - 18, ly - 14, lx + 15, ly - 26), width=4)
            self._line(draw, (rx - 15, ry - 26, rx + 18, ry - 14), width=4)

    def _draw_mouth(self, draw, emotion, t):
        emotion = EMOTION_ALIASES.get(emotion, emotion)
        mx = self.width // 2
        my = 160 + self._bob(t)

        if emotion in ("happy", "wink", "love", "joy"):
            self._smile(draw, mx, my)
        elif emotion in ("worried", "sad"):
            self._frown(draw, mx, my)
        elif emotion == "angry":
            self._frown(draw, mx, my, width=38, color=self._pink(), dim_color=(103, 21, 62))
        elif emotion == "neutral":
            return
        elif emotion == "surprised":
            self._open_mouth(draw, mx, my + 4)
        elif emotion == "bored":
            self._line(draw, (mx - 18, my, mx + 18, my), width=5)

    def _draw_speech_mouth_overlay(self, img, t, speech_level=None):
        x0, y0, x1, y1 = MOUTH_OVERLAY_RECT
        img.paste(self.background.crop(MOUTH_OVERLAY_RECT), (x0, y0))
        draw = ImageDraw.Draw(img)
        mx = self.width // 2
        my = 160 + self._bob(t)

        if speech_level is not None:
            speech_level = max(0.0, min(1.0, speech_level))

        for i, height in enumerate((10, 18, 26, 18, 10)):
            phase = 0.5 + 0.5 * math.sin(t * 13.0 + i * 0.9)
            amount = phase if speech_level is None else phase * speech_level
            bar_h = int(5 + height * amount)
            x = mx - 20 + i * 10
            self._line(draw, (x, my + bar_h // 2, x, my - bar_h // 2), width=5)


def cycle_emotion(current):
    current = EMOTION_ALIASES.get(current, current)
    idx = FACE_SEQUENCE.index(current)
    return FACE_SEQUENCE[(idx + 1) % len(FACE_SEQUENCE)]


def run_avatar(initial_emotion, fps, speaking, auto_cycle, emulated=False, emulator_scale=None):
    board = create_board(emulated=emulated, emulator_scale=emulator_scale)
    avatar = RobotAvatar(board.LCD_WIDTH, board.LCD_HEIGHT)
    emotion = EMOTION_ALIASES.get(initial_emotion, initial_emotion)
    running = True
    emulated_speech_key = None
    emulated_speech_level = None
    last_cycle = time.monotonic()
    frame_delay = 1.0 / max(1, fps)
    dirty_x0, dirty_y0, dirty_x1, dirty_y1 = AVATAR_DIRTY_RECT
    dirty_width = dirty_x1 - dirty_x0
    dirty_height = dirty_y1 - dirty_y0
    background_buffer = bytearray(board.LCD_WIDTH * board.LCD_HEIGHT * 2)
    frame_buffer = bytearray(dirty_width * dirty_height * 2)
    next_frame_at = time.monotonic()

    def stop(_signum=None, _frame=None):
        nonlocal running
        running = False

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
    if emulated and hasattr(board, "on_key_press") and hasattr(board, "on_key_release"):
        board.on_key_press(set_emulated_speech_level)
        board.on_key_release(clear_emulated_speech_level)
    board.set_rgb(0, 0, 0)
    board.set_backlight(100)
    rgb_to_rgb565be(avatar.background, background_buffer)
    board.draw_image(0, 0, board.LCD_WIDTH, board.LCD_HEIGHT, background_buffer)

    print("Animating robot avatar. Press the WhisPlay button to change emotion, Ctrl+C to exit.")
    if emulated:
        print("Emulator: hold 1-5 to preview speech mouth heights.")

    try:
        while running:
            now = time.monotonic()
            if auto_cycle and now - last_cycle > 5.0:
                next_emotion()

            voice_active = speaking or emulated_speech_level is not None
            frame = avatar.draw_frame(
                emotion,
                speaking=voice_active,
                t=now,
                speech_level=emulated_speech_level,
            )
            rgb_to_rgb565be(frame.crop(AVATAR_DIRTY_RECT), frame_buffer)
            board.draw_image(dirty_x0, dirty_y0, dirty_width, dirty_height, frame_buffer)

            next_frame_at += frame_delay
            sleep_for = next_frame_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_frame_at = time.monotonic()
    finally:
        board.set_backlight(0)
        board.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Animate a cute robot face on the WhisPlay LCD.")
    parser.add_argument(
        "--emotion",
        choices=EMOTIONS,
        default="happy",
        help="Starting emotion.",
    )
    parser.add_argument("--fps", type=int, default=30, help="Target animation frame rate.")
    parser.add_argument("--speaking", action="store_true", help="Keep the mouth talking.")
    parser.add_argument("--no-auto-cycle", action="store_true", help="Do not change emotions automatically.")
    parser.add_argument("--emulated", action="store_true", help="Use the software ST7789 emulator.")
    parser.add_argument("--emulator-scale", type=int, default=None, help="Scale factor for the emulator window.")
    args = parser.parse_args()

    run_avatar(
        initial_emotion=args.emotion,
        fps=args.fps,
        speaking=args.speaking,
        auto_cycle=not args.no_auto_cycle,
        emulated=args.emulated,
        emulator_scale=args.emulator_scale,
    )
