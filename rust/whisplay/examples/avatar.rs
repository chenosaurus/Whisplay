use std::env;
use std::error::Error;
use std::fmt;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Duration, Instant};

use embedded_graphics::pixelcolor::{IntoStorage, Rgb565};
use minifb::{Key, Window, WindowOptions};
use whisplay::{WhisplayBoard, LCD_HEIGHT, LCD_WIDTH};

const FACE_SEQUENCE: [Emotion; 10] = [
    Emotion::Happy,
    Emotion::Wink,
    Emotion::Worried,
    Emotion::Angry,
    Emotion::Neutral,
    Emotion::Love,
    Emotion::Surprised,
    Emotion::Bored,
    Emotion::Joy,
    Emotion::Sad,
];

const AVATAR_DIRTY_RECT: Rect = Rect {
    x: 20,
    y: 44,
    width: 200,
    height: 176,
};
const MOUTH_OVERLAY_RECT: Rect = Rect {
    x: 70,
    y: 132,
    width: 100,
    height: 60,
};
const BOB_AMPLITUDE: f32 = 4.0;
const BOB_SPEED: f32 = 1.8;

type AppResult<T> = std::result::Result<T, Box<dyn Error>>;

fn main() -> AppResult<()> {
    let options = Options::parse()?;

    let running = Arc::new(AtomicBool::new(true));
    let running_for_signal = Arc::clone(&running);
    ctrlc::set_handler(move || {
        running_for_signal.store(false, Ordering::SeqCst);
    })?;

    run_avatar(options, running)?;
    Ok(())
}

fn run_avatar(options: Options, running: Arc<AtomicBool>) -> AppResult<()> {
    let mut display: Box<dyn AvatarDisplay> = if options.emulated {
        Box::new(EmulatedDisplay::new(options.emulator_scale)?)
    } else {
        Box::new(DisplayResetGuard::new(WhisplayBoard::new()?))
    };
    display.set_rgb(0, 0, 0)?;
    display.set_backlight(true)?;

    let mut avatar = RobotAvatar::new(LCD_WIDTH as usize, LCD_HEIGHT as usize);
    let avatar_width = avatar.width();
    let mut emotion = options.emotion.resolve_alias();
    let mut dirty_buffer = vec![0_u8; AVATAR_DIRTY_RECT.byte_len()];
    let mut last_cycle = Instant::now();
    let start = Instant::now();
    let frame_delay = Duration::from_secs_f64(1.0 / options.fps.max(1) as f64);
    let mut next_frame_at = Instant::now();
    let mut last_button_pressed = display.button_pressed()?;

    display.draw_frame(avatar.background())?;

    if options.emulated {
        println!(
            "Animating robot avatar. Space changes emotion, hold 1-5 for speech levels, Esc exits."
        );
    } else {
        println!(
            "Animating robot avatar. Press the WhisPlay button to change emotion, Ctrl+C to exit."
        );
    }

    while running.load(Ordering::SeqCst) && !display.should_close() {
        let now = Instant::now();
        if options.auto_cycle && last_cycle.elapsed() > Duration::from_secs(5) {
            emotion = cycle_emotion(emotion);
            last_cycle = now;
            println!("Emotion: {emotion}");
        }

        let button_pressed = display.button_pressed()?;
        if button_pressed && !last_button_pressed {
            emotion = cycle_emotion(emotion);
            last_cycle = now;
            println!("Emotion: {emotion}");
        }
        last_button_pressed = button_pressed;

        let speech_level = display.speech_level();
        let frame = avatar.draw_frame(
            emotion,
            options.speaking || speech_level.is_some(),
            start.elapsed().as_secs_f32(),
            speech_level,
        );
        copy_rect(frame, &mut dirty_buffer, avatar_width, AVATAR_DIRTY_RECT);
        display.draw_image(
            AVATAR_DIRTY_RECT.x,
            AVATAR_DIRTY_RECT.y,
            AVATAR_DIRTY_RECT.width,
            AVATAR_DIRTY_RECT.height,
            &dirty_buffer,
        )?;

        next_frame_at += frame_delay;
        if let Some(sleep_for) = next_frame_at.checked_duration_since(Instant::now()) {
            thread::sleep(sleep_for);
        } else {
            next_frame_at = Instant::now();
        }
    }

    display.reset()
}

#[derive(Clone, Copy)]
struct Rect {
    x: u16,
    y: u16,
    width: u16,
    height: u16,
}

impl Rect {
    fn byte_len(self) -> usize {
        self.width as usize * self.height as usize * 2
    }
}

struct Options {
    emotion: Emotion,
    fps: u32,
    speaking: bool,
    auto_cycle: bool,
    emulated: bool,
    emulator_scale: usize,
}

impl Options {
    fn parse() -> std::result::Result<Self, String> {
        let mut options = Self {
            emotion: Emotion::Happy,
            fps: 30,
            speaking: false,
            auto_cycle: true,
            emulated: false,
            emulator_scale: 2,
        };

        let mut args = env::args().skip(1);
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--emotion" => {
                    let value = args
                        .next()
                        .ok_or_else(|| "--emotion needs a value".to_string())?;
                    options.emotion = Emotion::parse(&value)?;
                }
                "--fps" => {
                    let value = args
                        .next()
                        .ok_or_else(|| "--fps needs a value".to_string())?;
                    options.fps = value
                        .parse::<u32>()
                        .map_err(|_| format!("invalid --fps value: {value}"))?;
                }
                "--speaking" => options.speaking = true,
                "--no-auto-cycle" => options.auto_cycle = false,
                "--emulated" => options.emulated = true,
                "--emulator-scale" => {
                    let value = args
                        .next()
                        .ok_or_else(|| "--emulator-scale needs a value".to_string())?;
                    options.emulator_scale = value
                        .parse::<usize>()
                        .map_err(|_| format!("invalid --emulator-scale value: {value}"))?
                        .max(1);
                }
                "--help" | "-h" => return Err(help_text()),
                other => return Err(format!("unknown argument: {other}\n\n{}", help_text())),
            }
        }

        Ok(options)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Emotion {
    Happy,
    Wink,
    Worried,
    Angry,
    Neutral,
    Love,
    Surprised,
    Bored,
    Joy,
    Sad,
    Curious,
    Excited,
    Sleepy,
    Thinking,
}

impl Emotion {
    fn parse(value: &str) -> std::result::Result<Self, String> {
        match value {
            "happy" => Ok(Self::Happy),
            "wink" => Ok(Self::Wink),
            "worried" => Ok(Self::Worried),
            "angry" => Ok(Self::Angry),
            "neutral" => Ok(Self::Neutral),
            "love" => Ok(Self::Love),
            "surprised" => Ok(Self::Surprised),
            "bored" => Ok(Self::Bored),
            "joy" => Ok(Self::Joy),
            "sad" => Ok(Self::Sad),
            "curious" => Ok(Self::Curious),
            "excited" => Ok(Self::Excited),
            "sleepy" => Ok(Self::Sleepy),
            "thinking" => Ok(Self::Thinking),
            _ => Err(format!("unknown emotion: {value}\n\n{}", help_text())),
        }
    }

    fn resolve_alias(self) -> Self {
        match self {
            Self::Curious => Self::Surprised,
            Self::Excited => Self::Joy,
            Self::Sleepy => Self::Bored,
            Self::Thinking => Self::Neutral,
            emotion => emotion,
        }
    }
}

impl fmt::Display for Emotion {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = match self.resolve_alias() {
            Self::Happy => "happy",
            Self::Wink => "wink",
            Self::Worried => "worried",
            Self::Angry => "angry",
            Self::Neutral => "neutral",
            Self::Love => "love",
            Self::Surprised => "surprised",
            Self::Bored => "bored",
            Self::Joy => "joy",
            Self::Sad => "sad",
            Self::Curious | Self::Excited | Self::Sleepy | Self::Thinking => unreachable!(),
        };
        formatter.write_str(value)
    }
}

fn help_text() -> String {
    "Usage: cargo run --release --example avatar -- [--emotion EMOTION] [--fps FPS] [--speaking] [--no-auto-cycle] [--emulated] [--emulator-scale SCALE]\n\nEmotions: happy, wink, worried, angry, neutral, love, surprised, bored, joy, sad, curious, excited, sleepy, thinking".to_string()
}

fn cycle_emotion(current: Emotion) -> Emotion {
    let current = current.resolve_alias();
    let index = FACE_SEQUENCE
        .iter()
        .position(|emotion| *emotion == current)
        .unwrap_or(0);
    FACE_SEQUENCE[(index + 1) % FACE_SEQUENCE.len()]
}

struct RobotAvatar {
    width: usize,
    height: usize,
    blink_until: Instant,
    next_blink: Instant,
    background: Vec<u8>,
    frame: Vec<u8>,
}

impl RobotAvatar {
    fn new(width: usize, height: usize) -> Self {
        let background = make_background(width, height);
        let frame = background.clone();
        Self {
            width,
            height,
            blink_until: Instant::now(),
            next_blink: Instant::now() + random_delay(1.5, 4.0),
            background,
            frame,
        }
    }

    fn width(&self) -> usize {
        self.width
    }

    fn background(&self) -> &[u8] {
        &self.background
    }

    fn draw_frame(
        &mut self,
        emotion: Emotion,
        speaking: bool,
        t: f32,
        speech_level: Option<f32>,
    ) -> &[u8] {
        self.frame.copy_from_slice(&self.background);
        let blink = self.maybe_blink();
        let mut surface = Surface::new(self.width, self.height, &mut self.frame);

        draw_mouth(&mut surface, emotion, t);
        if speaking || speech_level.is_some() {
            surface.copy_from(&self.background, MOUTH_OVERLAY_RECT);
            draw_speech_mouth_overlay(&mut surface, t, speech_level);
        }
        draw_eyes(&mut surface, emotion, blink, t);

        &self.frame
    }

    fn maybe_blink(&mut self) -> bool {
        let now = Instant::now();
        if now >= self.next_blink {
            self.blink_until = now + Duration::from_millis(100);
            self.next_blink = now + random_delay(2.0, 5.0);
        }
        now < self.blink_until
    }
}

struct Surface<'a> {
    width: usize,
    height: usize,
    data: &'a mut [u8],
}

impl<'a> Surface<'a> {
    fn new(width: usize, height: usize, data: &'a mut [u8]) -> Self {
        Self {
            width,
            height,
            data,
        }
    }

    fn copy_from(&mut self, source: &[u8], rect: Rect) {
        for row in 0..rect.height as usize {
            let src_start = (((rect.y as usize + row) * self.width) + rect.x as usize) * 2;
            let dst_start = src_start;
            let bytes = rect.width as usize * 2;
            self.data[dst_start..dst_start + bytes]
                .copy_from_slice(&source[src_start..src_start + bytes]);
        }
    }

    fn set_pixel(&mut self, x: i32, y: i32, color: Color) {
        if x < 0 || y < 0 || x >= self.width as i32 || y >= self.height as i32 {
            return;
        }

        let offset = ((y as usize * self.width) + x as usize) * 2;
        let color = color.rgb565();
        self.data[offset] = (color >> 8) as u8;
        self.data[offset + 1] = color as u8;
    }

    fn fill_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, color: Color) {
        for y in y0.max(0)..=y1.min(self.height as i32 - 1) {
            for x in x0.max(0)..=x1.min(self.width as i32 - 1) {
                self.set_pixel(x, y, color);
            }
        }
    }

    fn fill_circle(&mut self, cx: f32, cy: f32, radius: f32, color: Color) {
        let min_x = (cx - radius).floor() as i32;
        let max_x = (cx + radius).ceil() as i32;
        let min_y = (cy - radius).floor() as i32;
        let max_y = (cy + radius).ceil() as i32;
        let r2 = radius * radius;

        for y in min_y..=max_y {
            for x in min_x..=max_x {
                let dx = x as f32 - cx;
                let dy = y as f32 - cy;
                if dx * dx + dy * dy <= r2 {
                    self.set_pixel(x, y, color);
                }
            }
        }
    }

    fn fill_ellipse(&mut self, cx: f32, cy: f32, rx: f32, ry: f32, color: Color) {
        let min_x = (cx - rx).floor() as i32;
        let max_x = (cx + rx).ceil() as i32;
        let min_y = (cy - ry).floor() as i32;
        let max_y = (cy + ry).ceil() as i32;

        for y in min_y..=max_y {
            for x in min_x..=max_x {
                let dx = (x as f32 - cx) / rx;
                let dy = (y as f32 - cy) / ry;
                if dx * dx + dy * dy <= 1.0 {
                    self.set_pixel(x, y, color);
                }
            }
        }
    }

    fn fill_rounded_rect(&mut self, x0: i32, y0: i32, x1: i32, y1: i32, radius: i32, color: Color) {
        self.fill_rect(x0 + radius, y0, x1 - radius, y1, color);
        self.fill_rect(x0, y0 + radius, x1, y1 - radius, color);
        self.fill_circle(
            (x0 + radius) as f32,
            (y0 + radius) as f32,
            radius as f32,
            color,
        );
        self.fill_circle(
            (x1 - radius) as f32,
            (y0 + radius) as f32,
            radius as f32,
            color,
        );
        self.fill_circle(
            (x0 + radius) as f32,
            (y1 - radius) as f32,
            radius as f32,
            color,
        );
        self.fill_circle(
            (x1 - radius) as f32,
            (y1 - radius) as f32,
            radius as f32,
            color,
        );
    }

    fn line(&mut self, points: &[(f32, f32)], color: Color, width: i32) {
        for pair in points.windows(2) {
            self.line_segment(pair[0], pair[1], color, width);
        }
    }

    fn line_segment(&mut self, from: (f32, f32), to: (f32, f32), color: Color, width: i32) {
        let dx = to.0 - from.0;
        let dy = to.1 - from.1;
        let steps = dx.abs().max(dy.abs()).ceil().max(1.0) as i32;
        let radius = width.max(1) as f32 / 2.0;

        for step in 0..=steps {
            let amount = step as f32 / steps as f32;
            let x = from.0 + dx * amount;
            let y = from.1 + dy * amount;
            self.fill_circle(x, y, radius, color);
        }
    }

    fn arc(
        &mut self,
        center: (f32, f32),
        radii: (f32, f32),
        start_deg: f32,
        end_deg: f32,
        color: Color,
        width: i32,
    ) {
        let mut points = Vec::new();
        let span = end_deg - start_deg;
        let steps = (span.abs() / 4.0).ceil().max(1.0) as usize;
        for step in 0..=steps {
            let angle = (start_deg + span * step as f32 / steps as f32).to_radians();
            points.push((
                center.0 + radii.0 * angle.cos(),
                center.1 + radii.1 * angle.sin(),
            ));
        }
        self.line(&points, color, width);
    }

    fn polygon(&mut self, points: &[(f32, f32)], color: Color) {
        if points.len() < 3 {
            return;
        }

        let min_y = points
            .iter()
            .map(|(_, y)| y.floor() as i32)
            .min()
            .unwrap_or(0);
        let max_y = points
            .iter()
            .map(|(_, y)| y.ceil() as i32)
            .max()
            .unwrap_or(0);

        for y in min_y..=max_y {
            let scan_y = y as f32 + 0.5;
            let mut intersections = Vec::new();
            for index in 0..points.len() {
                let (x0, y0) = points[index];
                let (x1, y1) = points[(index + 1) % points.len()];
                if (y0 <= scan_y && y1 > scan_y) || (y1 <= scan_y && y0 > scan_y) {
                    let t = (scan_y - y0) / (y1 - y0);
                    intersections.push(x0 + t * (x1 - x0));
                }
            }
            intersections.sort_by(|left, right| left.total_cmp(right));

            for pair in intersections.chunks_exact(2) {
                for x in pair[0].ceil() as i32..=pair[1].floor() as i32 {
                    self.set_pixel(x, y, color);
                }
            }
        }
    }
}

#[derive(Clone, Copy)]
struct Color(u8, u8, u8);

impl Color {
    fn rgb565(self) -> u16 {
        Rgb565::new(self.0 >> 3, self.1 >> 2, self.2 >> 3).into_storage()
    }
}

fn make_background(width: usize, height: usize) -> Vec<u8> {
    let mut data = vec![0_u8; width * height * 2];
    let mut surface = Surface::new(width, height, &mut data);
    for y in 0..height {
        let shade = (5.0 + 12.0 * y as f32 / height as f32) as u8;
        let blue = (18.0 + 16.0 * y as f32 / height as f32) as u8;
        for x in 0..width {
            surface.set_pixel(x as i32, y as i32, Color(1, shade, blue));
        }
    }
    data
}

fn copy_rect(source: &[u8], dest: &mut [u8], source_width: usize, rect: Rect) {
    let row_bytes = rect.width as usize * 2;
    for row in 0..rect.height as usize {
        let source_start = (((rect.y as usize + row) * source_width) + rect.x as usize) * 2;
        let dest_start = row * row_bytes;
        dest[dest_start..dest_start + row_bytes]
            .copy_from_slice(&source[source_start..source_start + row_bytes]);
    }
}

fn draw_eyes(surface: &mut Surface<'_>, emotion: Emotion, blink: bool, t: f32) {
    let emotion = emotion.resolve_alias();
    let bob = bob(t);
    let ly = 107.0 + bob;
    let ry = 107.0 + bob;
    let lx = 80.0;
    let rx = 160.0;

    if blink
        && matches!(
            emotion,
            Emotion::Happy | Emotion::Neutral | Emotion::Surprised
        )
    {
        flat_eye(surface, lx, ly, None);
        flat_eye(surface, rx, ry, None);
        return;
    }

    match emotion {
        Emotion::Happy | Emotion::Neutral => {
            glow_dot(surface, lx, ly, 14.0, None);
            glow_dot(surface, rx, ry, 14.0, None);
        }
        Emotion::Wink => {
            flat_eye(surface, lx, ly, None);
            glow_dot(surface, rx, ry, 14.0, None);
        }
        Emotion::Worried => {
            sad_eye(surface, lx, ly - 3.0);
            sad_eye(surface, rx, ry - 3.0);
            line(
                surface,
                &[(lx - 18.0, ly - 28.0), (lx - 2.0, ly - 35.0)],
                None,
                4,
            );
            line(
                surface,
                &[(rx + 18.0, ry - 28.0), (rx + 2.0, ry - 35.0)],
                None,
                4,
            );
        }
        Emotion::Angry => {
            angry_eye(surface, lx, ly);
            angry_eye(surface, rx, ry);
        }
        Emotion::Love => {
            heart(surface, lx, ly);
            heart(surface, rx, ry);
        }
        Emotion::Surprised => {
            glow_dot(surface, lx, ly, 16.0, None);
            glow_dot(surface, rx, ry, 16.0, None);
        }
        Emotion::Bored => {
            flat_eye(surface, lx, ly - 2.0, None);
            flat_eye(surface, rx, ry - 2.0, None);
        }
        Emotion::Joy => {
            smile_eye(surface, lx, ly - 2.0);
            smile_eye(surface, rx, ry - 2.0);
        }
        Emotion::Sad => {
            glow_dot(surface, lx, ly + 4.0, 14.0, None);
            glow_dot(surface, rx, ry + 4.0, 14.0, None);
            line(
                surface,
                &[(lx - 18.0, ly - 14.0), (lx + 15.0, ly - 26.0)],
                None,
                4,
            );
            line(
                surface,
                &[(rx - 15.0, ry - 26.0), (rx + 18.0, ry - 14.0)],
                None,
                4,
            );
        }
        Emotion::Curious | Emotion::Excited | Emotion::Sleepy | Emotion::Thinking => unreachable!(),
    }
}

fn draw_mouth(surface: &mut Surface<'_>, emotion: Emotion, t: f32) {
    let emotion = emotion.resolve_alias();
    let mx = surface.width as f32 / 2.0;
    let my = 160.0 + bob(t);

    match emotion {
        Emotion::Happy | Emotion::Wink | Emotion::Love | Emotion::Joy => {
            smile(surface, mx, my, 44.0)
        }
        Emotion::Worried | Emotion::Sad => frown(surface, mx, my, 40.0, None, None),
        Emotion::Angry => frown(
            surface,
            mx,
            my,
            38.0,
            Some(pink()),
            Some(Color(103, 21, 62)),
        ),
        Emotion::Neutral => {}
        Emotion::Surprised => open_mouth(surface, mx, my + 4.0, 9.0),
        Emotion::Bored => line(surface, &[(mx - 18.0, my), (mx + 18.0, my)], None, 5),
        Emotion::Curious | Emotion::Excited | Emotion::Sleepy | Emotion::Thinking => unreachable!(),
    }
}

fn draw_speech_mouth_overlay(surface: &mut Surface<'_>, t: f32, speech_level: Option<f32>) {
    let mx = surface.width as f32 / 2.0;
    let my = 160.0 + bob(t);

    for (index, height) in [10.0, 18.0, 26.0, 18.0, 10.0].iter().enumerate() {
        let phase = 0.5 + 0.5 * (t * 13.0 + index as f32 * 0.9).sin();
        let amount = speech_level.unwrap_or(1.0) * phase;
        let bar_h = 5.0 + height * amount.clamp(0.0, 1.0);
        let x = mx - 20.0 + index as f32 * 10.0;
        line(
            surface,
            &[(x, my + bar_h / 2.0), (x, my - bar_h / 2.0)],
            None,
            5,
        );
    }
}

fn bob(t: f32) -> f32 {
    (t * BOB_SPEED).sin() * BOB_AMPLITUDE
}

fn cyan() -> Color {
    Color(73, 238, 246)
}

fn dim_cyan() -> Color {
    Color(16, 92, 118)
}

fn pink() -> Color {
    Color(255, 76, 143)
}

fn face_bg() -> Color {
    Color(1, 4, 12)
}

fn line(surface: &mut Surface<'_>, points: &[(f32, f32)], color: Option<Color>, width: i32) {
    surface.line(points, color.unwrap_or_else(cyan), width);
}

fn glow_dot(surface: &mut Surface<'_>, cx: f32, cy: f32, radius: f32, color: Option<Color>) {
    let color = color.unwrap_or_else(cyan);
    let eye_w = radius * 2.0 - 6.0;
    let eye_h = radius * 2.0 + 10.0;
    let x0 = cx - eye_w / 2.0;
    let y0 = cy - eye_h / 2.0;
    let x1 = cx + eye_w / 2.0;
    let y1 = cy + eye_h / 2.0;

    surface.fill_rounded_rect(
        (x0 - 5.0).round() as i32,
        (y0 - 5.0).round() as i32,
        (x1 + 5.0).round() as i32,
        (y1 + 5.0).round() as i32,
        (eye_w / 2.0 + 5.0).round() as i32,
        dim_cyan(),
    );
    surface.fill_rounded_rect(
        x0.round() as i32,
        y0.round() as i32,
        x1.round() as i32,
        y1.round() as i32,
        (eye_w / 2.0).round() as i32,
        color,
    );
    surface.fill_rounded_rect(
        (x0 + 8.0).round() as i32,
        (y0 + 7.0).round() as i32,
        (x0 + 15.0).round() as i32,
        (y0 + 17.0).round() as i32,
        4,
        Color(225, 255, 255),
    );
}

fn flat_eye(surface: &mut Surface<'_>, cx: f32, cy: f32, color: Option<Color>) {
    let color = color.unwrap_or_else(cyan);
    line(
        surface,
        &[(cx - 24.0, cy), (cx + 24.0, cy)],
        Some(dim_cyan()),
        13,
    );
    line(surface, &[(cx - 21.0, cy), (cx + 21.0, cy)], Some(color), 9);
}

fn smile_eye(surface: &mut Surface<'_>, cx: f32, cy: f32) {
    surface.arc((cx, cy + 7.5), (27.0, 25.5), 200.0, 340.0, dim_cyan(), 13);
    surface.arc((cx, cy + 7.5), (24.0, 23.5), 200.0, 340.0, cyan(), 9);
}

fn sad_eye(surface: &mut Surface<'_>, cx: f32, cy: f32) {
    surface.arc((cx, cy + 12.5), (27.0, 21.5), 200.0, 340.0, dim_cyan(), 13);
    surface.arc((cx, cy + 12.5), (24.0, 19.5), 200.0, 340.0, cyan(), 9);
}

fn angry_eye(surface: &mut Surface<'_>, cx: f32, cy: f32) {
    let color = pink();
    let dim = Color(103, 21, 62);
    let size = 16.0;
    line(
        surface,
        &[(cx - size, cy - size), (cx + size, cy + size)],
        Some(dim),
        13,
    );
    line(
        surface,
        &[(cx - size, cy + size), (cx + size, cy - size)],
        Some(dim),
        13,
    );
    line(
        surface,
        &[
            (cx - size + 2.0, cy - size + 2.0),
            (cx + size - 2.0, cy + size - 2.0),
        ],
        Some(color),
        8,
    );
    line(
        surface,
        &[
            (cx - size + 2.0, cy + size - 2.0),
            (cx + size - 2.0, cy - size + 2.0),
        ],
        Some(color),
        8,
    );
}

fn heart(surface: &mut Surface<'_>, cx: f32, cy: f32) {
    let color = pink();
    surface.fill_ellipse(cx - 13.0, cy - 8.0, 12.0, 12.0, color);
    surface.fill_ellipse(cx + 13.0, cy - 8.0, 12.0, 12.0, color);
    surface.polygon(
        &[
            (cx - 24.0, cy - 6.0),
            (cx + 24.0, cy - 6.0),
            (cx, cy + 30.0),
        ],
        color,
    );
}

fn smile(surface: &mut Surface<'_>, cx: f32, cy: f32, width: f32) {
    surface.arc(
        (cx, cy + 2.0),
        (width / 2.0, 20.0),
        15.0,
        165.0,
        dim_cyan(),
        9,
    );
    surface.arc(
        (cx, cy + 1.5),
        (width / 2.0 - 2.0, 18.5),
        15.0,
        165.0,
        cyan(),
        5,
    );
}

fn frown(
    surface: &mut Surface<'_>,
    cx: f32,
    cy: f32,
    width: f32,
    color: Option<Color>,
    dim_color: Option<Color>,
) {
    surface.arc(
        (cx, cy + 18.0),
        (width / 2.0, 16.0),
        200.0,
        340.0,
        dim_color.unwrap_or_else(dim_cyan),
        8,
    );
    surface.arc(
        (cx, cy + 18.0),
        (width / 2.0 - 2.0, 14.0),
        200.0,
        340.0,
        color.unwrap_or_else(cyan),
        5,
    );
}

fn open_mouth(surface: &mut Surface<'_>, cx: f32, cy: f32, radius: f32) {
    surface.fill_circle(cx, cy, radius + 4.0, dim_cyan());
    surface.fill_circle(cx, cy, radius, cyan());
    surface.fill_ellipse(cx, cy + 4.5, radius - 5.0, 4.5, face_bg());
}

fn random_delay(min_seconds: f64, max_seconds: f64) -> Duration {
    Duration::from_secs_f64(rand::random_range(min_seconds..max_seconds))
}

trait AvatarDisplay {
    fn draw_frame(&mut self, pixel_data: &[u8]) -> AppResult<()>;
    fn draw_image(
        &mut self,
        x: u16,
        y: u16,
        width: u16,
        height: u16,
        pixel_data: &[u8],
    ) -> AppResult<()>;
    fn set_backlight(&mut self, enabled: bool) -> AppResult<()>;
    fn set_rgb(&mut self, red: u8, green: u8, blue: u8) -> AppResult<()>;
    fn button_pressed(&mut self) -> AppResult<bool>;
    fn reset(&mut self) -> AppResult<()>;

    fn should_close(&self) -> bool {
        false
    }

    fn speech_level(&self) -> Option<f32> {
        None
    }
}

struct EmulatedDisplay {
    window: Window,
    scale: usize,
    framebuffer: Vec<u8>,
    presented: Vec<u32>,
    backlight_enabled: bool,
}

impl EmulatedDisplay {
    fn new(scale: usize) -> AppResult<Self> {
        let scale = scale.max(1);
        let window = Window::new(
            "WhisPlay ST7789 Emulator",
            LCD_WIDTH as usize * scale,
            LCD_HEIGHT as usize * scale,
            WindowOptions::default(),
        )?;
        let framebuffer = vec![0_u8; LCD_WIDTH as usize * LCD_HEIGHT as usize * 2];
        let presented = vec![0_u32; LCD_WIDTH as usize * LCD_HEIGHT as usize * scale * scale];
        let mut display = Self {
            window,
            scale,
            framebuffer,
            presented,
            backlight_enabled: true,
        };
        display.present()?;
        Ok(display)
    }

    fn present(&mut self) -> AppResult<()> {
        if !self.window.is_open() {
            return Ok(());
        }

        let source_width = LCD_WIDTH as usize;
        let source_height = LCD_HEIGHT as usize;
        let target_width = source_width * self.scale;
        let shade = if self.backlight_enabled {
            255_u16
        } else {
            0_u16
        };

        for y in 0..source_height {
            for x in 0..source_width {
                let source_index = (y * source_width + x) * 2;
                let color = ((self.framebuffer[source_index] as u16) << 8)
                    | self.framebuffer[source_index + 1] as u16;
                let rgb = rgb565_to_u32(color, shade);

                for sy in 0..self.scale {
                    let target_row = (y * self.scale + sy) * target_width;
                    for sx in 0..self.scale {
                        self.presented[target_row + x * self.scale + sx] = rgb;
                    }
                }
            }
        }

        self.window
            .update_with_buffer(
                &self.presented,
                LCD_WIDTH as usize * self.scale,
                LCD_HEIGHT as usize * self.scale,
            )
            .map_err(Into::into)
    }
}

impl AvatarDisplay for EmulatedDisplay {
    fn draw_frame(&mut self, pixel_data: &[u8]) -> AppResult<()> {
        if pixel_data.len() != self.framebuffer.len() {
            return Err(format!(
                "pixel buffer has {} bytes, expected {}",
                pixel_data.len(),
                self.framebuffer.len()
            )
            .into());
        }
        self.framebuffer.copy_from_slice(pixel_data);
        self.present()
    }

    fn draw_image(
        &mut self,
        x: u16,
        y: u16,
        width: u16,
        height: u16,
        pixel_data: &[u8],
    ) -> AppResult<()> {
        let expected = width as usize * height as usize * 2;
        if pixel_data.len() != expected {
            return Err(format!(
                "pixel buffer has {} bytes, expected {expected}",
                pixel_data.len()
            )
            .into());
        }
        if x.checked_add(width).is_none_or(|right| right > LCD_WIDTH)
            || y.checked_add(height)
                .is_none_or(|bottom| bottom > LCD_HEIGHT)
        {
            return Err("image rectangle exceeds display bounds".into());
        }

        let source_row_bytes = width as usize * 2;
        let target_width = LCD_WIDTH as usize;
        for row in 0..height as usize {
            let source_start = row * source_row_bytes;
            let target_start = (((y as usize + row) * target_width) + x as usize) * 2;
            self.framebuffer[target_start..target_start + source_row_bytes]
                .copy_from_slice(&pixel_data[source_start..source_start + source_row_bytes]);
        }

        self.present()
    }

    fn set_backlight(&mut self, enabled: bool) -> AppResult<()> {
        self.backlight_enabled = enabled;
        self.present()
    }

    fn set_rgb(&mut self, _red: u8, _green: u8, _blue: u8) -> AppResult<()> {
        Ok(())
    }

    fn button_pressed(&mut self) -> AppResult<bool> {
        Ok(self.window.is_key_down(Key::Space))
    }

    fn reset(&mut self) -> AppResult<()> {
        self.framebuffer.fill(0);
        self.backlight_enabled = false;
        self.present()
    }

    fn should_close(&self) -> bool {
        !self.window.is_open() || self.window.is_key_down(Key::Escape)
    }

    fn speech_level(&self) -> Option<f32> {
        if self.window.is_key_down(Key::Key1) {
            Some(0.15)
        } else if self.window.is_key_down(Key::Key2) {
            Some(0.35)
        } else if self.window.is_key_down(Key::Key3) {
            Some(0.55)
        } else if self.window.is_key_down(Key::Key4) {
            Some(0.75)
        } else if self.window.is_key_down(Key::Key5) {
            Some(1.0)
        } else {
            None
        }
    }
}

fn rgb565_to_u32(color: u16, shade: u16) -> u32 {
    let r = (((color >> 11) & 0x1F) * 255 / 31) * shade / 255;
    let g = (((color >> 5) & 0x3F) * 255 / 63) * shade / 255;
    let b = ((color & 0x1F) * 255 / 31) * shade / 255;
    ((r as u32) << 16) | ((g as u32) << 8) | b as u32
}

struct DisplayResetGuard {
    board: WhisplayBoard,
    needs_reset: bool,
}

impl DisplayResetGuard {
    fn new(board: WhisplayBoard) -> Self {
        Self {
            board,
            needs_reset: true,
        }
    }

    fn reset_in_place(&mut self) -> whisplay::Result<()> {
        reset_display(&mut self.board)?;
        self.needs_reset = false;
        Ok(())
    }
}

impl AvatarDisplay for DisplayResetGuard {
    fn draw_frame(&mut self, pixel_data: &[u8]) -> AppResult<()> {
        Ok(self.board.draw_frame(pixel_data)?)
    }

    fn draw_image(
        &mut self,
        x: u16,
        y: u16,
        width: u16,
        height: u16,
        pixel_data: &[u8],
    ) -> AppResult<()> {
        Ok(self.board.draw_image(x, y, width, height, pixel_data)?)
    }

    fn set_backlight(&mut self, enabled: bool) -> AppResult<()> {
        Ok(self.board.set_backlight(enabled)?)
    }

    fn set_rgb(&mut self, red: u8, green: u8, blue: u8) -> AppResult<()> {
        Ok(self.board.set_rgb(red, green, blue)?)
    }

    fn button_pressed(&mut self) -> AppResult<bool> {
        Ok(self.board.button_pressed()?)
    }

    fn reset(&mut self) -> AppResult<()> {
        Ok(self.reset_in_place()?)
    }
}

impl Drop for DisplayResetGuard {
    fn drop(&mut self) {
        if self.needs_reset {
            let _ = reset_display(&mut self.board);
        }
    }
}

fn reset_display(board: &mut WhisplayBoard) -> whisplay::Result<()> {
    let mut result = board.fill_screen(0);

    if let Err(error) = board.set_rgb(0, 0, 0) {
        if result.is_ok() {
            result = Err(error);
        }
    }

    if let Err(error) = board.set_backlight(false) {
        if result.is_ok() {
            result = Err(error);
        }
    }

    if let Err(error) = board.reset_lcd() {
        if result.is_ok() {
            result = Err(error);
        }
    }

    result
}
