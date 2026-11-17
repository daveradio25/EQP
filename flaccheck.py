"""flaccheck - report an audio file's real format and look for signs that it
was made from a lossy source (for example an MP3 re-saved as FLAC).

Usage:
    python flaccheck.py song.flac [more files ...]
"""
import sys
import numpy as np
import soundfile as sf


def check(path):
    info = sf.info(path)
    print(f"\n{path}")
    print(f"  Format:  {info.samplerate} Hz, {info.subtype}, {info.channels} ch")

    # Average spectrum: how high does the music actually go?
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono, n = data.mean(axis=1), 8192
    if len(mono) < n:
        print("  Too short to analyse.")
        return
    segs = mono[: len(mono) // n * n].reshape(-1, n) * np.hanning(n)
    spec = 20 * np.log10(np.abs(np.fft.rfft(segs, axis=1)).mean(axis=0) + 1e-12)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    top = freqs[spec > spec.max() - 80].max()
    print(f"  Content: up to ~{top / 1000:.1f} kHz "
          f"(the format's limit is {sr / 2000:.1f} kHz)")
    if top < 0.85 * sr / 2:
        print("  Note:    early cutoff - possibly made from a lossy source.\n"
              "           (A hint, not proof: some recordings simply have "
              "little treble.)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    else:
        for p in sys.argv[1:]:
            try:
                check(p)
            except Exception as e:
                print(f"\n{p}\n  Could not read: {e}")
