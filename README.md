<div align="center">

# ⚡ OmniFlow

**Windows-native, polyglot process automation — visual workflow builder in a single HTML file + Python engine.**

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078d4?logo=windows)](https://microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Version](https://img.shields.io/badge/Version-4.0.0-indigo)](https://github.com/suiisharma/omniflow)

</div>

---

## 🧭 What is OmniFlow?

OmniFlow is a **self-contained Windows desktop automation platform** made of exactly two files:

| File | Role |
|---|---|
| `omniflow_engine.py` | Local Python HTTP server (port `8765`) + full automation runtime |
| `omniflow_studio.html` | Visual drag-and-drop workflow builder — open in any browser |

You build automation workflows visually in the Studio, and the Engine executes them — controlling Windows apps, running scripts, querying databases, scraping web pages, and performing screen OCR, all from a single browser tab.

---

## ✨ Feature Highlights (v4.0)

### 🪟 Bulletproof Windows Native Automation
- **Write text to Notepad / any Win32 app** — auto-launches the app if not open, focuses the window using `AttachThreadInput` (bypasses browser `ForegroundLockTimeout`), then writes via `EM_REPLACESEL` or Clipboard paste
- **Launch `.exe` applications** visibly in the foreground
- **Focus any desktop window** by title or Win32 class name
- **Click screen coordinates or UIA controls** with `mouse_event` Win32 calls
- **Read/write Windows Clipboard** and **Registry** (HKCU/HKLM)
- **Manage Windows Services** (start, stop, restart, query status)
- **Schedule workflows** via `schtasks.exe` Windows Task Scheduler

### 🌐 Polyglot Script Execution (Shared Context)
Run code in multiple languages in a single workflow, with a shared `ctx` object passing data between steps:

| Language | Runtime | Context | Export |
|---|---|---|---|
| Python 3 | `python` / `pythonw` | `ctx['variables']` | `omniflow_set(key, val)` |
| PowerShell | `pwsh` / `powershell` | `$ctx.variables` | `Set-OmniFlowVar key val` |
| Node.js | `node` | `ctx.variables` | `omniflow_set(key, val)` |
| Batch/CMD | `cmd.exe` | env vars | `echo ##OMNIFLOW_SET:k=v##` |
| Bash/sh | `bash` / `sh` | env vars | `omniflow_set key val` |
| C# | `dotnet-script` | injected | — |

### 🌍 Browser & Web Automation
- Open URLs in the system default browser
- Full **Playwright** CDP integration — navigate, click DOM selectors, fill forms, scrape content

### 👁️ Vision & OCR
- **OpenCV template matching** — find a button/image on screen and export its `(x, y)` coordinates
- **Tesseract OCR** — extract text from a screen region or image file

### 🗄️ Database Access
- **SQLite** — query or write any `.db` file, results exported to `ctx`
- **ODBC / SQL Server** — connect via connection string using `pyodbc`

### ⚡ Auto-Start Server Protocol
- Registers a custom `omniflow://` Windows URL protocol (no admin required, uses `HKCU`)
- Registers a Windows Logon startup daemon via `HKCU\...\Run`
- When you open `omniflow_studio.html`, the browser fires `omniflow://start`, which silently launches `pythonw.exe omniflow_engine.py --daemon` — server connects in ~1 second!

### 💾 Persistent Dual Storage
- All workflows, templates, settings, and run history are persisted in **SQLite + JSON** under `~/.omniflow/`
- `localStorage` caching in the browser for offline-capable access

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.8+** installed and in PATH
- Windows 10 or Windows 11

### 1 — Clone & Run

```bash
git clone https://github.com/suiisharma/omniflow.git
cd omniflow
python omniflow_engine.py
```

The engine will:
1. Register the `omniflow://` URL protocol (once)
2. Start the local HTTP server on `http://127.0.0.1:8765`
3. Open `omniflow_studio.html` in your default browser automatically

### 2 — Or open Studio directly

After running the engine once (to register the protocol), you can just **double-click `omniflow_studio.html`** anytime. The browser fires `omniflow://start` automatically and the server wakes up in the background.

---

## 🖥️ Studio Interface

```
┌────────────────────────────────────────────────────────────────────┐
│  OmniFlow Studio v4.0         [Tabs]              [● Server Online] │
├──────────────┬───────────────────────────────┬─────────────────────┤
│  Node Toolbox│     Visual Canvas              │  Step Inspector     │
│              │  ┌──────────┐ ┌──────────┐     │  Title, Category,   │
│ ● Windows    │  │ Step 1   │→│ Step 2   │     │  Action, Params     │
│ ● Polyglot   │  └──────────┘ └──────────┘     │                     │
│ ● Browser    │                                │  [▶ Run This Step]  │
│ ● Vision/OCR │                                │  [★ Save Template]  │
│ ● SQL/DB     │                                │  [🗑 Delete Step]   │
│              ├────────────────────────────────┤                     │
│ ★ My Templates│  ⚡ Execution Logs  │ ctx{}   │                     │
└──────────────┴───────────────────────────────┴─────────────────────┘
```

**4 tabs:**
1. **Visual Workflow Builder** — drag-and-drop canvas with live wiring, step inspector, and execution console
2. **Saved Workflows & Templates** — load, delete, import/export `.json` workflow files
3. **Live Win32 Window Spy** — capture mouse coordinates and enumerate open desktop windows in real-time
4. **Settings & Auto-Start** — configure runtimes (Python, Node, PowerShell paths), database defaults, download the auto-start `.bat`

---

## 🧱 Node Action Reference

### Windows Native (`category: windows`)

| Action | What it does | Key params |
|---|---|---|
| `win_type` | Write text to any app (auto-launches if needed) | `window_title`, `text`, `auto_launch`, `clear_existing` |
| `win_launch` | Open an `.exe` app in foreground | `executable`, `args`, `wait` |
| `win_find_window` | Focus a window by title/class | `title`, `class_name`, `bring_to_front` |
| `win_click` | Click screen coords or UIA control | `x`, `y`, `selector`, `button`, `double` |
| `win_clipboard` | Read or write clipboard text | `operation` (`read`/`write`), `text` |
| `win_registry` | Read/write registry values | `operation`, `key_path`, `value_name`, `value_data` |
| `win_service` | Control Windows services | `service_name`, `operation` (`start`/`stop`/`restart`/`status`) |
| `win_schedule_task` | Create/run/delete scheduled tasks | `task_name`, `schedule_type`, `start_time` |

### Polyglot Scripts (`category: polyglot`)

| Action | Runtime |
|---|---|
| `script_python` | Python 3 |
| `script_powershell` | PowerShell / pwsh |
| `script_javascript` | Node.js |
| `script_batch` | Windows CMD |
| `script_bash` | Bash / sh |

### Browser (`category: browser`)

| Action | What it does |
|---|---|
| `browser_navigate` | Open URL in system browser or Playwright |
| `browser_click` | Click a DOM selector via Playwright |
| `browser_fill` | Fill a form input via Playwright |
| `browser_extract` | Scrape text/attribute from DOM |

### Vision & OCR (`category: vision`)

| Action | What it does |
|---|---|
| `vision_find_image` | OpenCV template match on live screenshot |
| `vision_ocr_extract` | Tesseract OCR on a screen region or image |

### Database (`category: database`)

| Action | What it does |
|---|---|
| `db_query` | Run SQL against SQLite or ODBC (SQL Server) |

---

## 🔗 Shared Context & Variable Interpolation

Steps pass data to each other through a shared `ctx` object. Use `{{variable_name}}` or `{{steps.step_id.output.some_key}}` templates in any param field:

```json
{
  "variables": { "report_line": "Hello World" },
  "nodes": [
    {
      "action": "win_type",
      "params": { "text": "Report: {{variables.report_line}}" }
    }
  ]
}
```

Scripts export variables back to the context with:
- **Python:** `omniflow_set("my_var", value)`
- **PowerShell:** `Set-OmniFlowVar "my_var" $value`
- **Node.js:** `omniflow_set("my_var", value)`
- **Bash:** `omniflow_set my_var value`

---

## 🔌 REST API

The engine exposes a REST API at `http://127.0.0.1:8765`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | `GET` | Server health, platform info, Python version |
| `/api/run_node` | `POST` | Execute a single step |
| `/api/run_workflow` | `POST` | Execute a full workflow definition |
| `/api/workflows` | `GET/POST` | List or save workflows |
| `/api/workflows/<id>` | `DELETE` | Delete a workflow |
| `/api/templates` | `GET/POST` | List or save custom templates |
| `/api/settings` | `GET/POST` | Get or update global settings |
| `/api/history` | `GET` | Fetch run history (last 35 runs) |
| `/api/windows` | `GET` | List visible desktop windows (Win32 `EnumWindows`) |
| `/api/mouse_spy` | `GET` | Capture cursor position + window under cursor |
| `/api/register_autostart` | `POST` | Register `omniflow://` protocol + startup daemon |

---

## ⚙️ Optional Dependencies

Install only what you need:

```bash
# Browser automation via Playwright
pip install playwright
playwright install chromium

# Vision / screen matching
pip install opencv-python pillow

# OCR
pip install pytesseract
# Also install Tesseract binary: https://github.com/tesseract-ocr/tesseract

# SQL Server / ODBC
pip install pyodbc

# Windows UI Automation (UIA selector clicking)
pip install uiautomation
```

The engine runs without any of these — missing drivers simply raise a clear error for the steps that need them.

---

## 📁 Project Structure

```
omniflow/
├── omniflow_engine.py       # Python HTTP server + all automation drivers
├── omniflow_studio.html     # Single-file visual workflow builder (no build step)
├── .gitignore
└── README.md
```

**Persistent data** (auto-created, not in repo):
```
~/.omniflow/
├── omniflow_storage.db      # SQLite: workflows, templates, settings, run history
└── omniflow_storage.json    # JSON mirror / backup
```

---

## 🛠️ CLI Usage

```bash
# Start server (opens Studio in browser)
python omniflow_engine.py

# Start as background daemon (no window, no browser open)
python omniflow_engine.py --daemon

# Run a saved workflow JSON file headlessly
python omniflow_engine.py --run path/to/workflow.json

# Register omniflow:// protocol + Windows startup daemon
python omniflow_engine.py --register
```

---

## 🤝 Contributing

Pull requests are welcome! To get started:

```bash
git clone https://github.com/suiisharma/omniflow.git
cd omniflow
# Make your changes to omniflow_engine.py or omniflow_studio.html
# Test by running: python omniflow_engine.py
git checkout -b feature/my-feature
git commit -m "Add my feature"
git push origin feature/my-feature
```

---

## 📄 License

MIT © [suiisharma](https://github.com/suiisharma)
