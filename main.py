import os
import re
import sys
import json
import asyncio
import threading
import time
import ctypes
import subprocess
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import win32event
import win32api
import winerror
import websockets
import win32com.client
import requests
import miniaudio

# ==========================================
# Configuration
# ==========================================
CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "ws_uri": "",
    "max_history_lines": 50,
    "tts_backend": "system",   # system, openai
    "tts_rate": 1.5,
    "tts_volume": 100,
    "openai": {
        "api_key": "",
        "model": "tts-1",
        "voice": "alloy",
        "url": "https://api.openai.com/v1/audio/speech"
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                if "openai" not in config:
                    config["openai"] = DEFAULT_CONFIG["openai"]
                return config
        except Exception as e:
            print(f"Failed to read config: {e}, using defaults")
            return DEFAULT_CONFIG.copy()
    else:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)
        return DEFAULT_CONFIG.copy()

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Failed to save config: {e}")
        return False

def restart_program():
    python = sys.executable
    subprocess.Popen([python] + sys.argv)
    sys.exit(0)

# ==========================================
# Prevent duplicate instances
# ==========================================
MUTEX_NAME = 'OwncastChatTTS_Mutex'
mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
    sys.exit(0)

# ==========================================
# TTS Audio Player (supports interruption)
# ==========================================

class AudioPlayer:
    def __init__(self):
        self.device = miniaudio.PlaybackDevice()
        self._stop_flag = False
        self._play_thread = None
        self._lock = threading.Lock()

    def play(self, mp3_data: bytes):
        with self._lock:
            self._stop()
            self._stop_flag = False
            self._play_thread = threading.Thread(
                target=self._play, args=(mp3_data,), daemon=True
            )
            self._play_thread.start()

    def _play(self, mp3_data: bytes):
        try:
            stream = miniaudio.stream_memory(mp3_data)
            self.device.start(stream)

            while not self._stop_flag:
                time.sleep(0.1)

        except Exception as e:
            print(f"Playback error: {e}")
        finally:
            self.device.stop()

    def _stop(self):
        self._stop_flag = True

        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=0.5)
        self.device.stop()

    def close(self):
        self._stop()

class SystemTTSEngine:
    def __init__(self, rate=1, volume=100):
        self.rate = rate
        self.volume = volume
        self._current_voice = None
        self._lock = threading.Lock()

    def speak(self, text, voice_name=None):
        with self._lock:
            self.stop()
            self._current_voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._current_voice.Rate = self.rate
            self._current_voice.Volume = self.volume
            if voice_name:
                for v in self._current_voice.GetVoices():
                    if v.GetDescription() == voice_name:
                        self._current_voice.Voice = v
                        break
            self._current_voice.Speak(text, 1)

    def stop(self):
        if self._current_voice:
            try:
                self._current_voice.Skip("SENTENCE", 10000)
                self._current_voice = None
            except:
                pass

    def get_voices(self):
        voices = []
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            for v in speaker.GetVoices():
                voices.append(v.GetDescription())
            speaker = None
        except:
            pass
        return voices

class OpenAITTSEngine:
    def __init__(self, api_key, model, default_voice, url):
        self.api_key = api_key
        self.model = model
        self.default_voice = default_voice
        self.url = url
        self.player = AudioPlayer()
        self._seq = 0
        self._seq_lock = threading.Lock()

    def speak(self, text, voice=None, rate=1.5, error_callback=None):
        voice = voice or self.default_voice
        with self._seq_lock:
            self._seq += 1
            current_seq = self._seq

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
            "speed": float(rate)
        }

        def do_request():
            try:
                resp = requests.post(self.url, json=payload, headers=headers, timeout=10)
                if resp.status_code == 200:
                    with self._seq_lock:
                        if self._seq == current_seq:
                            self.player.play(resp.content)
                else:
                    error_msg = f"TTS error ({resp.status_code}): {resp.text[:100]}"
                    print(error_msg)
                    if error_callback:
                        error_callback(error_msg)
            except Exception as e:
                error_msg = f"OpenAI request error: {str(e)}"
                print(error_msg)
                if error_callback:
                    error_callback(error_msg)

        threading.Thread(target=do_request, daemon=True).start()

    def stop(self):
        self.player._stop()

    def close(self):
        self.player.close()

class TTSManager:
    def __init__(self, config, error_callback=None):
        self.config = config
        self.error_callback = error_callback
        self.system_engine = SystemTTSEngine(config["tts_rate"], config["tts_volume"])
        self.openai_engine = None
        self._init_engines()
        self.current_backend = config["tts_backend"]

    def _init_engines(self):
        oa = self.config["openai"]
        if oa.get("api_key"):
            self.openai_engine = OpenAITTSEngine(oa["api_key"], oa["model"], oa["voice"], oa["url"])

    def speak(self, text, backend=None):
        if backend:
            self.current_backend = backend
        if self.current_backend == "openai" and self.openai_engine:
            self.system_engine.stop()
            voice = self.config["openai"]["voice"]
            tts_rate = self.config["tts_rate"]
            self.openai_engine.speak(text, voice, tts_rate, error_callback=self.error_callback)
        else:
            self.system_engine.stop()
            if self.openai_engine:
                self.openai_engine.stop()
            self.system_engine.speak(text)

    def update_config(self, new_config):
        self.config = new_config
        self.system_engine.rate = new_config["tts_rate"]
        self.system_engine.volume = new_config["tts_volume"]
        self.current_backend = new_config["tts_backend"]
        self._init_engines()

    def close(self):
        if self.openai_engine:
            self.openai_engine.close()
        self.system_engine.stop()

# ==========================================
# Settings Window
# ==========================================
class SettingsWindow:
    def __init__(self, parent, config, on_config_changed, tts_manager):
        self.parent = parent
        self.config = config
        self.on_config_changed = on_config_changed
        self.tts_manager = tts_manager
        self.window = tk.Toplevel(parent)
        self.window.title("Settings")
        self.window.geometry("600x560")
        self.window.transient(parent)
        self.window.grab_set()
        self._center_window()

        self.ws_uri_var = tk.StringVar(value=config["ws_uri"])
        self.max_history_var = tk.IntVar(value=config["max_history_lines"])
        self.tts_backend_var = tk.StringVar(value=config["tts_backend"])
        self.tts_rate_var = tk.StringVar(value=str(config["tts_rate"]))
        self.tts_volume_var = tk.IntVar(value=config["tts_volume"])

        self.oa_api_key_var = tk.StringVar(value=config["openai"]["api_key"])
        self.oa_model_var = tk.StringVar(value=config["openai"]["model"])
        self.oa_voice_var = tk.StringVar(value=config["openai"]["voice"])
        self.oa_url_var = tk.StringVar(value=config["openai"]["url"])

        self._create_widgets()

    @staticmethod
    def _add_row(parent, label_text, widget, row, label_sticky="w", label_padx=5, widget_padx=10, pady=5):
        ttk.Label(parent, text=label_text, anchor=tk.W).grid(
            row=row, column=0, sticky=label_sticky, padx=label_padx, pady=pady
        )
        widget.grid(row=row, column=1, sticky="ew", padx=widget_padx, pady=pady)
        parent.columnconfigure(1, weight=1)

    def _create_scale_row(self, parent, row: int, text: str, var: tk.StringVar,
                          from_: float, to: float, step: float):
        precision = len(str(step).split('.')[1]) if '.' in str(step) else 0

        def format_value(val):
            val = float(val)
            return f"{val:.{precision}f}" if precision else str(int(val))

        entry_var = tk.StringVar(value=format_value(var.get()))

        def snap(val):
            return round(val / step) * step

        def on_scale(val):
            val = snap(float(val))
            val = max(from_, min(to, val))
            if precision == 0:
                val = int(val)
            var.set(format_value(val))
            entry_var.set(format_value(val))

        def on_entry(*args):
            try:
                val = float(entry_var.get())
                val = snap(val)
                val = max(from_, min(to, val))
                if precision == 0:
                    val = int(val)
                var.set(val)
                scale.set(val)
                entry_var.set(format_value(val))
            except ValueError:
                pass

        scale = ttk.Scale(parent, from_=from_, to=to, orient=tk.HORIZONTAL, command=on_scale)
        scale.set(var.get())
        entry = ttk.Entry(parent, textvariable=entry_var, width=6)

        ttk.Label(parent, text=text, anchor=tk.W).grid(row=row, column=0, sticky="w", padx=5, pady=6)
        scale.grid(row=row, column=1, sticky="ew", padx=5, pady=6)
        entry.grid(row=row, column=2, padx=5, pady=6)

        entry_var.trace_add("write", on_entry)
        parent.columnconfigure(1, weight=1)

    def _center_window(self):
        self.window.update_idletasks()
        w, h = self.window.winfo_width(), self.window.winfo_height()
        sw, sh = self.window.winfo_screenwidth(), self.window.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.window.geometry(f"+{x}+{y}")

    def _create_widgets(self):
        row = 0
        self._add_row(self.window, "Owncast WS URL:",
                      ttk.Entry(self.window, textvariable=self.ws_uri_var, width=60), row)
        row += 1

        self._add_row(self.window, "TTS backend:",
                      ttk.Combobox(self.window, textvariable=self.tts_backend_var,
                                   values=["system", "openai"]), row)
        row += 1

        sys_frame = ttk.LabelFrame(self.window, text="System", padding=5)
        sys_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=10, padx=10)
        sys_frame.columnconfigure(1, weight=1)
        self._create_scale_row(sys_frame, 0, "Speech rate:", self.tts_rate_var, -5, 5, 0.1)
        self._create_scale_row(sys_frame, 1, "Volume:", self.tts_volume_var, 0, 100, 1)
        row += 1

        oa_frame = ttk.LabelFrame(self.window, text="OpenAI", padding=5)
        oa_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=10, padx=10)
        oa_frame.columnconfigure(1, weight=1)
        self._add_row(oa_frame, "API Key:", ttk.Entry(oa_frame, textvariable=self.oa_api_key_var, width=50), 0)
        self._add_row(oa_frame, "Model:", ttk.Entry(oa_frame, textvariable=self.oa_model_var), 1)
        oa_voice_combo = ttk.Combobox(oa_frame, textvariable=self.oa_voice_var,
                                      values=["alloy", "echo", "fable", "onyx", "nova", "shimmer"])
        self._add_row(oa_frame, "Default voice:", oa_voice_combo, 2)
        self._add_row(oa_frame, "API URL:", ttk.Entry(oa_frame, textvariable=self.oa_url_var, width=50), 3)
        row += 1

        btn_frame = ttk.Frame(self.window)
        btn_frame.grid(row=row, column=0, columnspan=2)
        ttk.Button(btn_frame, text="Test TTS", command=self._test_tts).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="Cancel", command=self.window.destroy).pack(side=tk.LEFT)

    def _test_tts(self):
        if self.tts_manager:
            self.tts_manager.speak("test123", self.tts_backend_var.get())
        else:
            messagebox.showerror("Error", "TTS manager not available")

    def _save(self):
        new_config = {
            "ws_uri": self.ws_uri_var.get().strip(),
            "max_history_lines": self.max_history_var.get(),
            "tts_backend": self.tts_backend_var.get(),
            "tts_rate": self.tts_rate_var.get(),
            "tts_volume": self.tts_volume_var.get(),
            "openai": {
                "api_key": self.oa_api_key_var.get(),
                "model": self.oa_model_var.get(),
                "voice": self.oa_voice_var.get(),
                "url": self.oa_url_var.get()
            }
        }
        if not new_config["ws_uri"]:
            messagebox.showerror("Error", "WebSocket URL cannot be empty")
            return
        if save_config(new_config):
            self.config.update(new_config)
            self.on_config_changed(new_config)
            self.window.destroy()
        else:
            messagebox.showerror("Error", "Failed to save configuration")

# ==========================================
# Main Window
# ==========================================
class OwncastChatTTS:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.root.title("Owncast Chat TTS")
        self.root.geometry("650x600")
        self.root.minsize(500, 400)
        self._center_window()

        if getattr(sys, 'frozen', False):
            application_path = sys._MEIPASS
        else:
            application_path = os.path.dirname(__file__)
        icon_path = os.path.join(application_path, 'owncast.ico')
        root.iconbitmap(icon_path)

        self.root.grid_rowconfigure(0, weight=0)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_rowconfigure(2, weight=0)
        self.root.grid_columnconfigure(0, weight=1)

        btn_frame = ttk.Frame(self.root)
        btn_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10,5))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)
        btn_frame.grid_columnconfigure(2, weight=1)
        btn_frame.grid_columnconfigure(3, weight=1)

        self.pause_btn = ttk.Button(btn_frame, text="Pause", width=10)
        self.pause_btn.grid(row=0, column=0, padx=2)
        ttk.Button(btn_frame, text="Clear", command=self._clear_history, width=10).grid(row=0, column=1, padx=2)
        ttk.Button(btn_frame, text="Settings", command=self._open_settings, width=10).grid(row=0, column=2, padx=2)
        ttk.Button(btn_frame, text="Exit", command=self._on_close, width=10).grid(row=0, column=3, padx=2)

        self.text_frame = ttk.LabelFrame(self.root, text="History", padding=5)
        self.text_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.text_frame.grid_rowconfigure(0, weight=1)
        self.text_frame.grid_columnconfigure(0, weight=1)
        self.history_text = scrolledtext.ScrolledText(self.text_frame, wrap=tk.WORD, font=("Microsoft YaHei UI", 9), state=tk.DISABLED)
        self.history_text.grid(row=0, column=0, sticky="nsew")

        self.status_var = tk.StringVar(value="Initializing...")
        self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=2)
        self.status_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(0,10))

        self.is_paused = False
        self.is_running = True
        self.ws_websocket = None
        self.ws_task = None
        self.loop = None

        self.tts_manager = TTSManager(config, error_callback=self._on_tts_error)

        self._start_ws_thread()
        self.pause_btn.config(command=self._toggle_pause)

    def _center_window(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"+{x}+{y}")

    def _start_ws_thread(self):
        self.ws_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.ws_thread.start()

    def _open_settings(self):
        SettingsWindow(self.root, self.config, self._on_config_updated, self.tts_manager)

    def _on_config_updated(self, new_config):
        old_uri = self.config.get("ws_uri", "")
        new_uri = new_config["ws_uri"]
        self.config.update(new_config)
        self.tts_manager.update_config(new_config)
        if old_uri != new_uri:
            self._reconnect_ws()
        if self.config["openai"]["api_key"] == "":
            self.config["tts_backend"] = "system"
        self._update_status(f"Connected to Owncast | TTS backend: {self.config['tts_backend']}", is_error=False)

    def _reconnect_ws(self):
        if self.loop and self.ws_task:
            asyncio.run_coroutine_threadsafe(self._cancel_ws(), self.loop)

    async def _cancel_ws(self):
        if self.ws_task:
            self.ws_task.cancel()
        if self.ws_websocket:
            await self.ws_websocket.close()

    async def _connect_ws(self):
        ws_uri = self.config["ws_uri"]
        while self.is_running:
            try:
                if ws_uri == "":
                    self._update_status("Owncast WS URL is empty", is_error=True)
                    ws_uri = self.config["ws_uri"]
                    await asyncio.sleep(1)
                    continue
                self._update_status("Connecting to Owncast...", is_error=False)
                async with websockets.connect(ws_uri, ping_interval=20, ping_timeout=10) as ws:
                    self.ws_websocket = ws
                    self._update_status(f"Connected | TTS backend: {self.config['tts_backend']}", is_error=False)
                    async for message in ws:
                        try:
                            data = json.loads(message)
                            msg_type = data.get('type')
                            if msg_type == 'CHAT':
                                display_name = data['user']['displayName']
                                raw_body = data['body']
                                clean_body = re.sub('<.*?>', '', raw_body).strip()
                                display_msg = f"{display_name}: {clean_body}"
                                if clean_body:
                                    self._add_message(display_msg, tts_text=clean_body)
                            elif msg_type == 'USER_JOINED':
                                display_msg = f"[NOTICE] {data['user']['displayName']} joined"
                                self._add_message(display_msg, tts_text=None)
                            elif msg_type == 'NAME_CHANGE':
                                display_msg = f"[NOTICE] {data['oldName']} changed name to {data['user']['displayName']}"
                                self._add_message(display_msg, tts_text=None)
                        except json.JSONDecodeError:
                            continue
            except websockets.ConnectionClosed:
                if self.is_running:
                    self._update_status("Connection closed, reconnecting...", is_error=True)
                    self._add_message("[ERROR] Connection closed, retrying in 3 seconds...", tts_text=None)
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.is_running:
                    self._update_status(f"Connection error: {str(e)[:30]}...", is_error=True)
                    self._add_message(f"[ERROR] Connection error, retrying in 3 seconds...", tts_text=None)
                    await asyncio.sleep(3)

    def _run_async_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ws_task = self.loop.create_task(self._connect_ws())
        self.loop.run_until_complete(self.ws_task)

    def _add_message(self, msg, tts_text=None):
        def update_ui():
            self.history_text.config(state=tk.NORMAL)
            self.history_text.insert(tk.END, msg + '\n')
            self.history_text.see(tk.END)
            lines = int(self.history_text.index('end-1c').split('.')[0])
            max_lines = self.config["max_history_lines"]
            if lines > max_lines:
                self.history_text.delete('1.0', f'{lines - max_lines}.0')
            self.history_text.config(state=tk.DISABLED)

        self.root.after(0, update_ui)

        if not self.is_paused and tts_text and tts_text.strip():
            threading.Thread(target=self.tts_manager.speak, args=(tts_text.strip(),), daemon=True).start()

    def _clear_history(self):
        self.history_text.config(state=tk.NORMAL)
        self.history_text.delete('1.0', tk.END)
        self.history_text.config(state=tk.DISABLED)

    def _toggle_pause(self):
        self.is_paused = not self.is_paused
        self.pause_btn.config(text="Resume" if self.is_paused else "Pause")
        if self.is_paused:
            self.tts_manager.system_engine.stop()
            if self.tts_manager.openai_engine:
                self.tts_manager.openai_engine.stop()
            self._update_status("TTS paused", is_error=False)
        else:
            self._update_status(f"Connected to Owncast | TTS backend: {self.config['tts_backend']}", is_error=False)

    def _update_status(self, text, is_error=False):
        def _update():
            self.status_var.set(text)
            self.status_bar.config(foreground="red" if is_error else "black")
        self.root.after(0, _update)

    def _on_tts_error(self, error_msg):
        self._add_message(f"[ERROR] {error_msg}", tts_text=None)
        self._update_status(error_msg, is_error=True)

        def restore_status():
            if not self.is_paused:
                self._update_status(f"Connected to Owncast | TTS backend: {self.config['tts_backend']}", is_error=False)
        self.root.after(5000, restore_status)

    def _on_close(self):
        self.is_running = False
        if self.loop and self.ws_task:
            asyncio.run_coroutine_threadsafe(self._cancel_ws(), self.loop)
        self.tts_manager.close()
        self.root.destroy()

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except:
            pass
    config = load_config()
    root = tk.Tk()
    app = OwncastChatTTS(root, config)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
