import os
import time


class EmulatedWhisPlayBoard:
    """Software framebuffer for testing WhisPlay display code without hardware."""

    LCD_WIDTH = 240
    LCD_HEIGHT = 280
    CornerHeight = 20

    def __init__(self, scale=None, title="WhisPlay ST7789 Emulator"):
        try:
            import pygame
        except ImportError as exc:
            raise RuntimeError(
                "The WhisPlay emulator requires pygame. Install dependencies with: uv sync"
            ) from exc

        self._pygame = pygame
        self._scale = int(scale or os.environ.get("WHISPLAY_EMULATOR_SCALE", "2"))
        self._scale = max(1, self._scale)
        self._closed = False
        self._button_down = False
        self._button_press_callback = None
        self._button_release_callback = None
        self._key_press_callback = None
        self._key_release_callback = None
        self._current_r = 0
        self._current_g = 0
        self._current_b = 0
        self._backlight = 100
        self._rgb565_table = _rgb565_table()
        self._framebuffer = bytearray(self.LCD_WIDTH * self.LCD_HEIGHT * 3)

        pygame.init()
        pygame.display.set_caption(title)
        window_size = (self.LCD_WIDTH * self._scale, self.LCD_HEIGHT * self._scale)
        self._window = pygame.display.set_mode(window_size)
        self.fill_screen(0)
        print("WhisPlay emulator ready. Space = button, Esc/window close = exit.")

    def fill_screen(self, color):
        self._pump_events()
        rgb = self._rgb565_table[(color & 0xFFFF) * 3: (color & 0xFFFF) * 3 + 3]
        self._framebuffer[:] = rgb * (self.LCD_WIDTH * self.LCD_HEIGHT)
        self._present()

    def draw_pixel(self, x, y, color):
        if x < 0 or y < 0 or x >= self.LCD_WIDTH or y >= self.LCD_HEIGHT:
            return
        self._pump_events()
        src = (color & 0xFFFF) * 3
        dst = (y * self.LCD_WIDTH + x) * 3
        self._framebuffer[dst: dst + 3] = self._rgb565_table[src: src + 3]
        self._present()

    def draw_line(self, x0, y0, x1, y1, color):
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        while True:
            if 0 <= x0 < self.LCD_WIDTH and 0 <= y0 < self.LCD_HEIGHT:
                src = (color & 0xFFFF) * 3
                dst = (y0 * self.LCD_WIDTH + x0) * 3
                self._framebuffer[dst: dst + 3] = self._rgb565_table[src: src + 3]
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
        self._pump_events()
        self._present()

    def draw_image(self, x, y, width, height, pixel_data):
        if x < 0 or y < 0 or width < 0 or height < 0:
            raise ValueError("Image dimensions must be positive and on-screen")
        if (x + width > self.LCD_WIDTH) or (y + height > self.LCD_HEIGHT):
            raise ValueError("Image dimensions exceed screen bounds")

        expected_len = width * height * 2
        data = memoryview(pixel_data)
        if len(data) != expected_len:
            raise ValueError(f"Expected {expected_len} RGB565 bytes, got {len(data)}")

        self._pump_events()
        src_index = 0
        for row in range(height):
            dst_index = ((y + row) * self.LCD_WIDTH + x) * 3
            for _ in range(width):
                color = (data[src_index] << 8) | data[src_index + 1]
                table_index = color * 3
                self._framebuffer[dst_index: dst_index + 3] = self._rgb565_table[
                    table_index: table_index + 3
                ]
                src_index += 2
                dst_index += 3
        self._present()

    def set_backlight(self, brightness):
        self._backlight = max(0, min(100, int(brightness)))
        if not self._closed:
            self._pump_events()

    def set_backlight_mode(self, _mode):
        if not self._closed:
            self._pump_events()

    def set_rgb(self, r, g, b):
        self._current_r = max(0, min(255, int(r)))
        self._current_g = max(0, min(255, int(g)))
        self._current_b = max(0, min(255, int(b)))
        if not self._closed:
            self._pump_events()

    def set_rgb_fade(self, r_target, g_target, b_target, duration_ms=100):
        steps = 20
        delay = duration_ms / steps / 1000.0
        r0, g0, b0 = self._current_r, self._current_g, self._current_b
        for step in range(steps + 1):
            ratio = step / steps
            self.set_rgb(
                int(r0 + (r_target - r0) * ratio),
                int(g0 + (g_target - g0) * ratio),
                int(b0 + (b_target - b0) * ratio),
            )
            time.sleep(delay)

    def button_pressed(self):
        self._pump_events()
        return self._button_down

    def on_button_press(self, callback):
        self._button_press_callback = callback

    def on_button_release(self, callback):
        self._button_release_callback = callback

    def on_key_press(self, callback):
        self._key_press_callback = callback

    def on_key_release(self, callback):
        self._key_release_callback = callback

    def cleanup(self):
        if not self._closed:
            self._closed = True
            self._pygame.quit()

    def _pump_events(self):
        if self._closed:
            raise KeyboardInterrupt

        pygame = self._pygame
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.cleanup()
                raise KeyboardInterrupt
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.cleanup()
                    raise KeyboardInterrupt
                if event.key == pygame.K_SPACE and not self._button_down:
                    self._button_down = True
                    if self._button_press_callback:
                        self._button_press_callback()
                elif self._key_press_callback:
                    self._key_press_callback(getattr(event, "unicode", "") or pygame.key.name(event.key), event.key)
            elif event.type == pygame.KEYUP and event.key == pygame.K_SPACE:
                self._button_down = False
                if self._button_release_callback:
                    self._button_release_callback()
            elif event.type == pygame.KEYUP and self._key_release_callback:
                self._key_release_callback(getattr(event, "unicode", "") or pygame.key.name(event.key), event.key)

    def _present(self):
        pygame = self._pygame
        surface = pygame.image.frombuffer(
            bytes(self._framebuffer),
            (self.LCD_WIDTH, self.LCD_HEIGHT),
            "RGB",
        )
        if self._backlight < 100:
            surface = surface.copy()
            shade = max(0, min(255, int(255 * self._backlight / 100)))
            surface.fill((shade, shade, shade), special_flags=pygame.BLEND_RGB_MULT)

        if self._scale == 1:
            self._window.blit(surface, (0, 0))
        else:
            scaled = pygame.transform.scale(
                surface,
                (self.LCD_WIDTH * self._scale, self.LCD_HEIGHT * self._scale),
            )
            self._window.blit(scaled, (0, 0))
        pygame.display.flip()


def _rgb565_table():
    table = bytearray(65536 * 3)
    for color in range(65536):
        r = (color >> 11) & 0x1F
        g = (color >> 5) & 0x3F
        b = color & 0x1F
        index = color * 3
        table[index] = (r << 3) | (r >> 2)
        table[index + 1] = (g << 2) | (g >> 4)
        table[index + 2] = (b << 3) | (b >> 2)
    return bytes(table)
