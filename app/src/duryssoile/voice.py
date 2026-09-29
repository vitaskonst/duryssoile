"""Re-encode clips as OGG/Opus voice notes.

Telegram draws a voice message's waveform only for OGG/Opus, so the API
offers every clip in that format as well (GET /api/v1.0/audio/{id}
?format=opus). Converted with mpg123 (MP3 -> WAV) and opusenc (WAV -> Opus),
which is why the admin page accepts only MP3, WAV and OGG/Opus clips; a clip
that already is OGG/Opus is used as it is.
"""

import asyncio
import os
import tempfile


class UnsupportedClip(Exception):
    """The clip is in a format this module cannot convert."""


class ConversionFailed(Exception):
    """A converter failed on a clip it should have handled."""


def is_ogg_opus(clip: bytes) -> bool:
    # The first Ogg page of an Opus stream carries the OpusHead header.
    return clip[:4] == b'OggS' and b'OpusHead' in clip[:128]


def is_wav(clip: bytes) -> bool:
    return clip[:4] == b'RIFF' and clip[8:12] == b'WAVE'


async def run(*command: str) -> None:
    process = await asyncio.create_subprocess_exec(*command, stderr=asyncio.subprocess.PIPE)
    _, errors = await process.communicate()
    if process.returncode:
        raise ConversionFailed(f'{command[0]}: {errors.decode(errors="replace").strip()[-300:]}')


async def to_opus(clip: bytes) -> bytes:
    """The clip as mono OGG/Opus at a voice bitrate.

    Raises UnsupportedClip for anything but MP3, WAV or OGG/Opus.
    """
    if is_ogg_opus(clip):
        return clip
    if clip[:4] == b'OggS':
        raise UnsupportedClip('OGG without Opus (e.g. Vorbis)')

    with tempfile.TemporaryDirectory() as tmp:
        wav_path, ogg_path = os.path.join(tmp, 'in.wav'), os.path.join(tmp, 'out.ogg')
        if is_wav(clip):
            with open(wav_path, 'wb') as f:
                f.write(clip)
        else:
            mp3_path = os.path.join(tmp, 'in.mp3')
            with open(mp3_path, 'wb') as f:
                f.write(clip)
            await run('mpg123', '--quiet', '-w', wav_path, mp3_path)
            # mpg123 exits 0 on input it cannot decode, just without output.
            if not os.path.exists(wav_path) or os.path.getsize(wav_path) <= 44:
                raise UnsupportedClip('not MP3, WAV or OGG/Opus')

        await run('opusenc', '--quiet', '--downmix-mono', '--bitrate', '48', wav_path, ogg_path)
        with open(ogg_path, 'rb') as f:
            return f.read()
