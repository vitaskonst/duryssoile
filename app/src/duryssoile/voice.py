"""Re-encode clips as OGG/Opus voice notes.

Telegram draws a voice message's waveform only for OGG/Opus, and inline
voice results must link to that format, so the API offers every clip in it
as well (GET /api/v1.0/audio/{id}?format=opus). ffmpeg rather than a narrower
decoder: the admin page accepts mp3, wav, ogg, opus, m4a and aac.
"""

import asyncio


class ConversionFailed(Exception):
    pass


async def to_opus(clip: bytes) -> bytes:
    """Mono OGG/Opus at a voice bitrate, from a clip in any supported format."""
    process = await asyncio.create_subprocess_exec(
        'ffmpeg', '-nostdin', '-loglevel', 'error',
        '-i', 'pipe:0',
        # Audio only (drops e.g. an MP3's cover art), without metadata.
        '-vn', '-map_metadata', '-1',
        '-ac', '1', '-c:a', 'libopus', '-b:a', '48k',
        '-f', 'ogg', 'pipe:1',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, errors = await process.communicate(clip)
    if process.returncode or not output:
        raise ConversionFailed(errors.decode(errors='replace').strip()[-500:])
    return output
