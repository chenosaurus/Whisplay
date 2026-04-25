use std::env;
use std::time::{Duration, Instant};

use whisplay::{Result, WhisplayBoard, FRAME_BYTES};

fn main() -> Result<()> {
    let frames = env::args()
        .nth(1)
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(300);

    let mut board = WhisplayBoard::new()?;
    board.set_backlight(true)?;

    let mut frame = vec![0_u8; FRAME_BYTES];
    let mut frame_times = Vec::with_capacity(frames);

    println!("Pushing {frames} generated full-screen RGB565 frames...");
    let total_start = Instant::now();

    for frame_index in 0..frames {
        fill_test_pattern(&mut frame, frame_index);

        let start = Instant::now();
        board.draw_frame(&frame)?;
        frame_times.push(start.elapsed());
    }

    let total = total_start.elapsed();
    frame_times.sort_unstable();

    let average = total / frames as u32;
    let p95 = percentile(&frame_times, 95);
    let fps = frames as f64 / total.as_secs_f64();

    println!("frames: {frames}");
    println!("total: {:.3}s", total.as_secs_f64());
    println!("average frame write: {:.3}ms", millis(average));
    println!("p95 frame write: {:.3}ms", millis(p95));
    println!("display fps: {:.1}", fps);
    println!();
    println!("Compare with the Python video path by running:");
    println!("  cd ../../example && python3 play_mp4.py --file data/whisplay_test.mp4");

    Ok(())
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
