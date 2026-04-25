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

EMOTIONS = ("happy", "curious", "excited", "sleepy", "thinking", "sad")
AVATAR_DIRTY_RECT = (20, 44, 220, 220)


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

    def draw_frame(self, emotion, speaking=False, t=0.0):
        img = self.background.copy()
        draw = ImageDraw.Draw(img)

        blink = self.maybe_blink(time.monotonic())
        self._draw_eyes(draw, emotion, blink, t)
        self._draw_mouth(draw, emotion, speaking, t)

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

    def _draw_star(self, draw, cx, cy, radius, color):
        draw.line((cx - radius, cy, cx + radius, cy), fill=color, width=2)
        draw.line((cx, cy - radius, cx, cy + radius), fill=color, width=2)
        draw.point((cx, cy), fill=(255, 255, 255))

    def _draw_eye_capsule(self, draw, box, fill, sparkle=True):
        x0, y0, x1, y1 = box
        bg = self._face_bg()
        accent = (255, 117, 203)
        shadow = (5, 24, 45)

        draw.rounded_rectangle((x0 - 7, y0 - 5, x1 + 7, y1 + 5), radius=23, fill=(4, 21, 36))
        draw.rounded_rectangle((x0 - 4, y0 - 3, x1 + 4, y1 + 3), radius=21, fill=(18, 84, 105))
        draw.rounded_rectangle(box, radius=18, fill=fill)

        inner = (x0 + 7, y0 + 8, x1 - 7, y1 - 8)
        draw.rounded_rectangle(inner, radius=13, fill=shadow)
        draw.rounded_rectangle((inner[0] + 4, inner[1] + 6, inner[2] - 4, inner[3] - 2), radius=10, fill=(10, 59, 77))
        draw.rounded_rectangle((inner[0] + 4, inner[1] + 6, inner[2] - 4, inner[1] + 20), radius=9, fill=(99, 248, 244))
        draw.rounded_rectangle((inner[0] + 6, inner[1] + 25, inner[2] - 6, inner[3] - 5), radius=8, fill=(33, 160, 185))
        draw.arc((x0 + 4, y0 + 4, x1 - 4, y1 - 9), 190, 350, fill=(206, 255, 255), width=3)

        if sparkle:
            draw.ellipse((x0 + 10, y0 + 11, x0 + 20, y0 + 22), fill=(245, 255, 255))
            draw.ellipse((x1 - 17, y0 + 31, x1 - 10, y0 + 39), fill=(194, 255, 252))
            draw.arc((x0 - 7, y0 - 8, x1 + 7, y1 + 8), 290, 340, fill=accent, width=3)
            self._draw_star(draw, x1 + 11, y0 + 11, 4, (255, 147, 218))
            draw.point((x0 - 6, y1 - 6), fill=bg)

    def _draw_happy_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        draw.arc((x0 - 3, y0 + 14, x1 + 3, y1 + 22), 190, 350, fill=(23, 95, 119), width=13)
        draw.arc((x0, y0 + 15, x1, y1 + 20), 190, 350, fill=color, width=8)
        draw.arc((x0 + 7, y0 + 18, x1 - 7, y1 + 12), 205, 335, fill=(238, 255, 255), width=2)

    def _draw_sleepy_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        y = (y0 + y1) // 2
        draw.arc((x0 - 1, y - 15, x1 + 1, y + 19), 10, 170, fill=(19, 82, 108), width=10)
        draw.arc((x0, y - 14, x1, y + 17), 10, 170, fill=color, width=6)
        draw.line((x1 + 9, y - 13, x1 + 20, y - 21), fill=(104, 244, 245), width=3)

    def _draw_sad_eye(self, draw, box, color):
        x0, y0, x1, y1 = box
        self._draw_eye_capsule(draw, (x0, y0 + 10, x1, y1), color, sparkle=False)
        draw.line((x0 - 5, y0 + 9, x1 + 5, y0 + 25), fill=self._face_bg(), width=12)
        draw.ellipse((x1 - 8, y1 + 4, x1 + 1, y1 + 17), fill=(104, 244, 245))

    def _draw_thinking_eye(self, draw, box, color, offset):
        x0, y0, x1, y1 = box
        box = (x0 + offset, y0, x1 + offset, y1)
        self._draw_eye_capsule(draw, box, color)
        draw.rounded_rectangle((x0 + 15 + offset, y0 + 28, x1 - 15 + offset, y1 - 17), radius=7, fill=(238, 255, 255))

    def _draw_eyes(self, draw, emotion, blink, t):
        bob = int(math.sin(t * 2.0) * 2)
        color = (104, 244, 245)
        glow = (31, 114, 145)
        left = (55, 74 + bob, 103, 151 + bob)
        right = (137, 74 + bob, 185, 151 + bob)

        if blink:
            for x0, y0, x1, y1 in (left, right):
                y = (y0 + y1) // 2
                draw.line((x0 + 2, y, x1 - 2, y), fill=(23, 95, 119), width=11)
                draw.line((x0 + 5, y, x1 - 5, y), fill=color, width=6)
            return

        if emotion == "happy":
            for box in (left, right):
                self._draw_happy_eye(draw, box, color)
            self._draw_star(draw, 120, 84 + bob, 3, (255, 147, 218))
            return

        if emotion == "sleepy":
            for box in (left, right):
                self._draw_sleepy_eye(draw, box, color)
            return

        if emotion == "sad":
            self._draw_sad_eye(draw, (53, 86 + bob, 101, 154 + bob), color)
            self._draw_sad_eye(draw, (139, 86 + bob, 187, 154 + bob), color)
            return

        if emotion == "curious":
            self._draw_eye_capsule(draw, (54, 85 + bob, 99, 146 + bob), color)
            self._draw_eye_capsule(draw, (132, 66 + bob, 188, 155 + bob), color)
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
                draw.rounded_rectangle((x0 - 8, y0 - 7, x1 + 8, y1 + 7), radius=25, fill=(12, 47, 70))
                draw.rounded_rectangle((x0 - 4, y0 - 4, x1 + 4, y1 + 4), radius=22, fill=glow)
                self._draw_eye_capsule(draw, box, (120 + pulse, 255, 250))
                self._draw_star(draw, x0 - 8, y0 + 2, 4, (255, 147, 218))
            return

        for box in (left, right):
            self._draw_eye_capsule(draw, box, color)

    def _draw_mouth(self, draw, emotion, speaking, t):
        mx = self.width // 2
        my = 181 + int(math.sin(t * 2.0) * 2)
        color = (104, 244, 245)

        if speaking:
            openness = 8 + int(18 * (0.5 + 0.5 * math.sin(t * 14.0)))
            mouth_width = 24 + int(8 * (0.5 + 0.5 * math.sin(t * 9.0)))
            draw.rounded_rectangle(
                (mx - mouth_width, my - openness // 2, mx + mouth_width, my + openness),
                radius=openness,
                fill=(21, 91, 116),
            )
            draw.rounded_rectangle(
                (mx - mouth_width + 6, my - openness // 2 + 5, mx + mouth_width - 6, my + openness - 5),
                radius=max(4, openness // 2),
                fill=color,
            )
            for i, height in enumerate((10, 17, 23, 15, 9)):
                phase = 0.5 + 0.5 * math.sin(t * 12.0 + i * 0.8)
                bar_h = int(4 + height * phase)
                x = mx - 14 + i * 7
                draw.line((x, my + openness // 2 - bar_h, x, my + openness // 2), fill=self._face_bg(), width=3)
            return

        if emotion in ("happy", "excited"):
            draw.arc((mx - 34, my - 23, mx + 34, my + 25), 15, 165, fill=(21, 91, 116), width=11)
            draw.arc((mx - 32, my - 22, mx + 32, my + 23), 15, 165, fill=color, width=6)
            draw.ellipse((mx - 5, my + 12, mx + 5, my + 18), fill=(255, 147, 218))
        elif emotion == "sad":
            draw.arc((mx - 30, my + 4, mx + 30, my + 42), 195, 345, fill=(21, 91, 116), width=9)
            draw.arc((mx - 28, my + 5, mx + 28, my + 40), 195, 345, fill=color, width=5)
        elif emotion == "sleepy":
            draw.line((mx - 21, my + 4, mx + 21, my + 4), fill=(21, 91, 116), width=9)
            draw.line((mx - 18, my + 4, mx + 18, my + 4), fill=color, width=5)
        elif emotion == "thinking":
            draw.ellipse((mx - 10, my - 4, mx + 10, my + 16), outline=(21, 91, 116), width=7)
            draw.ellipse((mx - 8, my - 2, mx + 8, my + 14), outline=color, width=4)
        else:
            draw.arc((mx - 24, my - 12, mx + 24, my + 16), 20, 160, fill=(21, 91, 116), width=8)
            draw.arc((mx - 22, my - 12, mx + 22, my + 16), 20, 160, fill=color, width=5)


def cycle_emotion(current):
    idx = EMOTIONS.index(current)
    return EMOTIONS[(idx + 1) % len(EMOTIONS)]


def run_avatar(initial_emotion, fps, speaking, auto_cycle, emulated=False, emulator_scale=None):
    board = create_board(emulated=emulated, emulator_scale=emulator_scale)
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
