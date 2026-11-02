import time
from faster_whisper import WhisperModel

audio_path = "/Users/sahilteltumde/Music/demoaudio.m4a"

start = time.perf_counter()

model = WhisperModel(
    "base",
    device="cpu",
    compute_type="int8"
)

model_load_time = time.perf_counter() - start

start = time.perf_counter()

segments, info = model.transcribe(
    audio_path,
    language="en",
    vad_filter=True,
    beam_size=1
)

transcript = " ".join(
    segment.text.strip()
    for segment in segments
)

transcription_time = time.perf_counter() - start

print("Candidate Answer:", transcript)
print(f"Model Loading: {model_load_time:.2f} sec")
print(f"Transcription: {transcription_time:.2f} sec")