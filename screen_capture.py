# Create self contained exe file:
# pyinstaller --noconfirm --onefile --windowed --add-data "C:/gstreamer/1.0/msvc_x86_64;gstreamer" screen_capture.py

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import logging
import os
import shlex
import shutil
import subprocess
import threading
import sys

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Modified for standalone portability
_GST_SEARCH_PATHS = [
    r"C:\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
    r"C:\gstreamer\1.0\x86_64\bin\gst-launch-1.0.exe",
    r"C:\Program Files\GStreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe",
    r"C:\Program Files\GStreamer\1.0\x86_64\bin\gst-launch-1.0.exe",
]

def _find_gst_launch() -> str | None:
    # 1. Check if running inside a PyInstaller standalone bundle
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        # We will bundle the GStreamer binaries into a folder named "gstreamer" inside the exe
        internal_path = os.path.join(sys._MEIPASS, "gstreamer", "bin", "gst-launch-1.0.exe")
        if os.path.isfile(internal_path):
            log.debug("gst-launch-1.0 found inside standalone bundle: %s", internal_path)
            
            # CRITICAL: GStreamer requires its 'bin' directory to be on the PATH to find its DLLs
            gst_bin_dir = os.path.dirname(internal_path)
            if gst_bin_dir not in os.environ["PATH"]:
                os.environ["PATH"] = gst_bin_dir + os.path.pathsep + os.environ["PATH"]
                
            return internal_path

    # 2. Fallback to system-wide installations if not bundled
    exe = shutil.which("gst-launch-1.0") or shutil.which("gst-launch-1.0.exe")
    if exe:
        log.debug("gst-launch-1.0 found on PATH: %s", exe)
        return exe
    for path in _GST_SEARCH_PATHS:
        if os.path.isfile(path):
            log.debug("gst-launch-1.0 found at: %s", path)
            return path
    log.warning("gst-launch-1.0 not found on PATH or in known locations")
    return None


class ScreenCaptureApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("GStreamer Screen Capture")
        self.root.resizable(False, False)
        self.process: subprocess.Popen | None = None
        self.is_capturing = False
        self._stopping = False
        self._closing = False
        self.mode_var = tk.StringVar(value="video+audio")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI --

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)

        ttk.Label(main, text="Output directory:").grid(row=0, column=0, sticky="w", **pad)
        dir_frame = ttk.Frame(main)
        dir_frame.grid(row=0, column=1, columnspan=2, sticky="ew", **pad)
        dir_frame.columnconfigure(0, weight=1)
        self.dir_var = tk.StringVar(value=r"c:\Recording")
        self.dir_var.trace_add("write", lambda *_: self._refresh_command())
        ttk.Entry(dir_frame, textvariable=self.dir_var, width=42).grid(row=0, column=0, sticky="ew")
        ttk.Button(dir_frame, text="Browse…", command=self._browse_dir).grid(row=0, column=1, padx=(6, 0))

        ttk.Label(main, text="File name:").grid(row=1, column=0, sticky="w", **pad)
        self.filename_var = tk.StringVar(value="capture")
        self.filename_var.trace_add("write", lambda *_: self._refresh_command())
        ttk.Entry(main, textvariable=self.filename_var, width=32).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(main, text="Record:").grid(row=2, column=0, sticky="w", **pad)
        mode_frame = ttk.Frame(main)
        mode_frame.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        for label, value in (("Video + Audio", "video+audio"),
                             ("Video only", "video"),
                             ("Audio only", "audio")):
            ttk.Radiobutton(mode_frame, text=label, variable=self.mode_var,
                            value=value, command=self._refresh_command).pack(side="left", padx=(0, 12))

        ttk.Separator(main, orient="horizontal").grid(row=3, column=0, columnspan=3,
                                                      sticky="ew", pady=8)

        ttk.Label(main, text="GStreamer command:").grid(row=4, column=0, columnspan=3,
                                                        sticky="w", padx=8)
        self.cmd_text = tk.Text(main, height=4, width=68, state="disabled",
                                wrap="word", bg="#f4f4f4", relief="sunken",
                                font=("Consolas", 9))
        self.cmd_text.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(2, 8))

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var,
                  foreground="navy", font=("", 9, "italic")).grid(
            row=6, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))

        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=7, column=0, columnspan=3, pady=(0, 4))
        self.start_btn = ttk.Button(btn_frame, text="▶  Start Capture",
                                    command=self._start_capture, width=18)
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(btn_frame, text="■  Stop Capture",
                                   command=self._stop_capture, state="disabled", width=18)
        self.stop_btn.pack(side="left", padx=6)

        self._refresh_command()

    # --------------------------------------------------------- helpers -------

    def _browse_dir(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.dir_var.get() or os.getcwd())
        if directory:
            self.dir_var.set(directory)

    def _output_path(self) -> str:
        directory = self.dir_var.get().strip()
        filename = self.filename_var.get().strip() or "capture"
        if not filename.lower().endswith(".mkv"):
            filename = f"{filename}.mkv"
        return os.path.join(directory, filename)

    def _build_command(self, output_path: str | None = None) -> str:
        loc = (output_path or self._output_path()).replace("\\", "/")
        mode = self.mode_var.get()
        if mode == "video":
            return (
                f'gst-launch-1.0 -e d3d11screencapturesrc ! videoconvert ! mfh265enc ! queue ! h265parse'
                f' ! matroskamux ! filesink location="{loc}"'
            )
        if mode == "audio":
            return (
                f'gst-launch-1.0 -e wasapisrc loopback=true ! audioconvert ! audioresample ! opusenc'
                f' ! matroskamux ! filesink location="{loc}"'
            )
        return (
            f'gst-launch-1.0 -e d3d11screencapturesrc ! videoconvert ! mfh265enc ! queue ! h265parse ! mux.'
            f' wasapisrc loopback=true ! audioconvert ! audioresample ! opusenc ! queue ! mux.'
            f' matroskamux name=mux ! filesink location="{loc}"'
        )

    def _refresh_command(self, *_) -> None:
        cmd = self._build_command()
        self.cmd_text.config(state="normal")
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", cmd)
        self.cmd_text.config(state="disabled")

    # --------------------------------------------------------- capture -------

    def _start_capture(self) -> None:
        output_path = self._output_path()
        directory = os.path.dirname(output_path)

        if not directory:
            messagebox.showerror("Error", "Please select an output directory.")
            return

        if not os.path.isdir(directory):
            try:
                os.makedirs(directory, exist_ok=True)
                log.info("Created output directory: %s", directory)
            except OSError as exc:
                log.error("Cannot create output directory %s: %s", directory, exc)
                messagebox.showerror("Error", f"Cannot create directory:\n{exc}")
                return

        if os.path.exists(output_path):
            if not messagebox.askyesno("Overwrite?",
                                       f"File already exists:\n{output_path}\n\nOverwrite it?"):
                log.debug("User declined to overwrite %s — capture cancelled", output_path)
                return
            log.warning("Overwriting existing file: %s", output_path)

        gst = _find_gst_launch()
        if not gst:
            messagebox.showerror(
                "GStreamer not found",
                "gst-launch-1.0.exe was not found on PATH or in common GStreamer "
                "installation directories.\n\nInstall GStreamer from "
                "https://gstreamer.freedesktop.org/download/ (Complete install).",
            )
            return

        command = self._build_command(output_path)
        log.debug("Launching: %s", command)
        args = shlex.split(command)
        args[0] = gst

        try:
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except OSError as exc:
            log.error("Failed to launch gst-launch-1.0: %s", exc)
            messagebox.showerror("Launch failed", str(exc))
            return

        log.info("Capture started (mode=%s, pid=%d) → %s",
                 self.mode_var.get(), self.process.pid, output_path)
        self._capture_path = output_path
        self._capture_mode = self.mode_var.get()
        self.is_capturing = True
        self._stopping = False
        self._closing = False
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set(f"Capturing → {output_path}")

        threading.Thread(target=self._watch_process, daemon=True).start()

    def _stop_capture(self) -> None:
        self.stop_btn.config(state="disabled")
        self.root.config(cursor="watch")
        self._stopping = True
        self._animate_stopping(0)
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _animate_stopping(self, tick: int) -> None:
        if not self._stopping:
            return
        self.status_var.set(f"Stopping{'.' * (tick % 4)}")
        self.root.after(400, lambda: self._animate_stopping(tick + 1))

    def _do_stop(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        log.info("Killing gst-launch (pid=%d)", proc.pid)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        proc.wait()
        log.info("gst-launch exited (rc=%d)", proc.returncode)

    def _watch_process(self) -> None:
        proc = self.process
        if proc:
            stderr_output = proc.stderr.read() if proc.stderr else b""
            proc.wait()
            if proc.returncode not in (0, 1, -2) and stderr_output:
                error_msg = stderr_output.decode(errors="replace").strip()
                log.error("gst-launch stderr: %s", error_msg)
                self.root.after(0, lambda: self._show_gst_error(error_msg))
        # Switch UI to "Remuxing..." before blocking on the remux pipeline
        self.root.after(0, self._on_capture_process_done)
        self._do_remux()
        self.root.after(0, self._on_stopped)

    def _on_capture_process_done(self) -> None:
        self._stopping = False  # stops the Stopping... animation
        self.root.config(cursor="watch")
        self.status_var.set("Remuxing...")

    def _build_remux_command(self, loc_src: str, loc_dst: str) -> str:
        mode = getattr(self, "_capture_mode", "video+audio")
        if mode == "video+audio":
            # matroskademux exposes one pad per track; link each to a separate
            # queue then into matroskamux so both video and audio are preserved.
            return (
                f'gst-launch-1.0 -e filesrc location="{loc_src}" ! matroskademux name=demux'
                f' demux.video_0 ! h265parse ! queue ! mux.'
                f' demux.audio_0 ! opusparse ! queue ! mux.'
                f' matroskamux name=mux ! filesink location="{loc_dst}"'
            )
        # Single-track file (video-only or audio-only)
        return (
            f'gst-launch-1.0 filesrc location="{loc_src}" ! matroskademux'
            f' ! queue ! matroskamux ! filesink location="{loc_dst}"'
        )

    def _do_remux(self) -> None:
        src = getattr(self, "_capture_path", None)
        if not src or not os.path.isfile(src):
            log.warning("Remux skipped: source file not found (%s)", src)
            return

        gst = _find_gst_launch()
        if not gst:
            log.warning("Remux skipped: gst-launch-1.0 not found")
            return

        dst = src + ".remux"
        loc_src = src.replace("\\", "/")
        loc_dst = dst.replace("\\", "/")
        remux_cmd = self._build_remux_command(loc_src, loc_dst)
        log.info("Remuxing: %s → %s", src, dst)
        log.debug("Remux command: %s", remux_cmd)

        args = shlex.split(remux_cmd)
        args[0] = gst
        try:
            result = subprocess.run(args, capture_output=True, timeout=300)
            if result.returncode == 0:
                os.replace(dst, src)
                log.info("Remux complete — file replaced: %s", src)
            else:
                stderr = result.stderr.decode(errors="replace").strip()
                log.error("Remux failed (rc=%d): %s", result.returncode, stderr)
                if os.path.isfile(dst):
                    os.remove(dst)
        except subprocess.TimeoutExpired:
            log.error("Remux timed out after 300s")
            if os.path.isfile(dst):
                os.remove(dst)
        except OSError as exc:
            log.error("Remux error: %s", exc)
            if os.path.isfile(dst):
                os.remove(dst)

    def _on_stopped(self) -> None:
        self._stopping = False
        self.is_capturing = False
        self.process = None
        self.root.config(cursor="")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("Stopped — ready.")
        if self._closing:
            self.root.destroy()

    def _show_gst_error(self, msg: str) -> None:
        messagebox.showerror(
            "GStreamer error",
            f"The pipeline reported an error:\n\n{msg[:800]}"
            "\n\nMake sure GStreamer is installed and d3d11screencapturesrc is available.",
        )

    def _on_close(self) -> None:
        if self.is_capturing:
            if messagebox.askyesno("Quit", "Capture is still running. Stop and quit?"):
                log.info("Window closed during capture — stopping before exit")
                self._closing = True
                self._stop_capture()
        else:
            self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ScreenCaptureApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
