import asyncio
import edge_tts

async def main():
    text = (
        "Hello, welcome to your technical interview. "
        "Can you explain the difference between "
        "a Python list and a tuple?"
    )

    voice = "en-IN-NeerjaNeural"

    tts = edge_tts.Communicate(text, voice)
    await tts.save("question.mp3")

asyncio.run(main())