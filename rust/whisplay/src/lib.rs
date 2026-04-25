use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

use gpio_cdev::{Chip, LineHandle, LineRequestFlags};
use spidev::{SpiModeFlags, Spidev, SpidevOptions};
use thiserror::Error;

pub const LCD_WIDTH: u16 = 240;
pub const LCD_HEIGHT: u16 = 280;
pub const FRAME_BYTES: usize = LCD_WIDTH as usize * LCD_HEIGHT as usize * 2;

const DC_PIN: u8 = 13;
const RST_PIN: u8 = 7;
const LED_PIN: u8 = 15;
const SPI_DATA_CHUNK_BYTES: usize = 4096;

#[derive(Debug, Error)]
pub enum Error {
    #[error("unsupported platform: {model}")]
    UnsupportedPlatform { model: String },
    #[error("physical pin {pin} is not defined for {platform:?}")]
    MissingPin { platform: PlatformKind, pin: u8 },
    #[error("pixel buffer has {actual} bytes, expected {expected}")]
    InvalidFrameSize { actual: usize, expected: usize },
    #[error("image rectangle exceeds display bounds")]
    OutOfBounds,
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("GPIO error: {0}")]
    Gpio(#[from] gpio_cdev::Error),
}

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PlatformKind {
    RaspberryPi,
    RadxaZero3,
    RadxaCubieA7z,
}

#[derive(Clone, Debug)]
pub struct PlatformConfig {
    pub kind: PlatformKind,
    pub model: String,
    pub spi_bus: u8,
    pub spi_cs: u8,
    pub spi_speed_hz: u32,
    pin_map: HashMap<u8, PinLine>,
}

#[derive(Clone, Copy, Debug)]
struct PinLine {
    chip: u32,
    offset: u32,
}

impl PlatformConfig {
    pub fn detect() -> Result<Self> {
        Self::detect_from_paths("/proc/device-tree/model", "/proc/device-tree/compatible")
    }

    pub fn detect_from_paths(
        model_path: impl AsRef<Path>,
        compatible_path: impl AsRef<Path>,
    ) -> Result<Self> {
        let model = read_device_tree_string(model_path).unwrap_or_else(|_| "Unknown".to_string());
        let compatible = read_device_tree_string(compatible_path).unwrap_or_default();
        let model_lower = model.to_lowercase();
        let compat_lower = compatible.to_lowercase();

        if model_lower.contains("raspberry") {
            let chip = if model.contains("Raspberry Pi 5") {
                4
            } else {
                0
            };
            return Ok(Self {
                kind: PlatformKind::RaspberryPi,
                model,
                spi_bus: 0,
                spi_cs: 0,
                spi_speed_hz: 100_000_000,
                pin_map: rpi_pin_map(chip),
            });
        }

        if model_lower.contains("radxa") || compat_lower.contains("radxa") {
            if compat_lower.contains("cubie-a7z") {
                return Ok(Self {
                    kind: PlatformKind::RadxaCubieA7z,
                    model,
                    spi_bus: 1,
                    spi_cs: 0,
                    spi_speed_hz: 48_000_000,
                    pin_map: map_from_pairs(RADXA_CUBIE_A7Z_PIN_MAP),
                });
            }

            return Ok(Self {
                kind: PlatformKind::RadxaZero3,
                model,
                spi_bus: 3,
                spi_cs: 0,
                spi_speed_hz: 48_000_000,
                pin_map: map_from_pairs(RADXA_ZERO3_PIN_MAP),
            });
        }

        Err(Error::UnsupportedPlatform { model })
    }

    fn pin(&self, pin: u8) -> Result<PinLine> {
        self.pin_map.get(&pin).copied().ok_or(Error::MissingPin {
            platform: self.kind,
            pin,
        })
    }
}

pub struct WhisplayBoard {
    config: PlatformConfig,
    spi: Spidev,
    dc: LineHandle,
    rst: LineHandle,
    backlight: LineHandle,
}

impl WhisplayBoard {
    pub fn new() -> Result<Self> {
        Self::open(PlatformConfig::detect()?)
    }

    pub fn open(config: PlatformConfig) -> Result<Self> {
        let dc = request_output(&config, DC_PIN, 0)?;
        let rst = request_output(&config, RST_PIN, 0)?;
        let backlight = request_output(&config, LED_PIN, 0)?;
        let spi = open_spi(&config)?;

        let mut board = Self {
            config,
            spi,
            dc,
            rst,
            backlight,
        };

        board.set_backlight(true)?;
        board.reset_lcd()?;
        board.init_display()?;
        board.fill_screen(0)?;
        Ok(board)
    }

    pub fn config(&self) -> &PlatformConfig {
        &self.config
    }

    pub fn set_backlight(&mut self, enabled: bool) -> Result<()> {
        self.backlight.set_value(if enabled { 0 } else { 1 })?;
        Ok(())
    }

    pub fn reset_lcd(&mut self) -> Result<()> {
        self.rst.set_value(1)?;
        thread::sleep(Duration::from_millis(100));
        self.rst.set_value(0)?;
        thread::sleep(Duration::from_millis(100));
        self.rst.set_value(1)?;
        thread::sleep(Duration::from_millis(120));
        Ok(())
    }

    pub fn fill_screen(&mut self, color: u16) -> Result<()> {
        let mut frame = vec![0_u8; FRAME_BYTES];
        let high = (color >> 8) as u8;
        let low = color as u8;

        for pixel in frame.chunks_exact_mut(2) {
            pixel[0] = high;
            pixel[1] = low;
        }

        self.draw_image(0, 0, LCD_WIDTH, LCD_HEIGHT, &frame)
    }

    pub fn draw_frame(&mut self, pixel_data: &[u8]) -> Result<()> {
        if pixel_data.len() != FRAME_BYTES {
            return Err(Error::InvalidFrameSize {
                actual: pixel_data.len(),
                expected: FRAME_BYTES,
            });
        }
        self.draw_image(0, 0, LCD_WIDTH, LCD_HEIGHT, pixel_data)
    }

    pub fn draw_image(
        &mut self,
        x: u16,
        y: u16,
        width: u16,
        height: u16,
        pixel_data: &[u8],
    ) -> Result<()> {
        let expected = width as usize * height as usize * 2;
        if pixel_data.len() != expected {
            return Err(Error::InvalidFrameSize {
                actual: pixel_data.len(),
                expected,
            });
        }
        if x.checked_add(width).is_none_or(|right| right > LCD_WIDTH)
            || y.checked_add(height)
                .is_none_or(|bottom| bottom > LCD_HEIGHT)
        {
            return Err(Error::OutOfBounds);
        }

        self.set_window(x, y, x + width - 1, y + height - 1)?;
        self.send_data(pixel_data)
    }

    pub fn set_window(&mut self, x0: u16, y0: u16, x1: u16, y1: u16) -> Result<()> {
        self.send_command(0x2A, &u16_pair(x0, x1))?;
        self.send_command(0x2B, &u16_pair(y0 + 20, y1 + 20))?;
        self.send_command(0x2C, &[])
    }

    fn init_display(&mut self) -> Result<()> {
        self.send_command(0x11, &[])?;
        thread::sleep(Duration::from_millis(120));
        self.send_command(0x36, &[0xC0])?;
        self.send_command(0x3A, &[0x05])?;
        self.send_command(0xB2, &[0x0C, 0x0C, 0x00, 0x33, 0x33])?;
        self.send_command(0xB7, &[0x35])?;
        self.send_command(0xBB, &[0x32])?;
        self.send_command(0xC2, &[0x01])?;
        self.send_command(0xC3, &[0x15])?;
        self.send_command(0xC4, &[0x20])?;
        self.send_command(0xC6, &[0x0F])?;
        self.send_command(0xD0, &[0xA4, 0xA1])?;
        self.send_command(
            0xE0,
            &[
                0xD0, 0x08, 0x0E, 0x09, 0x09, 0x05, 0x31, 0x33, 0x48, 0x17, 0x14, 0x15, 0x31, 0x34,
            ],
        )?;
        self.send_command(
            0xE1,
            &[
                0xD0, 0x08, 0x0E, 0x09, 0x09, 0x15, 0x31, 0x33, 0x48, 0x17, 0x14, 0x15, 0x31, 0x34,
            ],
        )?;
        self.send_command(0x21, &[])?;
        self.send_command(0x29, &[])
    }

    fn send_command(&mut self, command: u8, args: &[u8]) -> Result<()> {
        self.dc.set_value(0)?;
        self.spi.write_all(&[command])?;
        if !args.is_empty() {
            self.send_data(args)?;
        }
        Ok(())
    }

    fn send_data(&mut self, data: &[u8]) -> Result<()> {
        self.dc.set_value(1)?;
        for chunk in data.chunks(SPI_DATA_CHUNK_BYTES) {
            self.spi.write_all(chunk)?;
        }
        Ok(())
    }
}

fn open_spi(config: &PlatformConfig) -> Result<Spidev> {
    let path = PathBuf::from(format!("/dev/spidev{}.{}", config.spi_bus, config.spi_cs));
    let mut spi = Spidev::open(path)?;
    let options = SpidevOptions::new()
        .bits_per_word(8)
        .max_speed_hz(config.spi_speed_hz)
        .mode(SpiModeFlags::SPI_MODE_0)
        .build();
    spi.configure(&options)?;
    Ok(spi)
}

fn request_output(config: &PlatformConfig, pin: u8, default_value: u8) -> Result<LineHandle> {
    let line = config.pin(pin)?;
    let mut chip = Chip::new(format!("/dev/gpiochip{}", line.chip))?;
    Ok(chip.get_line(line.offset)?.request(
        LineRequestFlags::OUTPUT,
        default_value,
        "whisplay-rs",
    )?)
}

fn u16_pair(first: u16, second: u16) -> [u8; 4] {
    [
        (first >> 8) as u8,
        first as u8,
        (second >> 8) as u8,
        second as u8,
    ]
}

fn read_device_tree_string(path: impl AsRef<Path>) -> std::io::Result<String> {
    let mut contents = Vec::new();
    fs::File::open(path)?.read_to_end(&mut contents)?;
    while contents.last() == Some(&0) {
        contents.pop();
    }
    Ok(String::from_utf8_lossy(&contents).trim().to_string())
}

fn rpi_pin_map(chip: u32) -> HashMap<u8, PinLine> {
    let pairs: Vec<(u8, u32, u32)> = RPI_BOARD_TO_BCM
        .iter()
        .map(|(pin, bcm)| (*pin, chip, *bcm))
        .collect();
    map_from_pairs(&pairs)
}

fn map_from_pairs(pairs: &[(u8, u32, u32)]) -> HashMap<u8, PinLine> {
    pairs
        .iter()
        .map(|(pin, chip, offset)| {
            (
                *pin,
                PinLine {
                    chip: *chip,
                    offset: *offset,
                },
            )
        })
        .collect()
}

const RPI_BOARD_TO_BCM: &[(u8, u32)] = &[
    (3, 2),
    (5, 3),
    (7, 4),
    (8, 14),
    (10, 15),
    (11, 17),
    (12, 18),
    (13, 27),
    (15, 22),
    (16, 23),
    (18, 24),
    (19, 10),
    (21, 9),
    (22, 25),
    (23, 11),
    (24, 8),
    (26, 7),
    (27, 0),
    (28, 1),
    (29, 5),
    (31, 6),
    (32, 12),
    (33, 13),
    (35, 19),
    (36, 16),
    (37, 26),
    (38, 20),
    (40, 21),
];

const RADXA_ZERO3_PIN_MAP: &[(u8, u32, u32)] = &[
    (3, 1, 0),
    (5, 1, 1),
    (7, 3, 20),
    (8, 0, 25),
    (10, 0, 24),
    (11, 3, 1),
    (12, 3, 3),
    (13, 3, 2),
    (15, 3, 8),
    (16, 3, 9),
    (18, 3, 10),
    (19, 4, 19),
    (21, 4, 21),
    (22, 3, 17),
    (23, 4, 18),
    (24, 4, 22),
    (26, 4, 25),
    (27, 4, 10),
    (28, 4, 11),
    (29, 3, 11),
    (31, 3, 12),
    (32, 3, 18),
    (33, 3, 19),
    (35, 3, 4),
    (36, 3, 7),
    (37, 1, 4),
    (38, 3, 6),
    (40, 3, 5),
];

const RADXA_CUBIE_A7Z_PIN_MAP: &[(u8, u32, u32)] = &[
    (3, 0, 311),
    (5, 0, 310),
    (7, 0, 32),
    (8, 0, 41),
    (10, 0, 42),
    (11, 0, 33),
    (12, 0, 37),
    (13, 1, 6),
    (15, 1, 7),
    (16, 0, 312),
    (18, 0, 313),
    (19, 0, 108),
    (21, 0, 109),
    (22, 1, 5),
    (23, 0, 107),
    (24, 0, 106),
    (26, 0, 110),
    (27, 0, 113),
    (28, 0, 112),
    (29, 0, 34),
    (31, 0, 35),
    (32, 1, 37),
    (33, 1, 35),
    (35, 0, 38),
    (36, 0, 36),
    (37, 1, 36),
    (38, 0, 40),
    (40, 0, 39),
];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn full_frame_size_matches_lcd_geometry() {
        assert_eq!(FRAME_BYTES, 134_400);
    }

    #[test]
    fn u16_pair_is_big_endian() {
        assert_eq!(u16_pair(0x0123, 0x4567), [0x01, 0x23, 0x45, 0x67]);
    }
}
