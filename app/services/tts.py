
import edge_tts


VOICE = "en-IN-NeerjaNeural"


async def synthesize_speech(text):
    if not text or not text.strip():
        raise ValueError("Text is required.")

    communicate = edge_tts.Communicate(
        text=text.strip(),
        voice=VOICE,
        rate="-5%"
    )

    audio_chunks = []

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])

    audio = b"".join(audio_chunks)

    if not audio:
        raise RuntimeError("No audio was generated.")

    return audio
