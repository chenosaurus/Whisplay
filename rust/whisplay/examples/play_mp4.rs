use std::env;
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use whisplay::{Result, WhisplayBoard, FRAME_BYTES, LCD_HEIGHT, LCD_WIDTH};

fn main() -> Result<()> {
    let video_path = env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(default_video_path);

    let mut board = WhisplayBoard::new()?;
    board.set_backlight(true)?;

    let mut process = start_ffmpeg(&video_path)?;
    let mut frame = vec![0_u8; FRAME_BYTES];
    let mut frames = 0_u64;
    let mut last_report = Instant::now();

    println!(
        "Playing {} on {}x{} LCD. Press Ctrl+C to exit.",
        video_path.display(),
        LCD_WIDTH,
        LCD_HEIGHT
    );

    loop {
        match read_frame(&mut process, &mut frame) {
            Ok(()) => {
                board.draw_frame(&frame)?;
                frames += 1;

                let elapsed = last_report.elapsed();
                if elapsed >= Duration::from_secs(5) {
                    println!("display fps: {:.1}", frames as f64 / elapsed.as_secs_f64());
                    frames = 0;
                    last_report = Instant::now();
                }
            }
            Err(err) if err.kind() == ErrorKind::UnexpectedEof => {
                let _ = process.kill();
                let _ = process.wait();
                process = start_ffmpeg(&video_path)?;
            }
            Err(err) => return Err(err.into()),
        }
    }
}

fn read_frame(process: &mut Child, frame: &mut [u8]) -> std::io::Result<()> {
    let stdout = process
        .stdout
        .as_mut()
        .ok_or_else(|| std::io::Error::new(ErrorKind::BrokenPipe, "ffmpeg stdout closed"))?;
    stdout.read_exact(frame)
}

fn start_ffmpeg(video_path: &Path) -> std::io::Result<Child> {
    Command::new("ffmpeg")
        .args(ffmpeg_args(video_path))
        .stdout(Stdio::piped())
        .spawn()
}

fn ffmpeg_args(video_path: &Path) -> Vec<String> {
    let model = std::fs::read_to_string("/proc/device-tree/model")
        .unwrap_or_default()
        .to_lowercase();
    let mut args = Vec::new();

    if model.contains("zero 2") || model.contains("raspberry pi 3") {
        args.extend(["-threads".to_string(), "4".to_string()]);
    } else if model.contains("zero") {
        args.extend(["-vcodec".to_string(), "h264_v4l2m2m".to_string()]);
    } else if model.contains("raspberry pi 4") || model.contains("raspberry pi 5") {
        args.extend(["-threads".to_string(), "4".to_string()]);
    }

    let scale_filter = if model.contains("raspberry pi 4") || model.contains("raspberry pi 5") {
        format!("scale={}:{}:flags=bicubic", LCD_WIDTH, LCD_HEIGHT)
    } else {
        format!("scale={}:{}:flags=neighbor", LCD_WIDTH, LCD_HEIGHT)
    };

    args.extend([
        "-i".to_string(),
        video_path.display().to_string(),
        "-vf".to_string(),
        scale_filter,
        "-vcodec".to_string(),
        "rawvideo".to_string(),
        "-pix_fmt".to_string(),
        "rgb565be".to_string(),
        "-f".to_string(),
        "image2pipe".to_string(),
        "-loglevel".to_string(),
        "quiet".to_string(),
        "-".to_string(),
    ]);

    args
}

fn default_video_path() -> PathBuf {
    PathBuf::from("../../example/data/whisplay_test.mp4")
}
