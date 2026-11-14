"""Transcribe every recording in a folder (offline batch)."""
import sys
from pathlib import Path

from faster_whisper import WhisperModel

model = WhisperModel("large-v3", device="cuda", compute_type="float16")

for path in sorted(Path(sys.argv[1]).glob("*.wav")):
    segments, info = model.transcribe(str(path), beam_size=1)
    Path(path.with_suffix(".txt")).write_text(" ".join(s.text for s in segments))
