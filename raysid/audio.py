"""
Audio module for RaySID client.

Provides Geiger counter tick sound generation based on CPS readings.
"""

import queue
import threading
import time
from typing import Optional

from .logging_config import get_audio_logger

logger = get_audio_logger()

# Try to import audio dependencies. sounddevice raises OSError (not
# ImportError) when the system PortAudio library is missing — common on
# a fresh Linux box without libportaudio2 — so catch that too and just
# disable audio instead of crashing the whole app.
try:
    import numpy as np
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except (ImportError, OSError):
    AUDIO_AVAILABLE = False
    np = None
    sd = None


class TickSoundGenerator:
    """Generate Geiger counter tick sounds based on CPS.
    
    Uses a continuous audio stream with tick queue for proper timing on macOS.
    
    Clicks scale (from mobile app settings):
    - 1:1   = 1 tick per 1 count (every detection)
    - 1:5   = 1 tick per 5 counts  
    - 1:10  = 1 tick per 10 counts
    - 1:20  = 1 tick per 20 counts (default)
    - 1:50  = 1 tick per 50 counts
    - 1:100 = 1 tick per 100 counts
    - 1:250 = 1 tick per 250 counts
    """
    
    def __init__(self, sample_rate: int = 44100, tick_scale: int = 20):
        self.sample_rate = sample_rate
        self.enabled = AUDIO_AVAILABLE
        self.tick_scale = tick_scale
        
        # Default: Classic Chernobyl-era Geiger sound (DP-5/SRP-68 style)
        self.tick_style = "3"
        
        # State
        self._current_cps = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_accumulator = 0.0
        self._tick_queue: queue.Queue = queue.Queue()
        self._stream = None
        
        # Pre-generate tick sound
        self._tick_wave = None
        if self.enabled:
            self._generate_tick()
    
    def set_tick_style(self, style: str = "3") -> None:
        """Set tick sound style.
        
        Styles:
        - "1": Sharp high click (modern digital)
        - "2": Medium click
        - "3": Classic Chernobyl DP-5 style (DEFAULT) - sharp crackling pop
        - "4": Deeper click
        - "5": Low thump
        """
        self.tick_style = style
        self._generate_tick()
        logger.debug(f"Tick style set to {style}")
    
    def _generate_tick(self) -> None:
        """Generate authentic Geiger counter tick waveform.
        
        Classic Soviet Geiger counters (DP-5, SRP-68-01) produce a sharp
        crackling "pop" sound - a brief electrical discharge through a speaker.
        """
        if not AUDIO_AVAILABLE:
            return
        
        style = self.tick_style
        
        if style == "1":
            # Sharp high click (modern)
            duration = 0.003
            samples = int(self.sample_rate * duration)
            tick = np.random.uniform(-1, 1, samples).astype(np.float32)
            env = np.exp(-np.linspace(0, 8, samples))
            tick = tick * env * 0.7
            
        elif style == "2":
            # Medium click
            duration = 0.006
            samples = int(self.sample_rate * duration)
            tick = np.random.uniform(-1, 1, samples).astype(np.float32)
            env = np.exp(-np.linspace(0, 6, samples))
            tick = tick * env * 0.6
            
        elif style == "3":
            # Classic Chernobyl DP-5 style - THE AUTHENTIC SOUND
            duration = 0.008
            samples = int(self.sample_rate * duration)
            
            # Initial sharp transient (the "pop")
            tick = np.zeros(samples, dtype=np.float32)
            tick[0] = 0.9
            tick[1] = -0.7
            tick[2] = 0.5
            tick[3] = -0.3
            
            # Add crackling noise decay
            noise = np.random.uniform(-1, 1, samples).astype(np.float32)
            env = np.exp(-np.linspace(0, 12, samples))
            tick = tick + noise * env * 0.4
            tick = np.clip(tick, -1, 1) * 0.8
            
        elif style == "4":
            # Deeper click
            duration = 0.012
            samples = int(self.sample_rate * duration)
            t = np.linspace(0, duration, samples)
            pulse = np.sin(2 * np.pi * 200 * t) * np.exp(-t * 300)
            noise = np.random.uniform(-0.3, 0.3, samples)
            tick = (pulse + noise * np.exp(-t * 500)).astype(np.float32) * 0.6
            
        elif style == "5":
            # Low thump
            duration = 0.015
            samples = int(self.sample_rate * duration)
            t = np.linspace(0, duration, samples)
            tick = (np.sin(2 * np.pi * 120 * t) * np.exp(-t * 200)).astype(np.float32) * 0.7
            
        else:
            # Fallback to style 3
            self.tick_style = "3"
            self._generate_tick()
            return
        
        self._tick_wave = tick.astype(np.float32)
    
    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        """Audio stream callback."""
        outdata.fill(0)
        
        # Check for pending ticks
        ticks_in_buffer = 0
        max_ticks = 3  # Max ticks per buffer
        
        while not self._tick_queue.empty() and ticks_in_buffer < max_ticks:
            try:
                self._tick_queue.get_nowait()
                # Place tick in buffer
                if self._tick_wave is not None:
                    tick_len = len(self._tick_wave)
                    pos = (ticks_in_buffer * frames) // (max_ticks + 1)
                    end = min(pos + tick_len, frames)
                    copy_len = end - pos
                    if copy_len > 0:
                        outdata[pos:end, 0] = self._tick_wave[:copy_len]
                ticks_in_buffer += 1
            except queue.Empty:
                break
    
    def _tick_thread(self) -> None:
        """Thread to calculate and queue ticks based on CPS."""
        last_time = time.time()
        
        while self._running:
            try:
                now = time.time()
                dt = min(now - last_time, 0.1)
                last_time = now
                
                with self._lock:
                    cps = self._current_cps
                
                if cps > 0:
                    expected = (cps * dt) / self.tick_scale
                    self._tick_accumulator += expected
                    
                    num_ticks = int(self._tick_accumulator)
                    if num_ticks > 0:
                        self._tick_accumulator -= num_ticks
                        # Queue ticks (limit to prevent backup)
                        for _ in range(min(num_ticks, 10)):
                            if self._tick_queue.qsize() < 20:
                                self._tick_queue.put(1)
                
                time.sleep(0.01)  # 100Hz tick calculation
            except Exception as e:
                logger.debug(f"Tick thread error: {e}")
                time.sleep(0.05)
    
    def start(self) -> None:
        """Start audio."""
        if not self.enabled or self._running:
            return
        
        try:
            self._running = True
            
            # Start audio stream
            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype=np.float32,
                callback=self._audio_callback,
                blocksize=1024,  # ~23ms at 44100Hz
                latency='low'
            )
            self._stream.start()
            
            # Start tick calculation thread
            self._thread = threading.Thread(target=self._tick_thread, daemon=True)
            self._thread.start()
            
            logger.debug("Audio stream started")
            
        except Exception as e:
            logger.error(f"Audio error: {e}")
            self._running = False
    
    def stop(self) -> None:
        """Stop audio."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        logger.debug("Audio stream stopped")
    
    def update(self, cps: float) -> None:
        """Update CPS value."""
        if not self.enabled:
            return
        
        if not self._running:
            self.start()
        
        with self._lock:
            self._current_cps = cps


def is_audio_available() -> bool:
    """Check if audio dependencies are available."""
    return AUDIO_AVAILABLE

