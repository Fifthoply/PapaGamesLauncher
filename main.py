import os
import sys
import json
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QVBoxLayout, QCheckBox,
    QPushButton, QLabel, QDialogButtonBox
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtCore import QUrl, QFile, QObject, Slot, Signal

# -------------------------------------------------------------------
# Persistent storage handler (~/.papadata.json)
# -------------------------------------------------------------------
class LocalStorageHandler(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._file = Path.home() / ".papadata.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self):
        with self._lock:
            try:
                with open(self._file, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
            except Exception as e:
                print(f"[ERROR] Could not save storage: {e}")

    @Slot(str, str, str)
    def setItem(self, game, key, value):
        self._data.setdefault(game, {})[key] = value
        self._save()

    @Slot(str, str)
    def removeItem(self, game, key):
        if game in self._data and key in self._data[game]:
            del self._data[game][key]
            if not self._data[game]:
                del self._data[game]
            self._save()

    @Slot(str)
    def clear(self, game):
        if game in self._data:
            del self._data[game]
            self._save()

    def get_game_data(self, game_key):
        """Return the current data dict for a game (synchronous)."""
        return self._data.get(game_key, {})

    def get_setting(self, key, default=None):
        return self._data.get("_settings", {}).get(key, default)

    def set_setting(self, key, value):
        self._data.setdefault("_settings", {})[key] = value
        self._save()

# -------------------------------------------------------------------
# Combined injection script
# -------------------------------------------------------------------
def _build_injection_script(game_key, game_data):
    # Load qwebchannel.js
    qwebchannel_js = QFile(":/qtwebchannel/qwebchannel.js")
    if not qwebchannel_js.open(QFile.ReadOnly):
        print("[ERROR] Could not read qwebchannel.js")
        return ""
    channel_lib = qwebchannel_js.readAll().data().decode("utf-8")
    qwebchannel_js.close()

    # Directly embed game data as a JavaScript object literal
    initial_data_js = json.dumps(game_data)

    override_code = f"""
(function() {{
    var gameKey = "{game_key}";
    var _initialData = {initial_data_js};

    // ----- The synchronous local storage handler -----
    var handler = {{
        _data: _initialData,
        getItem: function(key) {{
            return Object.prototype.hasOwnProperty.call(this._data, key)
                ? this._data[key] : null;
        }},
        setItem: function(key, value) {{
            var strVal = String(value);
            this._data[key] = strVal;
            if (window.__storageBackend) {{
                window.__storageBackend.setItem(gameKey, key, strVal);
            }} else {{
                __pending('setItem', key, strVal);
            }}
        }},
        removeItem: function(key) {{
            delete this._data[key];
            if (window.__storageBackend) {{
                window.__storageBackend.removeItem(gameKey, key);
            }} else {{
                __pending('removeItem', key);
            }}
        }},
        clear: function() {{
            this._data = {{}};
            if (window.__storageBackend) {{
                window.__storageBackend.clear(gameKey);
            }} else {{
                __pending('clear');
            }}
        }},
        key: function(index) {{
            return Object.keys(this._data)[index] || null;
        }},
        get length() {{
            return Object.keys(this._data).length;
        }}
    }};

    Object.defineProperty(window, 'localStorage', {{
        get: function() {{ return handler; }},
        configurable: false
    }});

    var __pendingOps = [];
    function __pending(method, key, value) {{
        __pendingOps.push({{ method: method, key: key, value: value }});
    }}

    new QWebChannel(qt.webChannelTransport, function(channel) {{
        window.__storageBackend = channel.objects.storage;
        __pendingOps.forEach(function(op) {{
            if (op.method === 'setItem') {{
                window.__storageBackend.setItem(gameKey, op.key, op.value);
            }} else if (op.method === 'removeItem') {{
                window.__storageBackend.removeItem(gameKey, op.key);
            }} else if (op.method === 'clear') {{
                window.__storageBackend.clear(gameKey);
            }}
        }});
        __pendingOps = [];
    }});
}})();
"""
    return channel_lib + "\n" + override_code

# -------------------------------------------------------------------
# Audio reminder dialog (unchanged)
# -------------------------------------------------------------------
class AudioReminderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Recordatorio")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        self.label = QLabel(
            "Presiona en la pantalla del juego para activar el audio\n"
            "cuando aparezca cada vez que abras un juego nuevo."
        )
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.checkbox = QCheckBox("No mostrar de nuevo.")
        layout.addWidget(self.checkbox)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        self.ok_button = button_box.button(QDialogButtonBox.Ok)
        self.ok_button.setText("Entendido.")
        self.ok_button.clicked.connect(self.accept)
        layout.addWidget(button_box)

    def dont_show_again(self):
        return self.checkbox.isChecked()

# -------------------------------------------------------------------
# Game window
# -------------------------------------------------------------------
class GameWindow(QMainWindow):
    def __init__(self, game_key, title, html_path, launcher_window, storage_handler):
        super().__init__()
        self.game_key = game_key
        self.title = title
        self.html_path = html_path
        self.launcher_window = launcher_window

        self.setWindowTitle(title)
        self.resize(800, 600)

        profile = QWebEngineProfile(self)
        self.page = QWebEnginePage(profile, self)
        self.webview = QWebEngineView()
        self.webview.setPage(self.page)
        self.setCentralWidget(self.webview)

        # QWebChannel – only need storage object for async writes
        self.channel = QWebChannel()
        self.channel.registerObject("storage", storage_handler)
        self.page.setWebChannel(self.channel)

        # Build the injection script with current game data
        game_data = storage_handler.get_game_data(game_key)
        injection = _build_injection_script(game_key, game_data)

        script = QWebEngineScript()
        script.setName("injection")
        script.setSourceCode(injection)
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        self.page.scripts().insert(script)

        # Load the original game file
        self.webview.setUrl(QUrl.fromLocalFile(html_path))

        self.closeEvent = self._close_event

    def _close_event(self, event):
        print(f"[LOG] Game '{self.title}' closed. Returning to launcher.")
        self.webview.page().setAudioMuted(True)
        self.hide()
        self.launcher_window.show()
        event.ignore()

# -------------------------------------------------------------------
# Launcher backend
# -------------------------------------------------------------------
class LauncherBackend(QObject):
    launch_signal = Signal(str)

    @Slot(str)
    def launch_game(self, game_key):
        print(f"[LOG] Launching: {game_key}")
        self.launch_signal.emit(game_key)

# -------------------------------------------------------------------
# Launcher window
# -------------------------------------------------------------------
class LauncherWindow(QMainWindow):
    def __init__(self, html_path, game_windows, storage_handler):
        super().__init__()
        self.setWindowTitle("Papa Games Offline")
        self.resize(480, 360)
        self.game_windows = game_windows
        self.storage = storage_handler

        profile = QWebEngineProfile(self)
        page = QWebEnginePage(profile, self)
        self.webview = QWebEngineView()
        self.webview.setPage(page)
        self.setCentralWidget(self.webview)

        self.channel = QWebChannel()
        self.backend = LauncherBackend()
        self.backend.launch_signal.connect(self.on_launch_game)
        self.channel.registerObject("backend", self.backend)
        self.webview.page().setWebChannel(self.channel)

        self._load_html(html_path)

    def _load_html(self, html_path):
        qwebchannel_js = QFile(":/qtwebchannel/qwebchannel.js")
        if not qwebchannel_js.open(QFile.ReadOnly):
            print("[ERROR] Could not read qwebchannel.js")
            return
        channel_script = qwebchannel_js.readAll().data().decode("utf-8")
        qwebchannel_js.close()

        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        html = html.replace("<head>", f"<head><script>{channel_script}</script>")
        self.webview.setHtml(html, QUrl.fromLocalFile(html_path))

    @Slot(str)
    def on_launch_game(self, game_key):
        if game_key not in self.game_windows:
            print(f"[ERROR] Unknown game key: {game_key}")
            return

        if not self.storage.get_setting("dont_show_audio_alert", False):
            dlg = AudioReminderDialog(self)
            dlg.exec()
            if dlg.dont_show_again():
                self.storage.set_setting("dont_show_audio_alert", True)

        self.hide()
        self.game_windows[game_key].show()

    def closeEvent(self, event):
        print("[LOG] Launcher closed. Exiting.")
        for win in self.game_windows.values():
            win.close()
        event.accept()
        QApplication.quit()

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    games_dir = os.path.join(base_dir, "games")
    launcher_html = os.path.join(base_dir, "launcher.html")

    game_mapping = {
        "cucharas_ultra": {
            "name": "Atrapa las Cucharas Ultra Dificil",
            "file": "Atrapa las Cucharas Ultra Dificil TURBOWARP.html"
        },
        "cucharas": {
            "name": "Atrapa las Cucharas!",
            "file": "Atrapa las Cucharas! TURBOWARP.html"
        },
        "cocodriliano": {
            "name": "Evita a Cocodriliano!",
            "file": "Evita a Cocodriliano! TURBOWARP.html"
        },
        "space_fighting": {
            "name": "Papa's Space Fighting (solo PC)",
            "file": "Papa's Space Fighting TURBOWARP.html"
        },
        "papa_running": {
            "name": "Papa Running (solo PC)",
            "file": "papa running oficial (DEFINITIVE) TURBOWARP.html"
        }
    }

    storage = LocalStorageHandler()

    game_windows = {}
    for key, info in game_mapping.items():
        game_file = os.path.join(games_dir, info["file"])
        if not os.path.exists(game_file):
            print(f"[ERROR] Game file not found: {game_file}")
            continue
        win = GameWindow(key, info["name"], game_file, None, storage)
        win.hide()
        game_windows[key] = win

    launcher = LauncherWindow(launcher_html, game_windows, storage)

    for win in game_windows.values():
        win.launcher_window = launcher

    launcher.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
