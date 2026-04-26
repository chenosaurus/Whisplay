#[allow(dead_code)]
mod avatar {
    include!("avatar.rs");

    pub struct ClientAvatarOptions {
        pub fps: u32,
        pub speaking: bool,
        pub auto_cycle: bool,
        pub emulated: bool,
        pub emulator_scale: usize,
    }

    pub fn run_client_avatar(
        options: ClientAvatarOptions,
        running: Arc<AtomicBool>,
        speech_level_state: Arc<std::sync::atomic::AtomicU32>,
    ) -> AppResult<()> {
        let mut display: Box<dyn AvatarDisplay> = if options.emulated {
            create_emulated_display(options.emulator_scale)?
        } else {
            Box::new(DisplayResetGuard::new(WhisplayBoard::new()?))
        };
        display.set_rgb(0, 0, 0)?;
        display.set_backlight(true)?;

        let mut avatar = RobotAvatar::new(LCD_WIDTH as usize, LCD_HEIGHT as usize);
        let avatar_width = avatar.width();
        let mut emotion = Emotion::Happy;
        let mut dirty_buffer = vec![0_u8; AVATAR_DIRTY_RECT.byte_len()];
        let mut last_cycle = Instant::now();
        let start = Instant::now();
        let frame_delay = Duration::from_secs_f64(1.0 / options.fps.max(1) as f64);
        let mut next_frame_at = Instant::now();
        let mut last_button_pressed = display.button_pressed()?;

        display.draw_frame(avatar.background())?;

        if options.emulated {
            println!(
                "Running avatar client. Space changes emotion, hold 1-5 for speech levels, Esc exits."
            );
        } else {
            println!(
                "Running avatar client. Press the WhisPlay button to change emotion, Ctrl+C to exit."
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

            let speech_level = display
                .speech_level()
                .or_else(|| shared_speech_level(&speech_level_state));
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

        running.store(false, Ordering::SeqCst);
        display.reset()
    }

    fn shared_speech_level(state: &std::sync::atomic::AtomicU32) -> Option<f32> {
        let level = f32::from_bits(state.load(Ordering::Relaxed));
        (level > 0.02).then_some(level.clamp(0.0, 1.0))
    }
}

mod db_meter;

use std::collections::VecDeque;
use std::env;
use std::sync::{
    atomic::{AtomicBool, AtomicU32, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use clap::Parser;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{
    Device, FromSample, Sample, SampleFormat, SampleRate, SizedSample, Stream, StreamConfig,
};
use db_meter::{calculate_db_level, display_dual_db_meters};
use futures_util::StreamExt;
use libwebrtc::native::apm::AudioProcessingModule;
use livekit::{
    options::TrackPublishOptions,
    track::{LocalAudioTrack, LocalTrack, TrackSource},
    webrtc::{
        audio_frame::AudioFrame,
        audio_source::native::NativeAudioSource,
        audio_stream::native::NativeAudioStream,
        prelude::{AudioSourceOptions, RtcAudioSource},
    },
    Room, RoomEvent, RoomOptions,
};
use livekit_api::access_token;
use log::{debug, error, info, warn};
use tokio::sync::mpsc;

const DEFAULT_SAMPLE_RATE: u32 = 48_000;
const DEFAULT_CHANNELS: u16 = 1;
const DEFAULT_WM8960_NAME: &str = "wm8960soundcard";

#[derive(Parser, Debug)]
#[command(author, version, about = "Whisplay avatar LiveKit audio client")]
struct Args {
    /// List available audio devices and exit
    #[arg(long)]
    list_devices: bool,

    /// Audio input device name to use. Defaults to a WM8960 device if available.
    #[arg(short = 'i', long)]
    input_device: Option<String>,

    /// Audio output device name to use. Defaults to a WM8960 device if available.
    #[arg(short = 'o', long)]
    output_device: Option<String>,

    /// Input channel index to capture from an interleaved input device
    #[arg(long, default_value_t = 0)]
    channel: u32,

    /// Sample rate in Hz
    #[arg(short, long, default_value_t = DEFAULT_SAMPLE_RATE)]
    sample_rate: u32,

    /// Master playback volume, from 0.0 to 1.0
    #[arg(long, default_value_t = 1.0)]
    volume: f32,

    /// Initial APM stream delay estimate in milliseconds
    #[arg(long, default_value_t = 50)]
    stream_delay_ms: i32,

    /// Enable APM echo cancellation
    #[arg(long, default_value_t = true)]
    echo_cancellation: bool,

    /// Enable APM noise suppression
    #[arg(long, default_value_t = true)]
    noise_suppression: bool,

    /// Enable APM auto gain control
    #[arg(long, default_value_t = true)]
    auto_gain_control: bool,

    /// Disable remote room audio playback
    #[arg(long)]
    no_playback: bool,

    /// LiveKit server URL. Can also be set with LIVEKIT_URL.
    #[arg(long)]
    url: Option<String>,

    /// LiveKit access token. Can also be set with LIVEKIT_TOKEN.
    #[arg(long)]
    token: Option<String>,

    /// LiveKit API key. Can also be set with LIVEKIT_API_KEY.
    #[arg(long)]
    api_key: Option<String>,

    /// LiveKit API secret. Can also be set with LIVEKIT_API_SECRET.
    #[arg(long)]
    api_secret: Option<String>,

    /// LiveKit room name
    #[arg(long, default_value = "whisplay-avatar")]
    room_name: String,

    /// LiveKit participant identity
    #[arg(long, default_value = "whisplay-avatar-client")]
    identity: String,

    /// Avatar frames per second
    #[arg(long, default_value_t = 30)]
    fps: u32,

    /// Keep the avatar mouth in speech animation
    #[arg(long)]
    speaking: bool,

    /// Disable automatic face cycling
    #[arg(long)]
    no_auto_cycle: bool,

    /// Use the desktop display emulator instead of Whisplay hardware
    #[arg(long)]
    emulated: bool,

    /// Desktop emulator scale
    #[arg(long, default_value_t = 2)]
    emulator_scale: usize,
}

struct EchoCancellationProcessor {
    apm: AudioProcessingModule,
    sample_rate: u32,
    frame_size: usize,
}

impl EchoCancellationProcessor {
    fn new(
        echo_cancellation: bool,
        noise_suppression: bool,
        auto_gain_control: bool,
        sample_rate: u32,
        stream_delay_ms: i32,
    ) -> Self {
        let apm = AudioProcessingModule::new(
            echo_cancellation,
            auto_gain_control,
            false,
            noise_suppression,
        );
        let mut processor = Self {
            apm,
            sample_rate,
            frame_size: (sample_rate / 100) as usize,
        };
        processor.set_stream_delay(stream_delay_ms);
        processor
    }

    fn process_microphone_audio(&mut self, audio_data: &mut [i16]) {
        if audio_data.len() != self.frame_size {
            warn!(
                "APM microphone frame has {} samples, expected {}",
                audio_data.len(),
                self.frame_size
            );
            return;
        }

        if let Err(error) = self
            .apm
            .process_stream(audio_data, self.sample_rate as i32, 1)
        {
            warn!("APM process_stream failed: {error}");
        }
    }

    fn process_reference_audio(&mut self, audio_data: &mut [i16]) {
        if audio_data.len() != self.frame_size {
            warn!(
                "APM reference frame has {} samples, expected {}",
                audio_data.len(),
                self.frame_size
            );
            return;
        }

        if let Err(error) = self
            .apm
            .process_reverse_stream(audio_data, self.sample_rate as i32, 1)
        {
            warn!("APM process_reverse_stream failed: {error}");
        }
    }

    fn set_stream_delay(&mut self, delay_ms: i32) {
        if let Err(error) = self.apm.set_stream_delay_ms(delay_ms) {
            warn!("APM set_stream_delay_ms failed: {error}");
        }
    }
}

struct AudioCapture {
    _stream: Stream,
    is_running: Arc<AtomicBool>,
}

impl AudioCapture {
    fn new(
        device: Device,
        config: StreamConfig,
        sample_format: SampleFormat,
        audio_tx: mpsc::UnboundedSender<Vec<i16>>,
        db_tx: Option<mpsc::UnboundedSender<f32>>,
        channel_index: u32,
        num_input_channels: u32,
    ) -> Result<Self> {
        let is_running = Arc::new(AtomicBool::new(true));
        let stream = match sample_format {
            SampleFormat::F32 => Self::create_input_stream::<f32>(
                device,
                config,
                audio_tx,
                db_tx,
                Arc::clone(&is_running),
                channel_index,
                num_input_channels,
            )?,
            SampleFormat::I16 => Self::create_input_stream::<i16>(
                device,
                config,
                audio_tx,
                db_tx,
                Arc::clone(&is_running),
                channel_index,
                num_input_channels,
            )?,
            SampleFormat::U16 => Self::create_input_stream::<u16>(
                device,
                config,
                audio_tx,
                db_tx,
                Arc::clone(&is_running),
                channel_index,
                num_input_channels,
            )?,
            sample_format => {
                return Err(anyhow!(
                    "unsupported input sample format: {sample_format:?}"
                ))
            }
        };

        stream.play()?;
        info!("Audio capture stream started");
        Ok(Self {
            _stream: stream,
            is_running,
        })
    }

    fn create_input_stream<T>(
        device: Device,
        config: StreamConfig,
        audio_tx: mpsc::UnboundedSender<Vec<i16>>,
        db_tx: Option<mpsc::UnboundedSender<f32>>,
        is_running: Arc<AtomicBool>,
        channel_index: u32,
        num_input_channels: u32,
    ) -> Result<Stream>
    where
        T: SizedSample + Send + 'static,
    {
        let mut logged_first_buffer = false;
        let stream = device.build_input_stream(
            &config,
            move |data: &[T], _: &cpal::InputCallbackInfo| {
                if !is_running.load(Ordering::Relaxed) {
                    return;
                }

                let converted: Vec<i16> = data
                    .iter()
                    .skip(channel_index as usize)
                    .step_by(num_input_channels as usize)
                    .map(|&sample| convert_sample_to_i16(sample))
                    .collect();

                if let Some(ref db_sender) = db_tx {
                    let db_level = calculate_db_level(&converted);
                    if !logged_first_buffer {
                        info!(
                            "Mic capture callback active: {} raw samples, {} selected-channel samples, channel={}, input_channels={}, first_db={:.1}",
                            data.len(),
                            converted.len(),
                            channel_index,
                            num_input_channels,
                            db_level
                        );
                        logged_first_buffer = true;
                    }
                    if let Err(error) = db_sender.send(db_level) {
                        warn!("Failed to send mic dB level: {error}");
                    }
                }

                if let Err(error) = audio_tx.send(converted) {
                    warn!("Failed to send audio data: {error}");
                }
            },
            move |error| {
                error!("Audio input stream error: {error}");
            },
            None,
        )?;

        Ok(stream)
    }
}

impl Drop for AudioCapture {
    fn drop(&mut self) {
        self.is_running.store(false, Ordering::Relaxed);
    }
}

struct AudioPlayback {
    _stream: Stream,
    is_running: Arc<AtomicBool>,
}

impl AudioPlayback {
    fn new(
        device: Device,
        config: StreamConfig,
        sample_format: SampleFormat,
        mixer: AudioMixer,
    ) -> Result<Self> {
        let is_running = Arc::new(AtomicBool::new(true));
        let stream = match sample_format {
            SampleFormat::F32 => {
                Self::create_output_stream::<f32>(device, config, mixer, Arc::clone(&is_running))?
            }
            SampleFormat::I16 => {
                Self::create_output_stream::<i16>(device, config, mixer, Arc::clone(&is_running))?
            }
            SampleFormat::U16 => {
                Self::create_output_stream::<u16>(device, config, mixer, Arc::clone(&is_running))?
            }
            sample_format => {
                return Err(anyhow!(
                    "unsupported output sample format: {sample_format:?}"
                ));
            }
        };

        stream.play()?;
        info!("Audio playback stream started");
        Ok(Self {
            _stream: stream,
            is_running,
        })
    }

    fn create_output_stream<T>(
        device: Device,
        config: StreamConfig,
        mixer: AudioMixer,
        is_running: Arc<AtomicBool>,
    ) -> Result<Stream>
    where
        T: SizedSample + Sample + Send + 'static + FromSample<f32>,
    {
        let stream = device.build_output_stream(
            &config,
            move |data: &mut [T], _: &cpal::OutputCallbackInfo| {
                if !is_running.load(Ordering::Relaxed) {
                    for sample in data.iter_mut() {
                        *sample = Sample::from_sample(0.0_f32);
                    }
                    return;
                }

                let mixed_samples = mixer.get_samples(data.len());
                for (sample, mixed_sample) in data.iter_mut().zip(mixed_samples) {
                    *sample = convert_i16_to_sample::<T>(mixed_sample);
                }
            },
            move |error| {
                error!("Audio output stream error: {error}");
            },
            None,
        )?;

        Ok(stream)
    }
}

impl Drop for AudioPlayback {
    fn drop(&mut self) {
        self.is_running.store(false, Ordering::Relaxed);
    }
}

#[derive(Clone)]
struct AudioMixer {
    buffer: Arc<Mutex<VecDeque<i16>>>,
    volume: f32,
    max_buffer_size: usize,
    db_tx: Option<mpsc::UnboundedSender<f32>>,
    reference_audio_tx: Option<mpsc::UnboundedSender<Vec<i16>>>,
    speech_level: Arc<AtomicU32>,
}

impl AudioMixer {
    fn with_reference_audio(
        sample_rate: u32,
        channels: u32,
        volume: f32,
        db_tx: mpsc::UnboundedSender<f32>,
        reference_audio_tx: mpsc::UnboundedSender<Vec<i16>>,
        speech_level: Arc<AtomicU32>,
    ) -> Self {
        let max_buffer_size = sample_rate as usize * channels as usize;
        Self {
            buffer: Arc::new(Mutex::new(VecDeque::with_capacity(max_buffer_size))),
            volume: volume.clamp(0.0, 1.0),
            max_buffer_size,
            db_tx: Some(db_tx),
            reference_audio_tx: Some(reference_audio_tx),
            speech_level,
        }
    }

    fn add_audio_data(&self, data: &[i16]) {
        let mut buffer = self.buffer.lock().expect("audio mixer lock poisoned");
        for &sample in data {
            buffer.push_back((sample as f32 * self.volume) as i16);
            if buffer.len() > self.max_buffer_size {
                buffer.pop_front();
            }
        }
    }

    fn get_samples(&self, requested_samples: usize) -> Vec<i16> {
        let mut buffer = self.buffer.lock().expect("audio mixer lock poisoned");
        let mut result = Vec::with_capacity(requested_samples);

        for _ in 0..requested_samples {
            result.push(buffer.pop_front().unwrap_or(0));
        }

        self.speech_level
            .store(calculate_speech_level(&result).to_bits(), Ordering::Relaxed);

        if let Some(db_tx) = &self.db_tx {
            let db_level = calculate_db_level(&result);
            let _ = db_tx.send(db_level);
        }

        if let Some(reference_audio_tx) = &self.reference_audio_tx {
            if reference_audio_tx.send(result.clone()).is_err() {
                debug!("Reference audio channel closed");
            }
        }

        result
    }
}

fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args = Args::parse();
    if args.list_devices {
        return list_audio_devices();
    }
    if !(0.0..=1.0).contains(&args.volume) {
        return Err(anyhow!("--volume must be between 0.0 and 1.0"));
    }

    let running = Arc::new(AtomicBool::new(true));
    let running_for_signal = Arc::clone(&running);
    ctrlc::set_handler(move || {
        running_for_signal.store(false, Ordering::SeqCst);
    })?;

    let speech_level = Arc::new(AtomicU32::new(0.0_f32.to_bits()));
    let avatar_options = avatar::ClientAvatarOptions {
        fps: args.fps,
        speaking: args.speaking,
        auto_cycle: !args.no_auto_cycle,
        emulated: args.emulated,
        emulator_scale: args.emulator_scale,
    };

    let audio_running = Arc::clone(&running);
    let audio_speech_level = Arc::clone(&speech_level);
    let audio_thread =
        thread::spawn(move || run_audio_thread(args, audio_speech_level, audio_running));

    let avatar_result =
        avatar::run_client_avatar(avatar_options, Arc::clone(&running), speech_level)
            .map_err(|error| anyhow!("avatar display failed: {error}"));
    running.store(false, Ordering::SeqCst);

    let audio_result = audio_thread
        .join()
        .map_err(|_| anyhow!("audio client thread panicked"))?;

    avatar_result?;
    audio_result.map_err(|error| anyhow!("audio client failed: {error}"))?;
    Ok(())
}

fn run_audio_thread(
    args: Args,
    speech_level: Arc<AtomicU32>,
    running: Arc<AtomicBool>,
) -> std::result::Result<(), String> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())?;
    let running_for_error = Arc::clone(&running);

    runtime
        .block_on(async move {
            let audio_runtime = start_audio_client(&args, speech_level).await?;
            info!("Avatar client connected. Press Ctrl+C to stop.");

            while running.load(Ordering::SeqCst) {
                tokio::time::sleep(Duration::from_millis(100)).await;
            }

            audio_runtime.shutdown().await;
            Ok(())
        })
        .map_err(|error: anyhow::Error| {
            running_for_error.store(false, Ordering::SeqCst);
            error.to_string()
        })
}

struct AudioRuntime {
    room: Arc<Room>,
    streaming_task: tokio::task::JoinHandle<Result<()>>,
    reference_task: tokio::task::JoinHandle<Result<()>>,
    db_meter_task: tokio::task::JoinHandle<Result<()>>,
    remote_audio_task: Option<tokio::task::JoinHandle<Result<()>>>,
    _audio_capture: AudioCapture,
    _audio_playback: Option<AudioPlayback>,
}

impl AudioRuntime {
    async fn shutdown(self) {
        self.streaming_task.abort();
        self.reference_task.abort();
        self.db_meter_task.abort();
        if let Some(task) = self.remote_audio_task {
            task.abort();
        }
        if let Err(error) = self.room.close().await {
            warn!("Failed to close LiveKit room: {error}");
        }
    }
}

async fn start_audio_client(args: &Args, speech_level: Arc<AtomicU32>) -> Result<AudioRuntime> {
    let url = args
        .url
        .clone()
        .or_else(|| env::var("LIVEKIT_URL").ok())
        .ok_or_else(|| anyhow!("provide --url or LIVEKIT_URL"))?;
    let token = resolve_livekit_token(args)?;

    let mut room_options = RoomOptions::default();
    room_options.auto_subscribe = true;
    info!(
        "Connecting to LiveKit room '{}' as '{}'...",
        args.room_name, args.identity
    );
    let (room, _) = Room::connect(&url, &token, room_options).await?;
    let room = Arc::new(room);
    info!("Connected to room: {}", room.name());

    let host = cpal::default_host();
    print_audio_device_inventory(&host);

    let input_device = select_input_device(&host, args.input_device.as_deref())?;
    let input_name = input_device
        .name()
        .unwrap_or_else(|_| "Unknown input".to_string());

    let input_supported_config = input_device.default_input_config()?;
    let supported_channels = input_supported_config.channels() as u32;
    if args.channel >= supported_channels {
        return Err(anyhow!(
            "--channel {} is invalid for input device with {} channels",
            args.channel,
            supported_channels
        ));
    }
    let input_config = StreamConfig {
        channels: input_supported_config.channels(),
        sample_rate: SampleRate(args.sample_rate),
        buffer_size: cpal::BufferSize::Default,
    };
    println!(
        "Selected audio input: name='{input_name}', default={}Hz/{}ch/{:?}, stream={}Hz/{}ch/{:?}, capture_channel={}",
        input_supported_config.sample_rate().0,
        input_supported_config.channels(),
        input_supported_config.sample_format(),
        input_config.sample_rate.0,
        input_config.channels,
        input_supported_config.sample_format(),
        args.channel
    );

    let output_device = if args.no_playback {
        None
    } else {
        Some(select_output_device(&host, args.output_device.as_deref())?)
    };
    let output_config = output_device.as_ref().map(|_| StreamConfig {
        channels: DEFAULT_CHANNELS,
        sample_rate: SampleRate(args.sample_rate),
        buffer_size: cpal::BufferSize::Default,
    });

    let livekit_source = NativeAudioSource::new(
        AudioSourceOptions {
            echo_cancellation: false,
            noise_suppression: false,
            auto_gain_control: false,
        },
        args.sample_rate,
        DEFAULT_CHANNELS as u32,
        1000,
    );
    let track = LocalAudioTrack::create_audio_track(
        "whisplay-microphone",
        RtcAudioSource::Native(livekit_source.clone()),
    );
    room.local_participant()
        .publish_track(
            LocalTrack::Audio(track),
            TrackPublishOptions {
                source: TrackSource::Microphone,
                ..Default::default()
            },
        )
        .await?;
    info!("Published microphone audio track");

    let echo_processor = Arc::new(tokio::sync::Mutex::new(EchoCancellationProcessor::new(
        args.echo_cancellation,
        args.noise_suppression,
        args.auto_gain_control,
        args.sample_rate,
        args.stream_delay_ms,
    )));
    info!(
        "APM enabled: echo_cancellation={}, noise_suppression={}, auto_gain_control={}",
        args.echo_cancellation, args.noise_suppression, args.auto_gain_control
    );

    let (audio_tx, audio_rx) = mpsc::unbounded_channel();
    let (reference_audio_tx, reference_audio_rx) = mpsc::unbounded_channel();
    let (mic_db_tx, mic_db_rx) = mpsc::unbounded_channel();
    let (room_db_tx, room_db_rx) = mpsc::unbounded_channel();
    let db_meter_task = tokio::spawn(display_dual_db_meters(mic_db_rx, room_db_rx));

    let audio_capture = AudioCapture::new(
        input_device,
        input_config,
        input_supported_config.sample_format(),
        audio_tx,
        Some(mic_db_tx),
        args.channel,
        supported_channels,
    )?;

    let streaming_task = tokio::spawn(stream_audio_to_livekit(
        audio_rx,
        livekit_source,
        Arc::clone(&echo_processor),
        args.sample_rate,
    ));
    let reference_task = tokio::spawn(process_reference_audio(
        reference_audio_rx,
        Arc::clone(&echo_processor),
        args.sample_rate,
    ));

    let (audio_playback, remote_audio_task) = if let (Some(output_device), Some(output_config)) =
        (output_device, output_config)
    {
        let output_name = output_device
            .name()
            .unwrap_or_else(|_| "Unknown output".to_string());
        let output_supported_config = output_device.default_output_config()?;
        println!(
                "Selected audio output: name='{output_name}', default={}Hz/{}ch/{:?}, stream={}Hz/{}ch/{:?}, volume={:.2}",
                output_supported_config.sample_rate().0,
                output_supported_config.channels(),
                output_supported_config.sample_format(),
                output_config.sample_rate.0,
                output_config.channels,
                output_supported_config.sample_format(),
                args.volume
            );
        let mixer = AudioMixer::with_reference_audio(
            args.sample_rate,
            DEFAULT_CHANNELS as u32,
            args.volume,
            room_db_tx,
            reference_audio_tx,
            speech_level,
        );
        let remote_audio_task = tokio::spawn(handle_remote_audio_streams(
            Arc::clone(&room),
            mixer.clone(),
            args.sample_rate,
        ));
        let playback = AudioPlayback::new(
            output_device,
            output_config,
            output_supported_config.sample_format(),
            mixer,
        )?;
        (Some(playback), Some(remote_audio_task))
    } else {
        warn!("Audio playback disabled; AEC will not receive speaker reference audio");
        (None, None)
    };

    Ok(AudioRuntime {
        room,
        streaming_task,
        reference_task,
        db_meter_task,
        remote_audio_task,
        _audio_capture: audio_capture,
        _audio_playback: audio_playback,
    })
}

fn resolve_livekit_token(args: &Args) -> Result<String> {
    if let Some(token) = args
        .token
        .clone()
        .or_else(|| env::var("LIVEKIT_TOKEN").ok())
    {
        return Ok(token);
    }

    let api_key = args
        .api_key
        .clone()
        .or_else(|| env::var("LIVEKIT_API_KEY").ok())
        .ok_or_else(|| {
            anyhow!(
                "provide --token/LIVEKIT_TOKEN, or provide --api-key/LIVEKIT_API_KEY and --api-secret/LIVEKIT_API_SECRET"
            )
        })?;
    let api_secret = args
        .api_secret
        .clone()
        .or_else(|| env::var("LIVEKIT_API_SECRET").ok())
        .ok_or_else(|| {
            anyhow!(
                "provide --token/LIVEKIT_TOKEN, or provide --api-key/LIVEKIT_API_KEY and --api-secret/LIVEKIT_API_SECRET"
            )
        })?;

    access_token::AccessToken::with_api_key(&api_key, &api_secret)
        .with_identity(&args.identity)
        .with_name(&args.identity)
        .with_grants(access_token::VideoGrants {
            room_join: true,
            room: args.room_name.clone(),
            ..Default::default()
        })
        .to_jwt()
        .map_err(Into::into)
}

async fn stream_audio_to_livekit(
    mut audio_rx: mpsc::UnboundedReceiver<Vec<i16>>,
    livekit_source: NativeAudioSource,
    echo_processor: Arc<tokio::sync::Mutex<EchoCancellationProcessor>>,
    sample_rate: u32,
) -> Result<()> {
    let mut buffer = Vec::new();
    let samples_per_10ms = (sample_rate / 100) as usize;
    let mut frame_count = 0_u64;
    let mut last_meter_at = Instant::now();
    info!("Starting LiveKit microphone stream: {sample_rate} Hz, mono");

    while let Some(audio_data) = audio_rx.recv().await {
        buffer.extend_from_slice(&audio_data);
        while buffer.len() >= samples_per_10ms {
            let mut chunk: Vec<i16> = buffer.drain(..samples_per_10ms).collect();
            frame_count += 1;
            if last_meter_at.elapsed() >= Duration::from_millis(500) {
                let level = calculate_speech_level(&chunk);
                info!(
                    "Mic VU frame #{frame_count}: [{}] {:.3}",
                    level_bar(level),
                    level
                );
                last_meter_at = Instant::now();
            }
            {
                let mut processor = echo_processor.lock().await;
                processor.process_microphone_audio(&mut chunk);
            }

            let audio_frame = AudioFrame {
                data: chunk.into(),
                sample_rate,
                num_channels: DEFAULT_CHANNELS as u32,
                samples_per_channel: samples_per_10ms as u32,
            };
            if let Err(error) = livekit_source.capture_frame(&audio_frame).await {
                error!("Failed to send LiveKit audio frame: {error}");
            }
        }
    }

    Ok(())
}

async fn process_reference_audio(
    mut reference_rx: mpsc::UnboundedReceiver<Vec<i16>>,
    echo_processor: Arc<tokio::sync::Mutex<EchoCancellationProcessor>>,
    sample_rate: u32,
) -> Result<()> {
    let mut buffer = Vec::new();
    let samples_per_10ms = (sample_rate / 100) as usize;
    info!("Starting APM reference stream: {sample_rate} Hz, mono");

    while let Some(audio_data) = reference_rx.recv().await {
        buffer.extend_from_slice(&audio_data);
        while buffer.len() >= samples_per_10ms {
            let mut chunk: Vec<i16> = buffer.drain(..samples_per_10ms).collect();
            let mut processor = echo_processor.lock().await;
            processor.process_reference_audio(&mut chunk);
        }
    }

    Ok(())
}

async fn handle_remote_audio_streams(
    room: Arc<Room>,
    mixer: AudioMixer,
    sample_rate: u32,
) -> Result<()> {
    let mut room_events = room.subscribe();
    info!("Listening for remote LiveKit audio tracks");

    while let Some(event) = room_events.recv().await {
        match event {
            RoomEvent::ParticipantConnected(participant) => {
                info!("Participant connected: {}", participant.identity());
            }
            RoomEvent::ParticipantDisconnected(participant) => {
                info!("Participant disconnected: {}", participant.identity());
            }
            RoomEvent::TrackSubscribed {
                track, participant, ..
            } => {
                info!(
                    "Track subscribed from {}: {} ({:?})",
                    participant.identity(),
                    track.name(),
                    track.kind()
                );

                if let livekit::track::RemoteTrack::Audio(audio_track) = track {
                    let participant_identity = participant.identity().to_string();
                    let mut audio_stream =
                        NativeAudioStream::new(audio_track.rtc_track(), sample_rate as i32, 1);
                    let mixer = mixer.clone();

                    tokio::spawn(async move {
                        let mut frame_count = 0_u64;
                        while let Some(audio_frame) = audio_stream.next().await {
                            mixer.add_audio_data(audio_frame.data.as_ref());
                            frame_count += 1;
                            if frame_count == 1 || frame_count % 100 == 0 {
                                let level = calculate_speech_level(audio_frame.data.as_ref());
                                info!(
                                    "Remote audio from {}: frame #{}, {} samples, {}Hz, {}ch, level={:.3}",
                                    participant_identity,
                                    frame_count,
                                    audio_frame.data.len(),
                                    audio_frame.sample_rate,
                                    audio_frame.num_channels,
                                    level
                                );
                            }
                            debug!(
                                "Received {} samples from {}",
                                audio_frame.data.len(),
                                participant_identity
                            );
                        }
                        info!("Remote audio stream ended for {participant_identity}");
                    });
                }
            }
            RoomEvent::TrackUnsubscribed {
                track, participant, ..
            } => {
                info!(
                    "Track unsubscribed from {}: {} ({:?})",
                    participant.identity(),
                    track.name(),
                    track.kind()
                );
            }
            other => {
                debug!("Room event: {other:?}");
            }
        }
    }

    Ok(())
}

fn list_audio_devices() -> Result<()> {
    let host = cpal::default_host();
    print_audio_device_inventory(&host);
    Ok(())
}

fn print_audio_device_inventory(host: &cpal::Host) {
    println!("Available audio input devices:");
    match host.input_devices() {
        Ok(devices) => {
            for (index, device) in devices.enumerate() {
                print_device_default_config(index, "input", &device);
            }
        }
        Err(error) => println!("   failed to enumerate input devices: {error}"),
    }

    println!("\nAvailable audio output devices:");
    match host.output_devices() {
        Ok(devices) => {
            for (index, device) in devices.enumerate() {
                print_device_default_config(index, "output", &device);
            }
        }
        Err(error) => println!("   failed to enumerate output devices: {error}"),
    }
    println!();
}

fn print_device_default_config(index: usize, direction: &str, device: &Device) {
    let name = device.name().unwrap_or_else(|_| "Unknown".to_string());
    println!("{}. {}", index + 1, name);

    let config = match direction {
        "input" => device.default_input_config(),
        "output" => device.default_output_config(),
        _ => return,
    };

    match config {
        Ok(config) => {
            println!(
                "   default {} config: {}Hz {}ch {:?}",
                direction,
                config.sample_rate().0,
                config.channels(),
                config.sample_format()
            );
        }
        Err(error) => println!("   no default {direction} config: {error}"),
    }
}

fn select_input_device(host: &cpal::Host, requested: Option<&str>) -> Result<Device> {
    if let Some(name) = requested {
        println!("Selecting requested audio input device containing '{name}'");
        return find_input_device_by_name(host, name);
    }

    match find_input_device_by_name(host, DEFAULT_WM8960_NAME) {
        Ok(device) => {
            println!("Auto-selected WM8960 audio input device");
            Ok(device)
        }
        Err(error) => {
            println!("WM8960 audio input not found ({error}); using system default input");
            host.default_input_device()
                .ok_or_else(|| anyhow!("no input device found"))
        }
    }
}

fn select_output_device(host: &cpal::Host, requested: Option<&str>) -> Result<Device> {
    if let Some(name) = requested {
        println!("Selecting requested audio output device containing '{name}'");
        return find_output_device_by_name(host, name);
    }

    match find_output_device_by_name(host, DEFAULT_WM8960_NAME) {
        Ok(device) => {
            println!("Auto-selected WM8960 audio output device");
            Ok(device)
        }
        Err(error) => {
            println!("WM8960 audio output not found ({error}); using system default output");
            host.default_output_device()
                .ok_or_else(|| anyhow!("no output device found"))
        }
    }
}

fn find_input_device_by_name(host: &cpal::Host, name: &str) -> Result<Device> {
    for device in host.input_devices()? {
        if device
            .name()
            .map(|device_name| device_name.contains(name))
            .unwrap_or(false)
        {
            return Ok(device);
        }
    }
    Err(anyhow!("input device '{name}' not found"))
}

fn find_output_device_by_name(host: &cpal::Host, name: &str) -> Result<Device> {
    for device in host.output_devices()? {
        if device
            .name()
            .map(|device_name| device_name.contains(name))
            .unwrap_or(false)
        {
            return Ok(device);
        }
    }
    Err(anyhow!("output device '{name}' not found"))
}

fn calculate_speech_level(samples: &[i16]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }

    let average = samples
        .iter()
        .map(|sample| sample.unsigned_abs() as f32)
        .sum::<f32>()
        / samples.len() as f32;
    (average / i16::MAX as f32 * 4.0).clamp(0.0, 1.0)
}

fn level_bar(level: f32) -> String {
    const WIDTH: usize = 24;
    let filled = (level.clamp(0.0, 1.0) * WIDTH as f32).round() as usize;
    let mut bar = String::with_capacity(WIDTH);
    for index in 0..WIDTH {
        bar.push(if index < filled { '#' } else { '-' });
    }
    bar
}

fn convert_sample_to_i16<T: SizedSample>(sample: T) -> i16 {
    if std::mem::size_of::<T>() == std::mem::size_of::<f32>() {
        let sample_f32 = unsafe { std::mem::transmute_copy::<T, f32>(&sample) };
        (sample_f32.clamp(-1.0, 1.0) * i16::MAX as f32) as i16
    } else if std::mem::size_of::<T>() == std::mem::size_of::<i16>() {
        unsafe { std::mem::transmute_copy::<T, i16>(&sample) }
    } else if std::mem::size_of::<T>() == std::mem::size_of::<u16>() {
        let sample_u16 = unsafe { std::mem::transmute_copy::<T, u16>(&sample) };
        ((sample_u16 as i32) - (u16::MAX as i32 / 2)) as i16
    } else {
        0
    }
}

fn convert_i16_to_sample<T: SizedSample + Sample + FromSample<f32>>(sample: i16) -> T {
    if std::mem::size_of::<T>() == std::mem::size_of::<f32>() {
        let sample_f32 = sample as f32 / i16::MAX as f32;
        unsafe { std::mem::transmute_copy::<f32, T>(&sample_f32) }
    } else if std::mem::size_of::<T>() == std::mem::size_of::<i16>() {
        unsafe { std::mem::transmute_copy::<i16, T>(&sample) }
    } else if std::mem::size_of::<T>() == std::mem::size_of::<u16>() {
        let sample_u16 = ((sample as i32) + (u16::MAX as i32 / 2)) as u16;
        unsafe { std::mem::transmute_copy::<u16, T>(&sample_u16) }
    } else {
        Sample::from_sample(0.0_f32)
    }
}
