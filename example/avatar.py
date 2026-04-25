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
AVATAR_DIRTY_RECT = (18, 10, 222, 240)


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


def rounded_rectangle(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


class RobotAvatar:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.blink_until = 0.0
        self.next_blink = time.monotonic() + random.uniform(1.5, 4.0)
        self.sparkle_phase = random.random() * math.tau
        self.background = self._make_background()

    def maybe_blink(self, now):
        if now >= self.next_blink:
            self.blink_until = now + 0.11
            self.next_blink = now + random.uniform(2.0, 5.5)
        return now < self.blink_until

    def draw_frame(self, emotion, speaking=False, t=0.0):
        img = self.background.copy()
        draw = ImageDraw.Draw(img)

        self._draw_head(draw, emotion, t)

        blink = self.maybe_blink(time.monotonic())
        self._draw_eyes(draw, emotion, blink, t)
        self._draw_cheeks(draw, emotion, t)
        self._draw_mouth(draw, emotion, speaking, t)
        self._draw_antenna(draw, emotion, t)

        return img

    def _make_background(self):
        img = Image.new("RGB", (self.width, self.height), (8, 13, 22))
        draw = ImageDraw.Draw(img)
        for y in range(self.height):
            shade = int(10 + y / self.height * 22)
            draw.line((0, y, self.width, y), fill=(5, shade, 35))

        for i in range(9):
            x = int((i * 47) % self.width)
            y = int((i * 71) % self.height)
            color = (18, 82, 110)
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=color)

        return img

    def _draw_head(self, draw, emotion, t):
        bob = int(math.sin(t * 2.0) * 2)
        head = (33, 55 + bob, self.width - 33, 227 + bob)
        shadow = (head[0] + 4, head[1] + 7, head[2] + 4, head[3] + 7)
        rounded_rectangle(draw, shadow, 34, fill=(2, 5, 12))
        rounded_rectangle(draw, head, 34, fill=(194, 226, 236), outline=(78, 135, 158), width=4)

        face = (49, 77 + bob, self.width - 49, 199 + bob)
        rounded_rectangle(draw, face, 26, fill=(18, 37, 51), outline=(94, 188, 211), width=3)

        if emotion == "thinking":
            draw.arc((54, 82 + bob, 186, 214 + bob), 205, 305, fill=(99, 220, 216), width=2)

    def _draw_antenna(self, draw, emotion, t):
        bob = int(math.sin(t * 2.0) * 2)
        cx = self.width // 2
        top = 40 + bob
        color = (255, 211, 88) if emotion in ("happy", "excited") else (98, 217, 236)
        glow = int(30 + 25 * (0.5 + 0.5 * math.sin(t * 6.0)))

        draw.line((cx, top + 18, cx, top - 8), fill=(93, 152, 171), width=4)
        draw.ellipse((cx - 10, top - 20, cx + 10, top), fill=(color[0], color[1], color[2]))
        draw.ellipse((cx - 15, top - 25, cx + 15, top + 5), outline=(color[0], color[1], glow), width=2)

    def _draw_eyes(self, draw, emotion, blink, t):
        bob = int(math.sin(t * 2.0) * 2)
        left = (78, 119 + bob)
        right = (162, 119 + bob)

        if emotion == "sleepy":
            blink = True

        if blink:
            draw.arc((left[0] - 23, left[1] - 7, left[0] + 23, left[1] + 20), 0, 180, fill=(111, 235, 239), width=5)
            draw.arc((right[0] - 23, right[1] - 7, right[0] + 23, right[1] + 20), 0, 180, fill=(111, 235, 239), width=5)
            return

        eye_color = (111, 235, 239)
        pupil_color = (4, 14, 22)
        if emotion == "sad":
            left = (76, 124 + bob)
            right = (164, 124 + bob)
        elif emotion == "curious":
            left = (73, 118 + bob)
            right = (166, 112 + bob)

        for cx, cy in (left, right):
            draw.ellipse((cx - 24, cy - 24, cx + 24, cy + 24), fill=eye_color)
            draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=pupil_color)
            draw.ellipse((cx - 6, cy - 8, cx - 1, cy - 3), fill=(236, 255, 255))

        if emotion == "excited":
            for cx, cy in (left, right):
                draw.polygon(
                    ((cx, cy - 31), (cx + 7, cy - 8), (cx + 30, cy - 8),
                     (cx + 11, cy + 6), (cx + 18, cy + 30), (cx, cy + 15),
                     (cx - 18, cy + 30), (cx - 11, cy + 6), (cx - 30, cy - 8),
                     (cx - 7, cy - 8)),
                    outline=(255, 247, 130),
                )

        if emotion == "sad":
            draw.line((56, 100 + bob, 96, 113 + bob), fill=(96, 182, 205), width=4)
            draw.line((144, 113 + bob, 184, 100 + bob), fill=(96, 182, 205), width=4)

    def _draw_cheeks(self, draw, emotion, t):
        if emotion not in ("happy", "excited", "curious"):
            return

        bob = int(math.sin(t * 2.0) * 2)
        glow = int(95 + 35 * (0.5 + 0.5 * math.sin(t * 3.0)))
        color = (255, 113, 148) if emotion != "curious" else (255, 175, 112)
        draw.ellipse((57, 153 + bob, 91, 171 + bob), fill=(glow, color[1] // 2, color[2] // 2))
        draw.ellipse((149, 153 + bob, 183, 171 + bob), fill=(glow, color[1] // 2, color[2] // 2))

    def _draw_mouth(self, draw, emotion, speaking, t):
        bob = int(math.sin(t * 2.0) * 2)
        mx = self.width // 2
        my = 169 + bob

        if speaking:
            openness = 10 + int(16 * (0.5 + 0.5 * math.sin(t * 14.0)))
            width = 28 + int(8 * math.sin(t * 9.0 + 0.5))
            draw.rounded_rectangle(
                (mx - width, my - openness // 2, mx + width, my + openness),
                radius=openness,
                fill=(99, 235, 220),
            )
            draw.rounded_rectangle(
                (mx - width + 6, my - openness // 2 + 5, mx + width - 6, my + openness - 4),
                radius=openness,
                fill=(6, 18, 27),
            )
            return

        if emotion in ("happy", "excited"):
            draw.arc((mx - 38, my - 23, mx + 38, my + 27), 10, 170, fill=(99, 235, 220), width=6)
        elif emotion == "sad":
            draw.arc((mx - 32, my + 3, mx + 32, my + 47), 190, 350, fill=(99, 235, 220), width=5)
        elif emotion == "sleepy":
            draw.line((mx - 22, my + 7, mx + 22, my + 7), fill=(99, 235, 220), width=5)
        elif emotion == "thinking":
            draw.ellipse((mx - 8, my, mx + 8, my + 16), outline=(99, 235, 220), width=4)
        else:
            draw.arc((mx - 25, my - 10, mx + 25, my + 18), 20, 160, fill=(99, 235, 220), width=5)


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
