use std::env;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::time::{Duration, Instant};

use whisplay::{WhisplayBoard, FRAME_BYTES};

fn main() -> std::result::Result<(), Box<dyn std::error::Error>> {
    let frames = env::args()
        .nth(1)
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(300);

    let running = Arc::new(AtomicBool::new(true));
    let running_for_signal = Arc::clone(&running);
    ctrlc::set_handler(move || {
        running_for_signal.store(false, Ordering::SeqCst);
    })?;

    let mut display = DisplayResetGuard::new(WhisplayBoard::new()?);
    display.board_mut().set_backlight(true)?;

    let mut frame = vec![0_u8; FRAME_BYTES];
    let mut frame_times = Vec::with_capacity(frames);

    println!("Pushing {frames} generated full-screen RGB565 frames...");
    let total_start = Instant::now();

    for frame_index in 0..frames {
        if !running.load(Ordering::SeqCst) {
            println!("Stopping early; resetting display...");
            break;
        }

        fill_test_pattern(&mut frame, frame_index);

        let start = Instant::now();
        display.board_mut().draw_frame(&frame)?;
        frame_times.push(start.elapsed());
    }

    let total = total_start.elapsed();
    frame_times.sort_unstable();
    let rendered_frames = frame_times.len();

    let average = if rendered_frames == 0 {
        Duration::ZERO
    } else {
        total / rendered_frames as u32
    };
    let p95 = percentile(&frame_times, 95);
    let fps = if total.is_zero() {
        0.0
    } else {
        rendered_frames as f64 / total.as_secs_f64()
    };

    println!("frames: {rendered_frames}/{frames}");
    println!("total: {:.3}s", total.as_secs_f64());
    println!("average frame write: {:.3}ms", millis(average));
    println!("p95 frame write: {:.3}ms", millis(p95));
    println!("display fps: {:.1}", fps);
    println!();
    println!("Compare with the Python video path by running:");
    println!("  cd ../../example && python3 play_mp4.py --file data/whisplay_test.mp4");

    display.reset()?;
    Ok(())
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

    fn board_mut(&mut self) -> &mut WhisplayBoard {
        &mut self.board
    }

    fn reset(mut self) -> whisplay::Result<()> {
        reset_display(&mut self.board)?;
        self.needs_reset = false;
        Ok(())
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

fn fill_test_pattern(frame: &mut [u8], frame_index: usize) {
    let color = match frame_index % 3 {
        0 => 0xF800_u16,
        1 => 0x07E0_u16,
        _ => 0x001F_u16,
    };
    let high = (color >> 8) as u8;
    let low = color as u8;

    for pixel in frame.chunks_exact_mut(2) {
        pixel[0] = high;
        pixel[1] = low;
    }
}

fn percentile(values: &[Duration], percentile: usize) -> Duration {
    if values.is_empty() {
        return Duration::ZERO;
    }

    let index = ((values.len() - 1) * percentile) / 100;
    values[index]
}

fn millis(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}
