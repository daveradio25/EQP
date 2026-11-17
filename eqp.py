"""EQP (EQ Player) - a small Windows audio player that sends audio straight
to your DAC in WASAPI exclusive mode and shows exactly what is being sent.

Usage:
    python eqp.py                     pick the output device from a list
    python eqp.py 13                  use output device number 13
    python eqp.py 13 a.flac b.flac    ...and queue these files

Features: IN/OUT format readout with a bit-perfect indicator, automatic
sample-rate matching, per-DAC bit-depth setting, a live 10-band graphic EQ
with named presets, and a simple playlist.  See README.md.
"""
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import numpy as np
import soundfile as sf
import sounddevice as sd
from math import gcd
from scipy.signal import resample_poly, sosfilt

BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]  # Hz
Q = 1.41          # one-octave-wide bands
RANGE_DB = 12     # each slider goes +/- this much
BUFFER_SECONDS = 0.2   # audio held ready for the DAC; larger = fewer dropouts

# Dark theme colours
BG, PANEL, TROUGH = "#1e1e1e", "#2a2a2a", "#3a3a3a"
FG, DIM, ACCENT, WARN = "#e0e0e0", "#9a9a9a", "#4fc3f7", "#ff6b6b"

# Presets and per-DAC settings live in a small text file next to this script
HERE = os.path.dirname(os.path.abspath(__file__))
PRESET_FILE = os.path.join(HERE, "eqp_presets.json")
OLD_PRESET_FILE = os.path.join(HERE, "flaceq_presets.json")   # pre-EQP name
# Icons are looked for in an "Images" folder next to this script
IMAGE_DIR = os.path.join(HERE, "Images")
ICON_FILE = os.path.join(IMAGE_DIR, "EQP_icon.ico")
LOGO_FILE = os.path.join(IMAGE_DIR, "EQP_logo.png")
SMALL_LOGO_FILE = os.path.join(IMAGE_DIR, "EQP_logo_small.png")  # shown in window

# Starter presets: rough starting points to adjust by ear, not measurements
DEFAULT_PRESETS = {
    "HiFiMan Edition XS - post-rock": {
        "preamp": -5, "gains": [4, 3, 1, 0, 0, 1, 2.5, 0, -1.5, -2]},
    "Beyerdynamic MMX 100 - post-rock": {
        "preamp": -2, "gains": [2, 0, -1.5, -2, 0, 0, 1, 0, -2.5, 0]},
}


def load_store():
    store = {}
    for path in (PRESET_FILE, OLD_PRESET_FILE):
        try:
            with open(path) as f:
                store = json.load(f)
            break
        except (OSError, ValueError):
            continue
    store.setdefault("presets", dict(DEFAULT_PRESETS))
    store.setdefault("last", None)
    return store


def save_store(store):
    try:
        with open(PRESET_FILE, "w") as f:
            json.dump(store, f, indent=2)
    except OSError as e:
        print("Could not save settings:", e)


def peaking(freq, gain_db, sr):
    """One peaking-EQ biquad section (RBJ Audio EQ Cookbook)."""
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * Q)
    cosw = np.cos(w0)
    b = [1 + alpha * a, -2 * cosw, 1 - alpha * a]
    den = [1 + alpha / a, -2 * cosw, 1 - alpha / a]
    return np.array(b + den) / den[0]


def build_sos(gains_db, freqs, sr):
    return np.array([peaking(f, g, sr) for f, g in zip(freqs, gains_db)])


FILE_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}


def rate_candidates(sr):
    """The file's own rate first, then the nearest rates a lesser DAC may take."""
    rates = [sr]
    for base, other in ((44100, 48000), (48000, 44100)):
        if sr % base == 0:
            r = sr // 2
            while r >= base:
                rates.append(r)
                r //= 2
            rates.append(other)
            break
    else:
        rates += [48000, 44100]
    return list(dict.fromkeys(rates))


def describe(sr, bits):
    return f"{sr / 1000:g} kHz / " + (f"{bits}-bit" if bits else "lossy")


class Player:
    def __init__(self, device, bits=16):
        self.device = device
        self.in_sr = self.in_bits = None
        self.preamp_db = 0.0
        self.exact = False
        self.set_format(bits)
        self.stream = None
        self.data = None
        self.gains = [0.0] * len(BANDS)
        self.preamp = 1.0
        self.pos = 0
        self.paused = False
        self.clipped = False
        self.finished = False
        self.dropouts = 0       # times the DAC ran out of audio
        self.rng = np.random.default_rng()

    def load(self, path):
        """Stop whatever is playing and start this file from the top."""
        self.close()
        in_bits = FILE_BITS.get(sf.info(path).subtype)   # None if not plain PCM
        data, in_sr = sf.read(path, dtype="float32", always_2d=True)
        if data.shape[1] == 1:                      # mono -> both ears
            data = np.repeat(data, 2, axis=1)
        self.ch = data.shape[1]

        # Use the file's own sample rate if the DAC accepts it; otherwise
        # step down to the nearest rate it does accept and convert.
        first_error = None
        for sr in rate_candidates(in_sr):
            self.sr = sr
            try:
                stream = self.open_stream(BUFFER_SECONDS)
                break
            except Exception as e:
                first_error = first_error or e
        else:
            raise first_error
        if sr != in_sr:
            g = gcd(sr, in_sr)
            data = resample_poly(data, sr // g, in_sr // g, axis=0).astype("float32")

        self.data, self.in_sr, self.in_bits = data, in_sr, in_bits
        self.freqs = [f for f in BANDS if f < 0.45 * sr]
        self.sos = build_sos(self.gains, self.freqs, sr)
        self.zi = np.zeros((len(self.freqs), 2, self.ch))
        self.pos = 0
        self.paused = self.finished = False
        self.refresh_exact()
        self.stream = stream
        self.stream.start()

    def set_format(self, bits):
        """Bit depth sent to the DAC. 24-bit travels in a 32-bit container."""
        self.out_bits = bits
        self.dtype = "int16" if bits == 16 else "int32"
        self.scale = 2.0 ** (bits - 1)
        self.shift = 256 if bits == 24 else 1

    def set_bits(self, bits):
        """Change the output bit depth, carrying on from the same position."""
        playing = self.stream is not None
        self.close()
        self.set_format(bits)
        if playing:
            self.refresh_exact()
            self.stream = self.open_stream(BUFFER_SECONDS)
            self.stream.start()

    def refresh_exact(self):
        """True when the DAC gets the file's samples completely untouched."""
        exact = (self.in_bits is not None and self.out_bits >= self.in_bits
                 and self.sr == self.in_sr and self.preamp_db == 0
                 and not any(self.gains))
        if not exact and self.exact:
            self.zi = np.zeros_like(self.zi)        # EQ restarts cleanly
        self.exact = exact

    def changes(self):
        """What, if anything, is being altered between file and DAC."""
        notes = []
        if self.sr != self.in_sr:
            notes.append("resampled")
        if self.in_bits is None:
            notes.append("lossy source")
        elif self.out_bits < self.in_bits:
            notes.append("bit depth reduced")
        if self.preamp_db or any(self.gains):
            notes.append("EQ on")
        return ", ".join(notes)

    def open_stream(self, latency):
        try:
            return sd.OutputStream(
                samplerate=self.sr, device=self.device, channels=self.ch,
                dtype=self.dtype, latency=latency, callback=self.callback,
                dither_off=True,    # the audio library must not add noise
                extra_settings=sd.WasapiSettings(exclusive=True))
        except Exception:
            if latency == "high":
                raise
            return self.open_stream("high")   # driver refused the big buffer

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def set_eq(self, gains_db, preamp_db):
        self.gains = list(gains_db)
        self.preamp_db = preamp_db
        self.preamp = 10 ** (preamp_db / 20)
        if self.stream is not None:
            self.sos = build_sos(self.gains, self.freqs, self.sr)
            self.refresh_exact()

    def callback(self, out, frames, time, status):
        if status:
            self.dropouts += 1
        if self.paused:
            out[:] = 0
            return
        chunk = self.data[self.pos:self.pos + frames]
        self.pos += len(chunk)
        if self.exact:
            # Nothing to change: hand the file's samples over as they are
            y = chunk.astype(np.float64) * self.scale
        else:
            y, self.zi = sosfilt(self.sos, chunk, axis=0, zi=self.zi)
            y *= self.preamp
            if np.abs(y).max(initial=0) > 1.0:
                self.clipped = True
            # TPDF dither before rounding to the output bit depth
            y = y * self.scale + self.rng.random(y.shape) - self.rng.random(y.shape)
        y = np.clip(np.round(y), -self.scale, self.scale - 1)
        out[:len(chunk)] = y * self.shift
        if len(chunk) < frames:
            out[len(chunk):] = 0
            self.finished = True
            raise sd.CallbackStop


def choose_device(store):
    """List the WASAPI outputs and ask which one the DAC is on."""
    apis = sd.query_hostapis()
    outputs = {i: d["name"] for i, d in enumerate(sd.query_devices())
               if d["max_output_channels"] > 0
               and "WASAPI" in apis[d["hostapi"]]["name"]}
    if not outputs:
        print("No Windows WASAPI audio outputs were found.")
        return None
    default = None
    print("\nEQP - EQ Player\n\nAudio outputs:\n")
    for i, name in outputs.items():
        note = ""
        if name == store.get("device"):
            default, note = i, "   <-- used last time"
        print(f"  {i:>3}   {name}{note}")
    print("\nChoose the output your DAC is connected to "
          "(its name usually includes 'USB' or 'DAC').")
    hint = f" [Enter = {default}]" if default is not None else ""
    while True:
        try:
            answer = input(f"Device number{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not answer and default is not None:
            return default
        if answer.isdigit() and int(answer) in outputs:
            return int(answer)
        print("  Please type one of the numbers listed above.")


def favour_audio():
    """Give the audio thread a better chance of meeting its deadlines."""
    sys.setswitchinterval(0.001)    # hand over to the audio thread quickly
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x80)   # "High" priority
    except Exception:
        pass


def own_taskbar_identity():
    """Stop Windows grouping the window under the generic Python icon.
    Must run before the window is created."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "EQP.Player")
    except Exception:
        pass


def set_icons(root):
    """Use the .ico for the title bar and taskbar; fall back to the .png.
    The player still runs normally if neither file is present."""
    try:
        root.iconbitmap(ICON_FILE)
        return
    except tk.TclError:
        pass
    try:
        root.logo = tk.PhotoImage(file=LOGO_FILE)   # keep a reference
        root.iconphoto(True, root.logo)
    except tk.TclError:
        pass


def dark_title_bar(root):
    """Ask Windows 11 for a dark title bar; silently skipped elsewhere."""
    try:
        import ctypes
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass


def main():
    device = next((int(a) for a in sys.argv[1:] if a.isdigit()), None)
    store = load_store()
    if device is None:
        device = choose_device(store)
        if device is None:
            return
    dac = sd.query_devices(device)["name"]
    store["device"] = dac                           # remembered for next time
    dacs = store.setdefault("dacs", {})             # per-DAC settings, by name
    save_store(store)
    favour_audio()
    p = Player(device, dacs.get(dac, {}).get("bits", 16))
    tracks = []                 # full paths, in playlist order
    state = {"current": None, "error": "", "preset": None}

    own_taskbar_identity()
    root = tk.Tk()
    root.title("EQP")
    set_icons(root)
    root.configure(bg=BG)
    ncols = len(BANDS) + 1

    # ---------- EQ sliders ----------
    sliders = []

    def settings():
        return {"preamp": pre.get(), "gains": [s.get() for s in sliders]}

    def update(_=None):
        p.set_eq([s.get() for s in sliders], pre.get())
        name = state["preset"]
        if name not in store["presets"]:
            preset_var.set("Presets")
        elif store["presets"][name] == settings():
            preset_var.set(name)
        else:
            preset_var.set(name + " (edited)")

    def column(col, label):
        s = tk.Scale(root, from_=RANGE_DB, to=-RANGE_DB, resolution=0.5,
                     length=260, command=update, bg=BG, fg=FG,
                     troughcolor=TROUGH, activebackground=ACCENT,
                     highlightthickness=0, bd=0)
        s.grid(row=0, column=col, padx=4, pady=(10, 0))
        tk.Label(root, text=label, bg=BG, fg=DIM).grid(row=1, column=col)
        return s

    pre = column(0, "Preamp\ndB")
    for i, f in enumerate(BANDS):
        sliders.append(column(i + 1, f"{f // 1000}k" if f >= 1000 else str(f)))

    # ---------- EQ presets ----------
    def apply(values, name):
        state["preset"] = name
        pre.set(values["preamp"])
        for s, g in zip(sliders, values["gains"]):
            s.set(g)
        update()

    def rebuild_menu():
        preset_menu.delete(0, "end")
        for name in sorted(store["presets"], key=str.lower):
            preset_menu.add_command(
                label=name, command=lambda n=name: apply(store["presets"][n], n))
        if not store["presets"]:
            preset_menu.add_command(label="(no presets saved)", state="disabled")

    def save_preset():
        name = simpledialog.askstring(
            "Save EQ preset", "Name for these EQ settings:",
            initialvalue=state["preset"] or "", parent=root)
        name = (name or "").strip()
        if not name:
            return
        if name in store["presets"] and not messagebox.askyesno(
                "Save EQ preset", f'Replace the existing "{name}"?', parent=root):
            return
        store["presets"][name] = settings()
        state["preset"] = name
        save_store(store)
        rebuild_menu()
        update()

    def delete_preset():
        name = state["preset"]
        if name not in store["presets"]:
            return
        if messagebox.askyesno("Delete EQ preset", f'Delete "{name}"?',
                               parent=root):
            del store["presets"][name]
            state["preset"] = None
            save_store(store)
            rebuild_menu()
            update()

    # ---------- Playback ----------
    def play(index):
        if not 0 <= index < len(tracks):
            return
        if state["current"] is not None and state["current"] < len(tracks):
            playlist.itemconfig(state["current"], fg=FG)
        state["error"] = ""
        state["current"] = index
        try:
            p.load(tracks[index])
        except Exception as e:
            state["error"] = f"Can't play: {e}"
            playlist.itemconfig(index, fg=WARN)
            return
        playlist.itemconfig(index, fg=ACCENT)
        playlist.see(index)
        pause_btn.config(text="Pause")
        root.title("EQP - " + os.path.basename(tracks[index]))

    def step(delta):
        cur = state["current"]
        play(0 if cur is None else cur + delta)

    def toggle():
        p.paused = not p.paused
        pause_btn.config(text="Play" if p.paused else "Pause")

    def flat():
        apply({"preamp": 0, "gains": [0] * len(sliders)}, None)

    def button(parent, text, command):
        b = tk.Button(parent, text=text, width=10, command=command, bg=PANEL,
                      fg=FG, activebackground=TROUGH, activeforeground=FG,
                      relief="flat", bd=0, pady=4)
        b.pack(side="left", padx=4)
        return b

    # ---------- IN / OUT readout ----------
    def choose_bits(bits):
        old = p.out_bits
        try:
            p.set_bits(bits)
        except Exception as e:
            state["error"] = f"DAC refused {bits}-bit: {e}"
            try:
                p.set_bits(old)
            except Exception:
                pass
            return
        state["error"] = ""
        dacs[dac] = {"bits": bits}
        save_store(store)
        bits_var.set(f"DAC bit depth: {bits}")

    readout = tk.Frame(root, bg=BG)
    readout.grid(row=2, column=0, columnspan=ncols, pady=(12, 0))

    def field(tag, width):
        tk.Label(readout, text=tag, bg=BG, fg=DIM).pack(side="left", padx=(8, 4))
        value = tk.Label(readout, text="-", width=width, anchor="w",
                         bg=PANEL, fg=FG, padx=6, pady=3)
        value.pack(side="left")
        return value

    in_lbl = field("IN", 16)
    out_lbl = field("OUT", 16)
    path_lbl = tk.Label(readout, width=34, anchor="w", bg=BG, fg=DIM)
    path_lbl.pack(side="left", padx=8)
    bits_var = tk.StringVar(value=f"DAC bit depth: {p.out_bits}")
    bits_btn = tk.Menubutton(
        readout, textvariable=bits_var, width=18, bg=PANEL, fg=FG,
        activebackground=TROUGH, activeforeground=FG, relief="flat", pady=4)
    bits_menu = tk.Menu(bits_btn, tearoff=0, bg=PANEL, fg=FG,
                        activebackground=TROUGH, activeforeground=FG)
    for b in (16, 24, 32):
        bits_menu.add_command(label=f"{b}-bit", command=lambda b=b: choose_bits(b))
    bits_btn.config(menu=bits_menu)
    bits_btn.pack(side="left", padx=4)

    presetbar = tk.Frame(root, bg=BG)
    presetbar.grid(row=3, column=0, columnspan=ncols, pady=(10, 0))
    try:
        root.small_logo = tk.PhotoImage(file=SMALL_LOGO_FILE)
        tk.Label(presetbar, image=root.small_logo, bg=BG).pack(
            side="left", padx=(4, 10))
    except tk.TclError:
        pass                    # no logo file: just leave it out
    preset_var = tk.StringVar(value="Presets")
    preset_btn = tk.Menubutton(
        presetbar, textvariable=preset_var, width=30, bg=PANEL, fg=FG,
        activebackground=TROUGH, activeforeground=FG, relief="flat", pady=4)
    preset_menu = tk.Menu(preset_btn, tearoff=0, bg=PANEL, fg=FG,
                          activebackground=TROUGH, activeforeground=FG)
    preset_btn.config(menu=preset_menu)
    preset_btn.pack(side="left", padx=4)
    button(presetbar, "Save as...", save_preset)
    button(presetbar, "Delete", delete_preset)
    button(presetbar, "Flat EQ", flat)

    bar = tk.Frame(root, bg=BG)
    bar.grid(row=4, column=0, columnspan=ncols, pady=10)
    button(bar, "Prev", lambda: step(-1))
    pause_btn = button(bar, "Pause", toggle)
    button(bar, "Next", lambda: step(1))
    status = tk.Label(bar, width=48, anchor="w", bg=BG, fg=FG)
    status.pack(side="left", padx=8)

    # ---------- Playlist (below the EQ) ----------
    def add_paths(paths):
        for path in paths:
            tracks.append(path)
            playlist.insert("end", "  " + os.path.basename(path))
        if state["current"] is None and tracks:
            play(len(tracks) - len(paths))

    def pick_files():
        add_paths(filedialog.askopenfilenames(
            title="Pick files to play",
            filetypes=[("Audio files", "*.flac *.wav *.aiff *.aif *.ogg"),
                       ("All files", "*.*")]))

    def remove_selected():
        for i in reversed(playlist.curselection()):
            playlist.delete(i)
            del tracks[i]
            cur = state["current"]
            if cur is not None and i < cur:
                state["current"] = cur - 1
            elif i == cur:
                p.close()
                state["current"] = None

    listframe = tk.Frame(root, bg=BG)
    listframe.grid(row=5, column=0, columnspan=ncols, sticky="nsew", padx=10)
    scroll = tk.Scrollbar(listframe)
    playlist = tk.Listbox(listframe, height=8, bg=PANEL, fg=FG, bd=0,
                          selectbackground=TROUGH, selectforeground=FG,
                          highlightthickness=0, activestyle="none",
                          selectmode="extended", yscrollcommand=scroll.set)
    scroll.config(command=playlist.yview)
    scroll.pack(side="right", fill="y")
    playlist.pack(side="left", fill="both", expand=True)
    playlist.bind("<Double-Button-1>",
                  lambda e: play(playlist.nearest(e.y)) if tracks else None)
    root.grid_rowconfigure(5, weight=1)

    filebar = tk.Frame(root, bg=BG)
    filebar.grid(row=6, column=0, columnspan=ncols, pady=10)
    button(filebar, "Pick files...", pick_files)
    button(filebar, "Remove", remove_selected)
    tk.Label(filebar, text="Double-click a track to play it",
             bg=BG, fg=DIM).pack(side="left", padx=8)

    # ---------- Status line / auto-advance ----------
    def tick():
        if p.finished:
            p.finished = False
            step(1)
        colour = FG
        if state["error"]:
            text, colour = state["error"], WARN
        elif p.stream is None:
            text = "Pick files to play"
        else:
            secs, total = p.pos // p.sr, len(p.data) // p.sr
            text = f"{secs // 60}:{secs % 60:02d} / {total // 60}:{total % 60:02d}"
            if p.dropouts:
                text += f"   dropouts: {p.dropouts}"
            if p.clipped:
                text, colour = text + "   CLIPPING - lower preamp", WARN
                p.clipped = False
        status.config(text=text, fg=colour)
        if p.stream is None:
            in_lbl.config(text="-")
            out_lbl.config(text="-")
            path_lbl.config(text="")
        else:
            in_lbl.config(text=describe(p.in_sr, p.in_bits))
            out_lbl.config(text=describe(p.sr, p.out_bits))
            if p.exact and p.in_bits <= 16:
                path_lbl.config(text="BIT-PERFECT", fg=ACCENT)
            elif p.exact:
                # Relies on the bit-depth setting, which can't be verified
                path_lbl.config(
                    text=f"BIT-PERFECT if DAC is truly {p.in_bits}-bit", fg=ACCENT)
            else:
                path_lbl.config(text=p.changes(), fg=DIM)
        root.after(500, tick)

    def on_close():
        # Remember the sliders as they are now for the next launch
        store["last"] = dict(settings(), name=state["preset"])
        save_store(store)
        p.close()
        root.destroy()

    rebuild_menu()
    if store["last"]:
        apply(store["last"], store["last"].get("name"))
    root.protocol("WM_DELETE_WINDOW", on_close)
    dark_title_bar(root)
    add_paths([a for a in sys.argv[1:] if not a.isdigit()])
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
