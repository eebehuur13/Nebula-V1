"""Template handler. Store outputs with nebula.storage and return links via the result."""

from __future__ import annotations

from nebula import storage


async def run(person, args: dict) -> dict:
    audio = b""  # call the provider here (key from settings / Secrets Manager)
    key = f"outputs/speak/{person.id}/example.mp3"
    await storage.put(key, audio, "audio/mpeg")
    return {"text": "Audio ready.", "files": [{"key": key, "filename": "voice.mp3", "mime": "audio/mpeg"}]}
