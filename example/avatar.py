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

try:
    from Driver.WhisPlay import WhisPlayBoard
except ImportError:
    print("Error: Library 'Driver/WhisPlay.py' not found.")
    sys.exit(1)


EMOTIONS = ("happy", "curious", "excited", "sleepy", "thinking", "sad")
AVATAR_DIRTY_RECT = (24, 48, 216, 218)


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
        self.background = Image.new("RGB", (self.width, self.height), (2, 6, 14))

    def maybe_blink(self, now):
        if now >= self.next_blink:
            self.blink_until = now + 0.10
            self.next_blink = now + random.uniform(2.0, 5.0)
        return now < self.blink_until

    def draw_frame(self, emotion, speaking=False, t=0.0):
        img = self.background.copy()
        draw = ImageDraw.Draw(img)

        blink = self.maybe_blink(time.monotonic())
        self._draw_eyes(draw, emotion, blink, t)
        self._draw_mouth(draw, emotion, speaking, t)

        return img

    def _draw_eye_capsule(self, draw, box, fill, sparkle=True):
        draw.rounded_rectangle(box, radius=18, fill=fill)
        x0, y0, x1, y1 = box
        if sparkle:
            draw.ellipse((x0 + 9, y0 + 10, x0 + 19, y0 + 21), fill=(240, 255, 255))
            draw.ellipse((x1 - 15, y0 + 28, x1 - 9, y0 + 35), fill=(185, 255, 252))

    def _draw_happy_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        draw.arc((x0, y0 + 8, x1, y1 + 22), 190, 350, fill=color, width=8)

    def _draw_sleepy_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        y = (y0 + y1) // 2
        draw.arc((x0, y - 12, x1, y + 20), 10, 170, fill=color, width=6)

    def _draw_sad_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        draw.rounded_rectangle((x0, y0 + 12, x1, y1), radius=17, fill=color)
        draw.line((x0 - 3, y0 + 7, x1 + 3, y0 + 20), fill=(2, 6, 14), width=9)

    def _draw_thinking_eye(self, draw, box, color, offset):
        x0, y0, x1, y1 = box
        box = (x0 + offset, y0, x1 + offset, y1)
        self._draw_eye_capsule(draw, box, color)

    def _draw_eyes(self, draw, emotion, blink, t):
        bob = int(math.sin(t * 2.0) * 2)
        color = (104, 244, 245)
        glow = (37, 120, 138)
        left = (58, 78 + bob, 101, 148 + bob)
        right = (139, 78 + bob, 182, 148 + bob)

        if blink:
            for x0, y0, x1, y1 in (left, right):
                y = (y0 + y1) // 2
                draw.line((x0 + 2, y, x1 - 2, y), fill=color, width=7)
            return

        if emotion == "happy":
            for box in (left, right):
                self._draw_happy_eye(draw, box, color)
            return

        if emotion == "sleepy":
            for box in (left, right):
                self._draw_sleepy_eye(draw, box, color)
            return

        if emotion == "sad":
            self._draw_sad_eye(draw, (55, 88 + bob, 99, 153 + bob), color)
            self._draw_sad_eye(draw, (141, 88 + bob, 185, 153 + bob), color)
            return

        if emotion == "curious":
            self._draw_eye_capsule(draw, (55, 83 + bob, 98, 145 + bob), color)
            self._draw_eye_capsule(draw, (136, 70 + bob, 185, 151 + bob), color)
            return

        if emotion == "thinking":
            look = int(math.sin(t * 1.8) * 5)
            self._draw_thinking_eye(draw, left, color, look)
            self._draw_thinking_eye(draw, right, color, look)
            return

        if emotion == "excited":
            pulse = int(20 * (0.5 + 0.5 * math.sin(t * 8.0)))
            for box in (left, right):
                x0, y0, x1, y1 = box
                draw.rounded_rectangle((x0 - 4, y0 - 4, x1 + 4, y1 + 4), radius=22, fill=glow)
                self._draw_eye_capsule(draw, box, (120 + pulse, 255, 250))
            return

        for box in (left, right):
            self._draw_eye_capsule(draw, box, color)

    def _draw_mouth(self, draw, emotion, speaking, t):
        mx = self.width // 2
        my = 178 + int(math.sin(t * 2.0) * 2)
        color = (104, 244, 245)

        if speaking:
            openness = 8 + int(18 * (0.5 + 0.5 * math.sin(t * 14.0)))
            mouth_width = 22 + int(8 * (0.5 + 0.5 * math.sin(t * 9.0)))
            draw.rounded_rectangle(
                (mx - mouth_width, my - openness // 2, mx + mouth_width, my + openness),
                radius=openness,
                fill=color,
            )
            draw.rounded_rectangle(
                (mx - mouth_width + 6, my - openness // 2 + 5, mx + mouth_width - 6, my + openness - 5),
                radius=max(4, openness // 2),
                fill=(2, 6, 14),
            )
            return

        if emotion in ("happy", "excited"):
            draw.arc((mx - 31, my - 21, mx + 31, my + 24), 15, 165, fill=color, width=6)
        elif emotion == "sad":
            draw.arc((mx - 28, my + 4, mx + 28, my + 42), 195, 345, fill=color, width=5)
        elif emotion == "sleepy":
            draw.line((mx - 18, my + 4, mx + 18, my + 4), fill=color, width=5)
        elif emotion == "thinking":
            draw.ellipse((mx - 7, my - 2, mx + 7, my + 12), outline=color, width=4)
        else:
            draw.arc((mx - 22, my - 12, mx + 22, my + 16), 20, 160, fill=color, width=5)


def cycle_emotion(current):
    idx = EMOTIONS.index(current)
    return EMOTIONS[(idx + 1) % len(EMOTIONS)]


def run_avatar(initial_emotion, fps, speaking, auto_cycle):
    board = WhisPlayBoard()
    avatar = RobotAvatar(board.LCD_WIDTH, board.LCD_HEIGHT)
    emotion = initial_emotion
    running = True
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

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    board.on_button_press(next_emotion)
    board.set_rgb(0, 0, 0)
    board.set_backlight(100)
    rgb_to_rgb565be(avatar.background, background_buffer)
    board.draw_image(0, 0, board.LCD_WIDTH, board.LCD_HEIGHT, background_buffer)

    print("Animating robot avatar. Press the WhisPlay button to change emotion, Ctrl+C to exit.")

    try:
        while running:
            now = time.monotonic()
            if auto_cycle and now - last_cycle > 5.0:
                next_emotion()

            voice_active = speaking or (emotion == "excited" and int(now * 2) % 2 == 0)
            frame = avatar.draw_frame(emotion, speaking=voice_active, t=now)
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
    parser.add_argument("--fps", type=int, default=12, help="Target animation frame rate.")
    parser.add_argument("--speaking", action="store_true", help="Keep the mouth talking.")
    parser.add_argument("--no-auto-cycle", action="store_true", help="Do not change emotions automatically.")
    args = parser.parse_args()

    run_avatar(
        initial_emotion=args.emotion,
        fps=args.fps,
        speaking=args.speaking,
        auto_cycle=not args.no_auto_cycle,
    )
