"""
converter_core.py
-----------------
Linux-compatible port of _build_cmds() and _get_duration() from converter.py.
Uses system `ffmpeg` binary (no bundled .exe).
"""

import re
import subprocess
from pathlib import Path

FFMPEG = "ffmpeg"

_SCALE = (
    "scale=848:480:force_original_aspect_ratio=decrease,"
    "pad=848:480:(ow-iw)/2:(oh-ih)/2,setsar=1"
)


def get_duration(filepath: str) -> float:
    """Return video duration in seconds, or 0.0 on failure."""
    r = subprocess.run(
        [FFMPEG, "-i", filepath],
        capture_output=True, text=True, timeout=30,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr)
    if m:
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)
    return 0.0


def build_cmds(src: str, dst: str, fmt_label: str,
               countdown_path: "str | None",
               target_mb: "float | None",
               total_dur: float,
               passlog: str,
               trim: "tuple[float, float] | None" = None) -> list[list[str]]:
    """
    Return a list of FFmpeg command lists (1 or 2 passes).

    Parameters
    ----------
    src           : absolute path to source video
    dst           : absolute path for output file
    fmt_label     : one of 'AVI', 'MOV', 'MP4'
    countdown_path: path to countdown clip, or None
    target_mb     : size cap in MB, or None
    total_dur     : duration in seconds used for bitrate math
                    (input clip duration + countdown duration)
    passlog       : prefix path for 2-pass log files
    trim          : (start_sec, end_sec) or None for full clip
    """
    ff = FFMPEG

    if "MOV" in fmt_label:
        audio_bps, default_vbr = 48_000 * 16 * 2, 2500
        audio_args = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
        video_base = ["-c:v", "libx264", "-profile:v", "high"]
        is_mjpeg, two_pass = False, bool(target_mb)
    elif "AVI" in fmt_label:
        audio_bps, default_vbr = 48_000 * 16, 2500
        audio_args = ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "1"]
        video_base = ["-c:v", "mjpeg"]
        is_mjpeg, two_pass = True, False
    else:  # MP4
        audio_bps, default_vbr = 128_000, None
        audio_args = ["-c:a", "aac", "-b:a", "128k"]
        video_base = ["-c:v", "libx264", "-preset", "medium"]
        is_mjpeg, two_pass = False, bool(target_mb)

    if target_mb and total_dur > 0:
        avail = target_mb * 8 * 1024 * 1024 - audio_bps * total_dur
        if avail <= 0:
            raise ValueError(
                f"{target_mb} MB is too small for {total_dur:.0f}s of audio. "
                f"Max clip length: {target_mb*8*1024*1024/audio_bps:.0f}s."
            )
        vbr = min(int(avail / total_dur / 1000),
                  default_vbr if default_vbr else 999_999)
    else:
        vbr = default_vbr

    if is_mjpeg:
        q = max(3, min(25, round(7500 / vbr))) if vbr else 3
        video_args = video_base + ["-q:v", str(q)]
    else:
        video_args = video_base + (["-b:v", f"{vbr}k"] if vbr else ["-crf", "23"])

    null_out = "/dev/null"

    if trim:
        ts, te = trim
        v_trim = f"trim=start={ts:.3f}:end={te:.3f},setpts=PTS-STARTPTS,"
        a_trim = f"atrim=start={ts:.3f}:end={te:.3f},asetpts=PTS-STARTPTS,"
    else:
        v_trim = a_trim = ""

    if countdown_path:
        fc_p1 = (f"[0:v]{v_trim}{_SCALE}[v0];[1:v]{_SCALE}[v1];"
                 f"[v0][v1]concat=n=2:v=1:a=0[v]")
        fc_p2 = (f"[0:v]{v_trim}{_SCALE}[v0];[1:v]{_SCALE}[v1];"
                 f"[0:a]{a_trim}aresample=48000[a0];[1:a]aresample=48000[a1];"
                 f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
        inputs  = ["-i", src, "-i", str(countdown_path)]
        base_p1 = [ff, "-y"] + inputs + ["-filter_complex", fc_p1, "-map", "[v]"]
        base_p2 = [ff, "-y"] + inputs + ["-filter_complex", fc_p2,
                                          "-map", "[v]", "-map", "[a]"]
    else:
        if trim:
            ts, te = trim
            base_p1 = base_p2 = [ff, "-y", "-ss", f"{ts:.3f}",
                                  "-i", src, "-t", f"{te - ts:.3f}", "-vf", _SCALE]
        else:
            base_p1 = base_p2 = [ff, "-y", "-i", src, "-vf", _SCALE]

    if two_pass:
        p1 = (base_p1 + ["-r", "25"] + video_args +
              ["-pass", "1", "-passlogfile", passlog, "-an", "-f", "null", null_out])
        p2 = (base_p2 + ["-r", "25"] + video_args +
              ["-pass", "2", "-passlogfile", passlog] + audio_args + [str(dst)])
        return [p1, p2]

    return [base_p2 + ["-r", "25"] + video_args + audio_args + [str(dst)]]
