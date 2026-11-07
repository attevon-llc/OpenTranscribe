"""The one audio-stream choice every decode path shares (issue #1263).

Rule: analyse the FIRST audio stream of the file, in container order. Three reasons:

* PyAV, which faster-whisper's ``decode_audio`` uses for transcription, decodes
  ``container.decode(audio=0)``, so the first stream is what ASR has always read.
* FFmpeg, given no ``-map``, selects the audio stream with the MOST CHANNELS (ffmpeg docs,
  "Stream selection"); in practice it also prefers a stream flagged default (measured on
  ffmpeg 6.1). On a multi-track file that can be a different track than ASR read, so speaker
  embeddings were computed on audio that was never transcribed.
* ``-map 0:a:0`` is the documented explicit selector for "first audio stream of input 0"
  (ffmpeg docs, ``-map`` option). A trailing ``?`` makes a missing stream non-fatal so a
  file with no audio still fails with the existing "does not contain any stream" message
  that callers classify as a no-audio-track error.

Every ffmpeg call that decodes audio must splice in :func:`audio_map_args` (or pass
:data:`AUDIO_MAP_SPEC` as ffmpeg-python's ``map=``); a structural test enforces it.
"""

AUDIO_STREAM_INDEX = 0
AUDIO_MAP_SPEC = f"0:a:{AUDIO_STREAM_INDEX}?"


def audio_map_args() -> list[str]:
    """Return the ffmpeg argv fragment selecting the shared audio stream."""
    return ["-map", AUDIO_MAP_SPEC]
