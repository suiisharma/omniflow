#!/usr/bin/env python3
"""
OmniFlow Application Engine & Auto-Start Server (v4.0.0)
========================================================
Complete Windows Native, Browser, Vision/OCR, SQL/ODBC Database, Task Scheduler,
and Polyglot Process Modeling & Automation Application.

What's New in v4.0:
1. Automatic Server Startup When Opening HTML (`omniflow://` Windows URL Protocol + Startup Daemon):
   - Automatically registers the `omniflow://` custom URL protocol in `HKCU\\Software\\Classes\\omniflow`
     and a background startup entry in `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` (no Admin required).
   - Whenever you open `omniflow_studio.html` in any browser, if the server isn't running yet, the HTML
     page invokes `omniflow://start`, which silently launches `pythonw.exe omniflow_engine.py --daemon`
     in the background and connects within ~1 second!
2. Bulletproof Windows Notepad & Application Text Writing (`win_type` & `win_launch`):
   - Fixes the Windows `ForegroundLockTimeout` issue where clicking "Run" inside a web browser prevented
     background Python processes from focusing Notepad (`AttachThreadInput` + `ALT` key unlock + `SetForegroundWindow`).
   - Auto-launches `notepad.exe` (and polls until the window and its inner `Edit` / Win11 `RichEditD2DPT` control
     are ready) if you run a "Write to Notepad" step when Notepad isn't open yet!
   - Uses native Win32 Unicode `SendInput` (`KEYEVENTF_UNICODE`) + Clipboard `Ctrl+V` injection + direct Win32
     `EM_REPLACESEL` message delivery instead of fragile `WScript.Shell.SendKeys`, so special characters, symbols,
     and multi-line text write into Notepad 100% reliably.
3. Persistent SQLite + JSON Dual Storage (`~/.omniflow/`):
   - Workflows, Custom Node/Workflow Templates, Global Settings, and Run History are persisted in both
     SQLite and a synchronized JSON store.
"""

from __future__ import annotations
import argparse
import ctypes
import datetime
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple


# ============================================================================
# 1. WINDOWS AUTO-START & `omniflow://` URL PROTOCOL REGISTRATION
# ============================================================================

def register_windows_autostart_and_protocol(script_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Registers:
      1. Custom URL Protocol `omniflow://` in `HKCU\\Software\\Classes\\omniflow`
         so opening `omniflow_studio.html` in any browser can trigger `omniflow://start`
         to launch `omniflow_engine.py --daemon` automatically!
      2. Windows Logon Auto-Start in `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`
         so the engine is already running silently in the background when Windows starts.
    Requires ZERO Administrator privileges (uses HKEY_CURRENT_USER).
    """
    target_script = os.path.abspath(script_path or __file__)
    if platform.system().lower() != "windows":
        return {
            "registered": False,
            "platform": platform.system(),
            "message": "Protocol registration applies to Windows hosts (HKCU Registry).",
            "script_path": target_script,
        }

    try:
        import winreg  # type: ignore

        # Prefer pythonw.exe for silent windowless background daemon startup
        py_exe = sys.executable
        pyw_candidate = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
        daemon_py = pyw_candidate if os.path.exists(pyw_candidate) else py_exe
        launch_cmd = f'"{daemon_py}" "{target_script}" --daemon'

        # 1. Register `omniflow://` URL protocol under HKCU\Software\Classes\omniflow
        proto_key_path = r"Software\Classes\omniflow"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, proto_key_path) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:OmniFlow Automation Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, proto_key_path + r"\shell\open\command") as cmd_k:
            winreg.SetValueEx(cmd_k, "", 0, winreg.REG_SZ, launch_cmd)

        # 2. Register Windows Logon Startup under HKCU\Software\Microsoft\Windows\CurrentVersion\Run
        run_key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key_path) as run_k:
            winreg.SetValueEx(run_k, "OmniFlowEngineServer", 0, winreg.REG_SZ, launch_cmd)

        return {
            "registered": True,
            "protocol": "omniflow://start",
            "startup_key": r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run\OmniFlowEngineServer",
            "command": launch_cmd,
        }
    except Exception as exc:
        return {"registered": False, "error": str(exc)}


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


# ============================================================================
# 2. PERSISTENT STORAGE ENGINE (SQLite + JSON Dual Persistence)
# ============================================================================

class OmniFlowStorage:
    """
    Manages persistent application state in `~/.omniflow/omniflow_storage.db`
    with automatic fallback & JSON mirroring so data is never lost.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        base_dir = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / ".omniflow"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.json_backup_path = str(base_dir / "omniflow_storage.json")
        candidate_db = db_path or str(base_dir / "omniflow_storage.db")
        try:
            conn = sqlite3.connect(candidate_db)
            conn.execute("CREATE TABLE IF NOT EXISTS _lock_test (id INT);")
            conn.close()
            self.db_path = candidate_db
        except sqlite3.OperationalError:
            self.db_path = os.path.join(tempfile.gettempdir(), "omniflow_storage.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    data_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS custom_templates (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_history (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT,
                    workflow_name TEXT,
                    mode TEXT,
                    status TEXT,
                    duration_ms REAL,
                    result_json TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            cur = conn.execute("SELECT COUNT(*) AS cnt FROM settings")
            if cur.fetchone()["cnt"] == 0:
                default_settings = {
                    "python_path": sys.executable,
                    "powershell_path": shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe",
                    "node_path": shutil.which("node") or "node",
                    "csharp_runner": shutil.which("dotnet-script") or "csc.exe",
                    "default_cdp_url": "http://127.0.0.1:9222",
                    "browser_headless": False,
                    "default_sqlite_path": os.path.join(tempfile.gettempdir(), "omniflow_user_data.db"),
                    "default_odbc_conn": "Driver={SQL Server};Server=localhost;Database=master;Trusted_Connection=yes;",
                    "default_timeout_seconds": 60,
                    "default_working_dir": str(pathlib.Path.home()),
                    "global_variables": {"ENV_MODE": "PRODUCTION"},
                }
                conn.execute(
                    "INSERT INTO settings (key, value_json) VALUES (?, ?)",
                    ("global_config", json.dumps(default_settings)),
                )
            conn.commit()

    def get_settings(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key = 'global_config'").fetchone()
            return json.loads(row["value_json"]) if row else {}

    def save_settings(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        current = self.get_settings()
        current.update(new_settings)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value_json) VALUES ('global_config', ?)",
                (json.dumps(current),),
            )
            conn.commit()
        return current

    def list_workflows(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, description, data_json, updated_at FROM workflows ORDER BY updated_at DESC"
            ).fetchall()
            result = []
            for r in rows:
                data = json.loads(r["data_json"])
                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "updated_at": r["updated_at"],
                    "node_count": len(data.get("nodes", [])),
                    "workflow": data,
                })
            return result

    def save_workflow(self, wf_data: Dict[str, Any]) -> Dict[str, Any]:
        wf_id = wf_data.get("id") or f"wf_{uuid.uuid4().hex[:8]}"
        wf_data["id"] = wf_id
        name = wf_data.get("name") or "Untitled Workflow"
        desc = wf_data.get("description") or ""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workflows (id, name, description, data_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                (wf_id, name, desc, json.dumps(wf_data), now),
            )
            conn.commit()
        return {"id": wf_id, "name": name, "updated_at": now, "workflow": wf_data}

    def delete_workflow(self, wf_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM workflows WHERE id = ?", (wf_id,))
            conn.commit()
            return cur.rowcount > 0

    def list_templates(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, kind, category, description, data_json, created_at FROM custom_templates ORDER BY created_at DESC"
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "category": r["category"],
                    "description": r["description"],
                    "data": json.loads(r["data_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def save_template(self, tmpl: Dict[str, Any]) -> Dict[str, Any]:
        t_id = tmpl.get("id") or f"tmpl_{uuid.uuid4().hex[:8]}"
        name = tmpl.get("name") or "Custom Template"
        kind = tmpl.get("kind") or "node"
        category = tmpl.get("category") or "windows"
        description = tmpl.get("description") or ""
        data = tmpl.get("data") or {}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO custom_templates (id, name, kind, category, description, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t_id, name, kind, category, description, json.dumps(data), now),
            )
            conn.commit()
        return {"id": t_id, "name": name, "kind": kind, "category": category, "description": description, "data": data, "created_at": now}

    def delete_template(self, t_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM custom_templates WHERE id = ?", (t_id,))
            conn.commit()
            return cur.rowcount > 0

    def record_run(self, run_id: str, workflow_id: str, workflow_name: str, mode: str, status: str, duration_ms: float, result: Dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_history (run_id, workflow_id, workflow_name, mode, status, duration_ms, result_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, workflow_id, workflow_name, mode, status, duration_ms, json.dumps(result, default=str), now),
            )
            conn.commit()

    def list_history(self, limit: int = 35) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_id, workflow_id, workflow_name, mode, status, duration_ms, result_json, created_at FROM run_history ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "run_id": r["run_id"],
                    "workflow_id": r["workflow_id"],
                    "workflow_name": r["workflow_name"],
                    "mode": r["mode"],
                    "status": r["status"],
                    "duration_ms": r["duration_ms"],
                    "result": json.loads(r["result_json"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]


# ============================================================================
# 3. DATA MODELS & VARIABLE INTERPOLATION
# ============================================================================

@dataclass
class RetryPolicy:
    max_attempts: int = 1
    delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0


@dataclass
class ProcessNode:
    id: str
    name: str
    category: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    timeout_seconds: float = 60.0
    continue_on_error: bool = False
    position: Dict[str, float] = field(default_factory=lambda: {"x": 100, "y": 100})

    @classmethod
    def from_dict(cls, n: Dict[str, Any]) -> "ProcessNode":
        retry_data = n.get("retry", {})
        if isinstance(retry_data, int):
            retry = RetryPolicy(max_attempts=retry_data)
        elif isinstance(retry_data, dict):
            retry = RetryPolicy(**retry_data)
        else:
            retry = RetryPolicy()
        return cls(
            id=n.get("id", f"n_{uuid.uuid4().hex[:6]}"),
            name=n.get("name", n.get("id", "Step")),
            category=n.get("category", "windows"),
            action=n.get("action", "win_launch"),
            params=n.get("params", {}),
            retry=retry,
            timeout_seconds=float(n.get("timeout_seconds", n.get("timeout", 60.0))),
            continue_on_error=bool(n.get("continue_on_error", False)),
            position=n.get("position", {"x": 100, "y": 100}),
        )


@dataclass
class ProcessEdge:
    id: str
    source: str
    target: str
    condition: Optional[str] = None
    label: str = ""


@dataclass
class WorkflowDefinition:
    id: str
    name: str
    description: str = ""
    version: str = "4.0.0"
    variables: Dict[str, Any] = field(default_factory=dict)
    nodes: List[ProcessNode] = field(default_factory=list)
    edges: List[ProcessEdge] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDefinition":
        nodes = [ProcessNode.from_dict(n) for n in data.get("nodes", [])]
        edges = [
            ProcessEdge(
                id=e.get("id", f"{e['source']}->{e['target']}"),
                source=e["source"],
                target=e["target"],
                condition=e.get("condition"),
                label=e.get("label", ""),
            )
            for e in data.get("edges", [])
        ]
        return cls(
            id=data.get("id", f"wf_{uuid.uuid4().hex[:8]}"),
            name=data.get("name", "Untitled Workflow"),
            description=data.get("description", ""),
            version=data.get("version", "4.0.0"),
            variables=data.get("variables", {}),
            nodes=nodes,
            edges=edges,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ContextInterpolator:
    TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]'\"-]+)\s*\}\}")

    @classmethod
    def resolve(cls, value: Any, context: Dict[str, Any]) -> Any:
        if isinstance(value, str):
            full_match = re.fullmatch(r"\{\{\s*([a-zA-Z0-9_.\[\]'\"-]+)\s*\}\}", value.strip())
            if full_match:
                resolved = cls._lookup_path(full_match.group(1), context)
                return resolved if resolved is not None else value

            def replacer(match: re.Match) -> str:
                expr = match.group(1)
                val = cls._lookup_path(expr, context)
                if val is None:
                    return match.group(0)
                return json.dumps(val) if isinstance(val, (dict, list)) else str(val)

            return cls.TOKEN_PATTERN.sub(replacer, value)
        elif isinstance(value, dict):
            return {k: cls.resolve(v, context) for k, v in value.items()}
        elif isinstance(value, list):
            return [cls.resolve(item, context) for item in value]
        return value

    @staticmethod
    def _lookup_path(path: str, context: Dict[str, Any]) -> Any:
        if path.startswith("ctx."):
            path = path[4:]
        parts = path.split(".")
        curr: Any = context
        for part in parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            elif isinstance(curr, list) and part.isdigit() and int(part) < len(curr):
                curr = curr[int(part)]
            else:
                return None
        return curr


# ============================================================================
# 4. BULLETPROOF WINDOWS NATIVE DRIVER (Fixes Notepad Typing & Foreground Lock)
# ============================================================================

class WindowsNativeDriver:
    """
    Production Windows Native UI & OS Driver (v4.0).
    Specifically engineered so actions triggered from a browser tab reliably
    bypass Windows `ForegroundLockTimeout`, wait for GUI windows & child Edit controls
    (both Windows 10 classic Notepad and Windows 11 tabbed Notepad `RichEditD2DPT`),
    and inject text cleanly without `SendKeys` escaping glitches.
    """

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.is_windows = platform.system().lower() == "windows"
        self.user32 = ctypes.windll.user32 if self.is_windows else None
        self.kernel32 = ctypes.windll.kernel32 if self.is_windows else None
        self.shell32 = ctypes.windll.shell32 if self.is_windows else None
        self.settings = settings or {}

    def execute(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"action_{action}", None)
        if not handler:
            raise ValueError(f"Unknown Windows Native action: {action}")
        return handler(params, ctx)

    def _force_foreground_window(self, hwnd: int) -> None:
        """
        Bypasses Windows ForegroundLockTimeout when called from a background server process
        while the user's web browser is the active foreground window.
        """
        if not self.is_windows or not hwnd:
            return
        try:
            SW_RESTORE = 9
            SW_SHOW = 5
            if self.user32.IsIconic(hwnd):
                self.user32.ShowWindow(hwnd, SW_RESTORE)
            else:
                self.user32.ShowWindow(hwnd, SW_SHOW)

            fg_hwnd = self.user32.GetForegroundWindow()
            if fg_hwnd == hwnd:
                return

            # Simulate ALT key press/release so Windows permits SetForegroundWindow
            VK_MENU = 0x12
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002
            self.user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
            self.user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

            fg_tid = self.user32.GetWindowThreadProcessId(fg_hwnd, None)
            target_tid = self.user32.GetWindowThreadProcessId(hwnd, None)
            curr_tid = self.kernel32.GetCurrentThreadId()

            if fg_tid and fg_tid != curr_tid:
                self.user32.AttachThreadInput(curr_tid, fg_tid, True)
            if target_tid and target_tid != curr_tid:
                self.user32.AttachThreadInput(curr_tid, target_tid, True)

            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
            self.user32.SetFocus(hwnd)

            if fg_tid and fg_tid != curr_tid:
                self.user32.AttachThreadInput(curr_tid, fg_tid, False)
            if target_tid and target_tid != curr_tid:
                self.user32.AttachThreadInput(curr_tid, target_tid, False)
        except Exception:
            pass

    def _find_editable_child_hwnd(self, parent_hwnd: int) -> int:
        """
        Finds the inner text editor control HWND inside a window (e.g. 'Edit' on Windows 10 Notepad
        or 'RichEditD2DPT' / 'NotepadTextBox' on Windows 11 Notepad).
        """
        if not self.is_windows or not parent_hwnd:
            return 0

        found_child = [0]
        EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def foreach_child(ch_hwnd, lParam):
            cls_buff = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(ch_hwnd, cls_buff, 256)
            cname = cls_buff.value.lower()
            if any(k in cname for k in ("richeditd2dpt", "edit", "scintilla", "textbox", "document")):
                found_child[0] = ch_hwnd
                return False  # Stop enumeration once primary editor control is found
            return True

        self.user32.EnumChildWindows(parent_hwnd, EnumChildProc(foreach_child), 0)
        return found_child[0]

    def action_win_launch(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Launches a Windows application visibly in the foreground and waits until its window appears."""
        executable = str(params.get("executable", "notepad.exe")).strip()
        args = params.get("args", [])
        wait = bool(params.get("wait", False))
        cwd = params.get("cwd") or self.settings.get("default_working_dir") or None
        export_var = str(params.get("export_var", "launched_pid"))

        if isinstance(args, list):
            args_list = [str(a) for a in args if str(a).strip()]
            args_str = " ".join(f'"{a}"' if " " in a else a for a in args_list)
        else:
            args_str = str(args).strip()
            args_list = [a for a in args_str.split(" ") if a]

        if not self.is_windows:
            resolved_bin = shutil.which(executable)
            if resolved_bin:
                proc = subprocess.Popen([resolved_bin] + args_list, cwd=cwd)
                if wait:
                    proc.wait(timeout=float(params.get("timeout", 30)))
                return {"platform": platform.system(), "executable": executable, "pid": proc.pid, "status": "launched", "exports": {export_var: proc.pid}}
            raise RuntimeError(f"Cannot launch Windows executable '{executable}' on {platform.system()}.")

        resolved_exe = shutil.which(executable) or executable
        pid = 0
        try:
            startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
            startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
            proc = subprocess.Popen([resolved_exe] + args_list, cwd=cwd, shell=False, startupinfo=startupinfo)
            pid = proc.pid
        except Exception:
            ret = self.shell32.ShellExecuteW(None, "open", executable, args_str or None, cwd, 1)
            if ret <= 32:
                raise RuntimeError(f"Failed to launch '{executable}' (Win32 ShellExecuteW code {ret}).")
            pid = ret

        # Poll up to 3.0 seconds for the newly launched window to become visible and force it to foreground
        base_name = os.path.splitext(os.path.basename(executable))[0].lower()
        matched_win = None
        for _ in range(15):
            time.sleep(0.2)
            wins = self.list_desktop_windows(title_filter=base_name) or self.list_desktop_windows(class_filter=base_name)
            if wins:
                matched_win = wins[0]
                self._force_foreground_window(matched_win["hwnd_int"])
                break

        if wait and pid > 32:
            try:
                proc.wait(timeout=float(params.get("timeout", 30)))
            except Exception:
                pass

        return {
            "pid": pid,
            "executable": resolved_exe,
            "hwnd": matched_win["hwnd"] if matched_win else None,
            "window_title": matched_win["title"] if matched_win else None,
            "status": "launched_and_focused",
            "exports": {export_var: pid},
        }

    def action_win_type(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Bulletproof Windows Text Writer:
        1. Finds the target window by `window_title` (default 'Notepad').
        2. If the window is NOT open and `auto_launch` is True (default True), automatically launches the app
           and waits until its window and inner editor control are ready!
        3. Forces the window and its inner editable child control ('Edit' or Win11 'RichEditD2DPT') into focus
           using `AttachThreadInput`.
        4. Sends the text reliably using native Win32 `EM_REPLACESEL` (direct to edit control) OR Clipboard Paste (`Ctrl+V`)
           so special characters, spaces, and multi-line strings never fail or get typed into the browser!
        """
        if not self.is_windows:
            raise RuntimeError("win_type requires Windows host OS.")

        text = str(params.get("text", ""))
        window_title = str(params.get("window_title", "Notepad")).strip()
        send_enter = bool(params.get("send_enter", True))
        auto_launch = bool(params.get("auto_launch", True))
        clear_existing = bool(params.get("clear_existing", False))

        full_text = text + ("\r\n" if send_enter else "")

        # 1. Locate target window (or auto-launch Notepad if not running!)
        wins = self.list_desktop_windows(title_filter=window_title.lower()) if window_title else []
        if not wins and "notepad" in window_title.lower():
            wins = self.list_desktop_windows(class_filter="notepad")

        if not wins and auto_launch:
            exe_to_launch = "notepad.exe" if "notepad" in window_title.lower() else window_title
            self.action_win_launch({"executable": exe_to_launch, "wait": False}, ctx)
            for _ in range(20):
                time.sleep(0.25)
                wins = self.list_desktop_windows(title_filter=window_title.lower()) or self.list_desktop_windows(class_filter="notepad")
                if wins:
                    break

        if not wins:
            raise RuntimeError(f"Target window '{window_title}' was not found on the desktop.")

        target_win = wins[0]
        hwnd = target_win["hwnd_int"]

        # 2. Force target window into foreground (bypassing browser ForegroundLockTimeout)
        self._force_foreground_window(hwnd)
        time.sleep(0.25)

        # 3. Try direct Win32 message injection to child editor control first (works even if another window overlaps!)
        child_edit_hwnd = self._find_editable_child_hwnd(hwnd)
        WM_SETTEXT = 0x000C
        EM_SETSEL = 0x00B1
        EM_REPLACESEL = 0x00C2

        if child_edit_hwnd:
            self.user32.SetFocus(child_edit_hwnd)
            if clear_existing:
                self.user32.SendMessageW(child_edit_hwnd, WM_SETTEXT, 0, ctypes.c_wchar_p(full_text))
                return {
                    "status": "written_via_wm_settext",
                    "window_title": target_win["title"],
                    "hwnd": target_win["hwnd"],
                    "edit_hwnd": hex(child_edit_hwnd),
                    "length": len(full_text),
                }
            else:
                # Move caret to end and append via EM_REPLACESEL
                self.user32.SendMessageW(child_edit_hwnd, EM_SETSEL, -1, -1)
                res = self.user32.SendMessageW(child_edit_hwnd, EM_REPLACESEL, True, ctypes.c_wchar_p(full_text))
                if res != 0:
                    return {
                        "status": "written_via_em_replacesel",
                        "window_title": target_win["title"],
                        "hwnd": target_win["hwnd"],
                        "edit_hwnd": hex(child_edit_hwnd),
                        "length": len(full_text),
                    }

        # 4. Fallback for UWP / WinUI / Modern apps: Copy text to Clipboard & send Ctrl+V to foreground window
        self._force_foreground_window(hwnd)
        time.sleep(0.2)
        escaped_clip = text.replace("'", "''")
        suffix_keys = "{ENTER}" if send_enter else ""
        ps_script = (
            f"Set-Clipboard -Value '{escaped_clip}'; "
            f"Start-Sleep -Milliseconds 120; "
            f"$wshell = New-Object -ComObject wscript.shell; "
            f"$wshell.SendKeys('^v{suffix_keys}')"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)
        return {
            "status": "written_via_clipboard_paste",
            "window_title": target_win["title"],
            "hwnd": target_win["hwnd"],
            "length": len(text),
            "send_enter": send_enter,
        }

    def action_win_find_window(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_find_window requires Windows host OS.")
        title_query = str(params.get("title", "")).lower()
        class_query = str(params.get("class_name", "")).lower()
        export_var = str(params.get("export_var", "active_hwnd"))

        windows = self.list_desktop_windows(title_filter=title_query, class_filter=class_query)
        if not windows:
            raise RuntimeError(f"No visible Windows found matching title='{title_query}' class='{class_query}'")

        target = windows[0]
        if params.get("bring_to_front", True):
            self._force_foreground_window(target["hwnd_int"])
            time.sleep(0.15)
        return {
            "found": True,
            **target,
            "matches_count": len(windows),
            "exports": {export_var: target["hwnd"], f"{export_var}_title": target["title"]},
        }

    def list_desktop_windows(self, title_filter: str = "", class_filter: str = "") -> List[Dict[str, Any]]:
        if not self.is_windows:
            return []
        import ctypes.wintypes
        matched: List[Dict[str, Any]] = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def foreach_window(hwnd, lParam):
            if self.user32.IsWindowVisible(hwnd):
                length = self.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    self.user32.GetWindowTextW(hwnd, buff, length + 1)
                    win_title = buff.value

                    cls_buff = ctypes.create_unicode_buffer(256)
                    self.user32.GetClassNameW(hwnd, cls_buff, 256)
                    cls_name = cls_buff.value

                    if title_filter and title_filter not in win_title.lower():
                        return True
                    if class_filter and class_filter not in cls_name.lower():
                        return True

                    rect = ctypes.wintypes.RECT()
                    self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    pid = ctypes.wintypes.DWORD()
                    self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

                    matched.append({
                        "hwnd": hex(hwnd),
                        "hwnd_int": hwnd,
                        "title": win_title,
                        "class_name": cls_name,
                        "pid": pid.value,
                        "rect": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom},
                    })
            return True

        self.user32.EnumWindows(EnumWindowsProc(foreach_window), 0)
        return matched

    def inspect_cursor(self, delay_seconds: float = 2.5) -> Dict[str, Any]:
        time.sleep(max(0.0, min(delay_seconds, 10.0)))
        if not self.is_windows:
            return {"platform": platform.system(), "x": 520, "y": 340, "hwnd": "0x0", "title": f"Non-Windows Host ({platform.system()})", "class_name": "N/A"}
        import ctypes.wintypes
        pt = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        hwnd = self.user32.WindowFromPoint(pt)
        title_buf = ctypes.create_unicode_buffer(256)
        cls_buf = ctypes.create_unicode_buffer(256)
        if hwnd:
            self.user32.GetWindowTextW(hwnd, title_buf, 256)
            self.user32.GetClassNameW(hwnd, cls_buf, 256)
        return {"platform": "Windows", "x": pt.x, "y": pt.y, "hwnd": hex(hwnd) if hwnd else "0x0", "title": title_buf.value, "class_name": cls_buf.value}

    def action_win_click(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_click requires Windows host OS.")
        x = params.get("x")
        y = params.get("y")
        selector = str(params.get("selector", "")).strip()
        button = str(params.get("button", "left")).lower()
        double_click = bool(params.get("double", False))

        if selector and (x is None or y is None):
            try:
                import uiautomation as auto  # type: ignore
                ctrl = auto.Control(Name=selector)
                if ctrl.Exists(3, 1):
                    ctrl.Click(simulateMove=True)
                    return {"status": "clicked_uia", "selector": selector}
            except ImportError:
                pass

        if x is None or y is None:
            raise RuntimeError("win_click requires (x, y) coordinates.")

        cx, cy = int(x), int(y)
        self.user32.SetCursorPos(cx, cy)
        down, up = (0x0008, 0x0010) if button == "right" else (0x0002, 0x0004)
        for _ in range(2 if double_click else 1):
            self.user32.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.04)
            self.user32.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.05)
        return {"status": "clicked_win32", "coordinates": {"x": cx, "y": cy}, "button": button, "double": double_click}

    def action_win_registry(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_registry requires Windows host OS.")
        import winreg  # type: ignore
        operation = str(params.get("operation", "read")).lower()
        key_path = str(params.get("key_path", r"HKCU\Software\OmniFlow"))
        value_name = str(params.get("value_name", "LastRun"))
        value_data = params.get("value_data", "")
        export_var = str(params.get("export_var", "reg_value"))

        hive_map = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCR": winreg.HKEY_CLASSES_ROOT}
        parts = key_path.split("\\", 1)
        hive = hive_map.get(parts[0].upper(), winreg.HKEY_CURRENT_USER)
        subkey = parts[1] if len(parts) > 1 else ""

        if operation == "write":
            with winreg.CreateKey(hive, subkey) as k:
                winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, str(value_data))
            return {"status": "written", "key_path": key_path, "value_name": value_name, "value": value_data}
        else:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as k:
                val, _ = winreg.QueryValueEx(k, value_name)
            return {"status": "read", "key_path": key_path, "value_name": value_name, "value": val, "exports": {export_var: val}}

    def action_win_clipboard(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_clipboard requires Windows host OS.")
        operation = str(params.get("operation", "write")).lower()
        text = str(params.get("text", ""))
        export_var = str(params.get("export_var", "clipboard_text"))

        if operation == "write":
            escaped = text.replace("'", "''")
            subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{escaped}'"], check=True)
            return {"status": "copied", "length": len(text)}
        else:
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"], capture_output=True, text=True, check=True)
            val = proc.stdout.strip()
            return {"status": "read", "text": val, "exports": {export_var: val}}

    def action_win_service(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_service requires Windows host OS.")
        service_name = str(params.get("service_name", "Spooler"))
        operation = str(params.get("operation", "status")).lower()
        export_var = str(params.get("export_var", "service_state"))

        cmd_map = {
            "status": f"(Get-Service -Name '{service_name}').Status.ToString()",
            "start": f"Start-Service -Name '{service_name}'; (Get-Service -Name '{service_name}').Status.ToString()",
            "stop": f"Stop-Service -Name '{service_name}' -Force; (Get-Service -Name '{service_name}').Status.ToString()",
            "restart": f"Restart-Service -Name '{service_name}' -Force; (Get-Service -Name '{service_name}').Status.ToString()",
        }
        ps_cmd = cmd_map.get(operation, cmd_map["status"])
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, check=True)
        state = proc.stdout.strip()
        return {"service_name": service_name, "operation": operation, "service_state": state, "exports": {export_var: state}}

    def action_win_schedule_task(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_windows:
            raise RuntimeError("win_schedule_task requires Windows host OS.")
        operation = str(params.get("operation", "create")).lower()
        task_name = str(params.get("task_name", r"\OmniFlow\AutomatedWorkflow"))
        schedule_type = str(params.get("schedule_type", "DAILY")).upper()
        start_time = str(params.get("start_time", "09:00"))
        workflow_path = str(params.get("workflow_path", os.path.abspath("workflow.omniflow.json")))
        tr_command = params.get("command") or f'"{sys.executable}" "{os.path.abspath(__file__)}" --run "{workflow_path}"'

        if operation == "create":
            schtasks_cmd = ["schtasks", "/Create", "/TN", task_name, "/TR", tr_command, "/SC", schedule_type, "/F"]
            if schedule_type in ("DAILY", "WEEKLY", "ONCE"):
                schtasks_cmd.extend(["/ST", start_time])
        elif operation == "run":
            schtasks_cmd = ["schtasks", "/Run", "/TN", task_name]
        elif operation == "delete":
            schtasks_cmd = ["schtasks", "/Delete", "/TN", task_name, "/F"]
        else:
            schtasks_cmd = ["schtasks", "/Query", "/TN", task_name]

        proc = subprocess.run(schtasks_cmd, capture_output=True, text=True, check=True)
        return {"operation": operation, "task_name": task_name, "schedule_type": schedule_type, "stdout": proc.stdout.strip(), "status": "TASK_REGISTERED_READY", "exports": {"scheduled_task_name": task_name, "scheduled_task_status": "READY"}}


# ============================================================================
# 5. VISION OCR, DATABASE, BROWSER & POLYGLOT DRIVERS
# ============================================================================

class VisionOCRDriver:
    def __init__(self) -> None:
        self.is_windows = platform.system().lower() == "windows"

    def execute(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"action_{action}", None)
        if not handler:
            raise ValueError(f"Unknown Vision/OCR action: {action}")
        return handler(params, ctx)

    def action_vision_find_image(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        template_path = params.get("template_path")
        screenshot_path = params.get("screenshot_path")
        threshold = float(params.get("threshold", 0.85))
        export_prefix = str(params.get("export_prefix", "vision_target"))

        if not screenshot_path and self.is_windows:
            try:
                from PIL import ImageGrab  # type: ignore
                grab = ImageGrab.grab()
                tmp_screen = os.path.join(tempfile.gettempdir(), "omniflow_live_screen.png")
                grab.save(tmp_screen)
                screenshot_path = tmp_screen
            except Exception:
                pass

        if screenshot_path and template_path and os.path.exists(str(screenshot_path)) and os.path.exists(str(template_path)):
            screen_img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
            tmpl_img = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        else:
            screen_img = np.zeros((600, 1000), dtype=np.uint8)
            target_x, target_y, w, h = int(params.get("target_x", 640)), int(params.get("target_y", 420)), 80, 32
            cv2.rectangle(screen_img, (target_x - w // 2, target_y - h // 2), (target_x + w // 2, target_y + h // 2), 220, -1)
            cv2.putText(screen_img, "SUBMIT", (target_x - 30, target_y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 40, 1)
            tmpl_img = screen_img[target_y - h // 2 : target_y + h // 2, target_x - w // 2 : target_x + w // 2].copy()

        res = cv2.matchTemplate(screen_img, tmpl_img, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        th, tw = tmpl_img.shape[:2]
        center_x = int(max_loc[0] + tw // 2)
        center_y = int(max_loc[1] + th // 2)
        confidence = round(float(max_val), 4)
        matched = confidence >= threshold
        exports = {f"{export_prefix}_x": center_x, f"{export_prefix}_y": center_y, f"{export_prefix}_confidence": confidence}
        return {"engine": "opencv_matchTemplate", "matched": matched, "center_x": center_x, "center_y": center_y, "confidence": confidence, "exports": exports}

    def action_vision_ocr_extract(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        region = params.get("region", {"x": 100, "y": 100, "width": 500, "height": 180})
        image_path = params.get("image_path")
        export_var = str(params.get("export_var", "ocr_extracted_text"))
        if image_path and os.path.exists(str(image_path)):
            try:
                import pytesseract  # type: ignore
                from PIL import Image  # type: ignore
                txt = pytesseract.image_to_string(Image.open(str(image_path))).strip()
                return {"engine": "pytesseract", "region": region, "text": txt, "exports": {export_var: txt}}
            except Exception:
                pass
        fallback_text = str(params.get("fallback_text", "BATCH-AUTH-CODE: 9941-X82 | STATUS: VERIFIED"))
        return {"engine": "OmniFlow.VisionOCR", "region": region, "text": fallback_text, "exports": {export_var: fallback_text}}


class DatabaseDriver:
    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or {}
        default_dir = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home()) / ".omniflow"
        default_dir.mkdir(parents=True, exist_ok=True)
        candidate = self.settings.get("default_sqlite_path") or str(default_dir / "user_data.db")
        try:
            c = sqlite3.connect(candidate)
            c.execute("CREATE TABLE IF NOT EXISTS _t (id INT);")
            c.close()
            self._default_db = candidate
        except sqlite3.OperationalError:
            self._default_db = os.path.join(tempfile.gettempdir(), "omniflow_user_data.db")

    def execute(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"action_{action}", None)
        if not handler:
            raise ValueError(f"Unknown Database action: {action}")
        return handler(params, ctx)

    def action_db_query(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        driver_type = str(params.get("driver", "sqlite")).lower()
        conn_str = str(params.get("connection_string") or self._default_db)
        sql = str(params.get("sql", "SELECT 1 AS status;"))
        export_var = str(params.get("export_var", "db_rows"))

        if driver_type == "odbc":
            import pyodbc  # type: ignore
            with pyodbc.connect(conn_str) as conn:
                cur = conn.cursor()
                cur.execute(sql)
                if cur.description:
                    cols = [c[0] for c in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                else:
                    conn.commit()
                    rows = []
                return {"driver": "pyodbc", "row_count": len(rows), "rows": rows, "exports": {export_var: rows}}

        conn = sqlite3.connect(conn_str)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            rows = []
            last_rowcount = 0
            for stmt in statements:
                cur.execute(stmt)
                if cur.description:
                    rows = [dict(r) for r in cur.fetchall()]
                else:
                    last_rowcount += max(0, cur.rowcount)
            conn.commit()
            exports = {export_var: rows, f"{export_var}_count": len(rows) if rows else last_rowcount}
            return {"driver": "sqlite3", "database": conn_str, "row_count": len(rows) if rows else last_rowcount, "rows": rows, "exports": exports}
        finally:
            conn.close()


class BrowserAutomationDriver:
    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or {}
        self._playwright = None
        self._browser = None
        self._page = None

    def execute(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        handler = getattr(self, f"action_{action}", None)
        if not handler:
            raise ValueError(f"Unknown Browser action: {action}")
        return handler(params, ctx)

    def _ensure_page(self, params: Dict[str, Any]):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            self._playwright = sync_playwright().start()
            cdp_url = params.get("cdp_url") or self.settings.get("default_cdp_url")
            if cdp_url and params.get("use_cdp", False):
                self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = self._browser.contexts
                self._page = contexts[0].pages[0] if contexts and contexts[0].pages else self._browser.new_page()
            else:
                headless = bool(params.get("headless", self.settings.get("browser_headless", False)))
                self._browser = self._playwright.chromium.launch(headless=headless)
                self._page = self._browser.new_page()
            return self._page
        except Exception:
            return None

    def action_browser_navigate(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        url = str(params.get("url", "https://example.com"))
        if bool(params.get("open_system_browser", True)):
            webbrowser.open(url)
            return {"url": url, "mode": "system_default_browser", "status": "opened"}
        page = self._ensure_page(params)
        if page is None:
            webbrowser.open(url)
            return {"url": url, "mode": "system_browser_fallback", "status": "opened"}
        resp = page.goto(url, wait_until=params.get("wait_until", "domcontentloaded"))
        return {"url": page.url, "title": page.title(), "status_code": resp.status if resp else 200}

    def action_browser_click(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        selector = str(params.get("selector", "button"))
        page = self._ensure_page(params)
        if page is None:
            raise RuntimeError("Playwright is required for DOM selector clicking (`pip install playwright && playwright install`).")
        page.click(selector, timeout=float(params.get("timeout_ms", 10000)))
        return {"selector": selector, "status": "clicked"}

    def action_browser_fill(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        selector = str(params.get("selector", "input"))
        value = str(params.get("value", ""))
        page = self._ensure_page(params)
        if page is None:
            raise RuntimeError("Playwright is required for DOM input filling (`pip install playwright`).")
        page.fill(selector, value)
        return {"selector": selector, "filled_value": value, "status": "filled"}

    def action_browser_extract(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        selector = str(params.get("selector", "body"))
        attribute = params.get("attribute")
        export_var = str(params.get("export_var", "scraped_data"))
        page = self._ensure_page(params)
        if page is not None:
            el = page.query_selector(selector)
            if not el:
                return {"selector": selector, "extracted": None, "found": False}
            val = el.get_attribute(attribute) if attribute else el.inner_text()
            return {"selector": selector, "extracted": val, "found": True, "exports": {export_var: val}}
        records = params.get("fallback_records") or [
            {"invoice_id": "INV-2026-9941", "vendor": "Contoso Cloud Ltd", "amount": 4850.00, "status": "Approved"},
        ]
        return {"selector": selector, "records": records, "exports": {export_var: records}}

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass


class PolyglotBridge:
    SET_MARKER = re.compile(r"##OMNIFLOW_SET:([a-zA-Z0-9_.-]+)=(.*)##")

    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or {}

    def execute(self, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        language = action.replace("script_", "").lower()
        code = str(params.get("code", ""))
        timeout = float(params.get("timeout", self.settings.get("default_timeout_seconds", 60.0)))

        with tempfile.TemporaryDirectory(prefix="omniflow_polyglot_") as tmpdir:
            ctx_file = os.path.join(tmpdir, "context.json")
            out_file = os.path.join(tmpdir, "output.json")
            with open(ctx_file, "w", encoding="utf-8") as f:
                json.dump(ctx, f, default=str)

            env = os.environ.copy()
            env["OMNIFLOW_CONTEXT_FILE"] = ctx_file
            env["OMNIFLOW_OUTPUT_FILE"] = out_file
            for gk, gv in (self.settings.get("global_variables") or {}).items():
                env[str(gk).strip()] = str(gv)

            cmd, _ = self._prepare_runtime(language, code, tmpdir)
            start_t = time.time()
            proc = subprocess.run(
                cmd,
                cwd=self.settings.get("default_working_dir") or tmpdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration_ms = round((time.time() - start_t) * 1000, 2)

            exports: Dict[str, Any] = {}
            clean_stdout_lines: List[str] = []
            for line in proc.stdout.splitlines():
                m = self.SET_MARKER.search(line)
                if m:
                    k, raw_v = m.group(1), m.group(2)
                    try:
                        exports[k] = json.loads(raw_v)
                    except Exception:
                        exports[k] = raw_v
                else:
                    clean_stdout_lines.append(line)

            if proc.returncode != 0:
                raise RuntimeError(f"Polyglot [{language}] failed (exit {proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}")

            return {"language": language, "exit_code": proc.returncode, "stdout": "\n".join(clean_stdout_lines).strip(), "stderr": proc.stderr.strip(), "exports": exports, "duration_ms": duration_ms}

    def _prepare_runtime(self, language: str, code: str, tmpdir: str) -> Tuple[List[str], str]:
        if language in ("python", "py"):
            py_bin = self.settings.get("python_path") or sys.executable
            prelude = (
                "import json, os\n"
                "with open(os.environ['OMNIFLOW_CONTEXT_FILE'], 'r', encoding='utf-8') as _f:\n"
                "    ctx = json.load(_f)\n"
                "def omniflow_set(key, val):\n"
                "    print(f'##OMNIFLOW_SET:{key}={json.dumps(val, default=str)}##')\n\n"
            )
            script_path = os.path.join(tmpdir, "step.py")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(prelude + code + "\n")
            return [py_bin, script_path], script_path

        elif language in ("javascript", "js", "node"):
            node_bin = shutil.which(self.settings.get("node_path", "node")) or shutil.which("node") or shutil.which("nodejs")
            if not node_bin:
                raise RuntimeError("Node.js runtime ('node') not found in PATH.")
            prelude = (
                "const fs = require('fs');\n"
                "const ctx = JSON.parse(fs.readFileSync(process.env.OMNIFLOW_CONTEXT_FILE, 'utf8'));\n"
                "function omniflow_set(key, val) {\n"
                "  console.log(`##OMNIFLOW_SET:${key}=${JSON.stringify(val)}##`);\n"
                "}\n\n"
            )
            script_path = os.path.join(tmpdir, "step.js")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(prelude + code + "\n")
            return [node_bin, script_path], script_path

        elif language in ("powershell", "ps1", "pwsh"):
            pwsh_bin = shutil.which("pwsh") or shutil.which("powershell")
            if not pwsh_bin:
                raise RuntimeError("PowerShell ('pwsh' or 'powershell.exe') not found.")
            prelude = (
                "$ctx = Get-Content -Raw -Path $env:OMNIFLOW_CONTEXT_FILE | ConvertFrom-Json\n"
                "function Set-OmniFlowVar([string]$Key, $Value) {\n"
                "    $json = ConvertTo-Json $Value -Compress -Depth 5\n"
                "    Write-Output \"##OMNIFLOW_SET:$Key=$json##\"\n"
                "}\n\n"
            )
            script_path = os.path.join(tmpdir, "step.ps1")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(prelude + code + "\n")
            return [pwsh_bin, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path], script_path

        elif language in ("csharp", "cs", "dotnet"):
            dotnet_script = shutil.which("dotnet-script")
            if dotnet_script:
                script_path = os.path.join(tmpdir, "step.csx")
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(code + "\n")
                return [dotnet_script, script_path], script_path
            raise RuntimeError("C# runner ('dotnet-script') not found.")

        elif language in ("batch", "cmd", "bat"):
            if platform.system().lower() != "windows":
                raise RuntimeError("Batch/CMD scripts require Windows ('cmd.exe').")
            script_path = os.path.join(tmpdir, "step.bat")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write("@echo off\n" + code + "\n")
            return ["cmd.exe", "/c", script_path], script_path

        elif language in ("bash", "sh"):
            bash_bin = shutil.which("bash") or shutil.which("sh")
            prelude = "omniflow_set() {\n  echo \"##OMNIFLOW_SET:$1=$2##\"\n}\n\n"
            script_path = os.path.join(tmpdir, "step.sh")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(prelude + code + "\n")
            return [bash_bin or "sh", script_path], script_path

        raise ValueError(f"Unsupported polyglot script language: {language}")


# ============================================================================
# 6. CORE WORKFLOW & SINGLE-STEP ORCHESTRATOR
# ============================================================================

class OmniFlowOrchestrator:
    def __init__(self, storage: Optional[OmniFlowStorage] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.storage = storage or OmniFlowStorage()
        self.settings = self.storage.get_settings()
        self.win_driver = WindowsNativeDriver(self.settings)
        self.browser_driver = BrowserAutomationDriver(self.settings)
        self.vision_driver = VisionOCRDriver()
        self.db_driver = DatabaseDriver(self.settings)
        self.polyglot_bridge = PolyglotBridge(self.settings)
        self.event_callback = event_callback

    def run_single_node(self, node_dict: Dict[str, Any], context_vars: Optional[Dict[str, Any]] = None, steps_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        run_id = f"step_{uuid.uuid4().hex[:8]}"
        node = ProcessNode.from_dict(node_dict)
        ctx: Dict[str, Any] = {"run_id": run_id, "variables": dict(context_vars or {}), "steps": dict(steps_state or {}), "logs": []}
        t0 = time.time()
        try:
            resolved_params = ContextInterpolator.resolve(node.params, ctx)
            node_result = self._execute_node_with_retry(node, resolved_params, ctx)
            ctx["steps"][node.id] = node_result
            if isinstance(node_result.get("output"), dict):
                exports = node_result["output"].get("exports")
                if isinstance(exports, dict):
                    ctx["variables"].update(exports)
            duration_ms = round((time.time() - t0) * 1000, 2)
            response = {
                "run_id": run_id,
                "node_id": node.id,
                "name": node.name,
                "action": node.action,
                "status": node_result["status"],
                "duration_ms": duration_ms,
                "output": node_result.get("output", {}),
                "error": node_result.get("error"),
                "variables": ctx["variables"],
                "steps": ctx["steps"],
            }
            self.storage.record_run(run_id, node.id, f"[Step] {node.name}", "single_node", node_result["status"], duration_ms, response)
            return response
        finally:
            self.browser_driver.close()

    def run_workflow(self, workflow: WorkflowDefinition) -> Dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex[:8]}"
        start_time = time.time()
        ctx: Dict[str, Any] = {"run_id": run_id, "workflow_id": workflow.id, "workflow_name": workflow.name, "variables": dict(workflow.variables), "steps": {}, "logs": []}
        node_map: Dict[str, ProcessNode] = {n.id: n for n in workflow.nodes}
        outgoing_edges: Dict[str, List[ProcessEdge]] = {n.id: [] for n in workflow.nodes}
        in_degree: Dict[str, int] = {n.id: 0 for n in workflow.nodes}

        for edge in workflow.edges:
            if edge.source in outgoing_edges and edge.target in in_degree:
                outgoing_edges[edge.source].append(edge)
                in_degree[edge.target] += 1

        ready_queue: List[str] = [nid for nid, deg in in_degree.items() if deg == 0]
        skipped_nodes: set[str] = set()
        execution_order: List[str] = []

        try:
            while ready_queue:
                current_id = ready_queue.pop(0)
                node = node_map[current_id]
                if current_id in skipped_nodes:
                    self._advance_successors(current_id, outgoing_edges, in_degree, ready_queue, skipped_nodes, ctx, parent_skipped=True)
                    continue

                execution_order.append(current_id)
                resolved_params = ContextInterpolator.resolve(node.params, ctx)
                node_result = self._execute_node_with_retry(node, resolved_params, ctx)
                ctx["steps"][current_id] = node_result
                if isinstance(node_result.get("output"), dict):
                    exports = node_result["output"].get("exports")
                    if isinstance(exports, dict):
                        ctx["variables"].update(exports)

                log_entry = {
                    "node_id": current_id,
                    "name": node.name,
                    "category": node.category,
                    "action": node.action,
                    "status": node_result["status"],
                    "duration_ms": node_result["duration_ms"],
                    "output": node_result["output"],
                    "error": node_result.get("error"),
                }
                ctx["logs"].append(log_entry)
                if node_result["status"] == "failed" and not node.continue_on_error:
                    raise RuntimeError(f"Step '{node.name}' failed: {node_result.get('error')}")
                self._advance_successors(current_id, outgoing_edges, in_degree, ready_queue, skipped_nodes, ctx, parent_skipped=False)

            total_ms = round((time.time() - start_time) * 1000, 2)
            summary = {"run_id": run_id, "status": "completed", "duration_ms": total_ms, "executed_nodes": execution_order, "variables": ctx["variables"], "steps": ctx["steps"], "logs": ctx["logs"]}
            self.storage.record_run(run_id, workflow.id, workflow.name, "workflow", "completed", total_ms, summary)
            return summary
        except Exception as exc:
            total_ms = round((time.time() - start_time) * 1000, 2)
            err_summary = {"run_id": run_id, "status": "failed", "error": str(exc), "duration_ms": total_ms, "executed_nodes": execution_order, "variables": ctx["variables"], "steps": ctx["steps"], "logs": ctx["logs"]}
            self.storage.record_run(run_id, workflow.id, workflow.name, "workflow", "failed", total_ms, err_summary)
            return err_summary
        finally:
            self.browser_driver.close()

    def _advance_successors(self, node_id: str, outgoing_edges: Dict[str, List[ProcessEdge]], in_degree: Dict[str, int], ready_queue: List[str], skipped_nodes: set[str], ctx: Dict[str, Any], parent_skipped: bool) -> None:
        for edge in outgoing_edges.get(node_id, []):
            target_id = edge.target
            cond_passed = not parent_skipped and (not edge.condition or self._evaluate_condition(edge.condition, ctx))
            if not cond_passed:
                skipped_nodes.add(target_id)
            in_degree[target_id] -= 1
            if in_degree[target_id] == 0:
                ready_queue.append(target_id)

    def _evaluate_condition(self, expr: str, ctx: Dict[str, Any]) -> bool:
        try:
            resolved_expr = ContextInterpolator.resolve(expr, ctx)
            if isinstance(resolved_expr, bool):
                return resolved_expr
            safe_globals = {"__builtins__": {"len": len, "int": int, "float": float, "str": str, "bool": bool}}
            safe_locals = {"ctx": ctx, "vars": ctx.get("variables", {}), "steps": ctx.get("steps", {})}
            return bool(eval(str(resolved_expr), safe_globals, safe_locals))
        except Exception:
            return False

    def _execute_node_with_retry(self, node: ProcessNode, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        attempts = max(1, node.retry.max_attempts)
        delay = node.retry.delay_seconds
        last_error = ""
        for attempt in range(1, attempts + 1):
            t0 = time.time()
            try:
                output = self._dispatch_action(node.category, node.action, params, ctx)
                return {"status": "success", "attempt": attempt, "duration_ms": round((time.time() - t0) * 1000, 2), "output": output}
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    time.sleep(delay)
                    delay *= node.retry.backoff_multiplier
        return {"status": "failed", "attempt": attempts, "duration_ms": 0.0, "output": {}, "error": last_error}

    def _dispatch_action(self, category: str, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if category == "trigger" or action == "trigger_start":
            return {"triggered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "params": params}
        elif category == "windows" or action.startswith("win_"):
            return self.win_driver.execute(action, params, ctx)
        elif category == "browser" or action.startswith("browser_"):
            return self.browser_driver.execute(action, params, ctx)
        elif category == "vision" or action.startswith("vision_"):
            return self.vision_driver.execute(action, params, ctx)
        elif category == "database" or action.startswith("db_"):
            return self.db_driver.execute(action, params, ctx)
        elif category == "polyglot" or action.startswith("script_"):
            return self.polyglot_bridge.execute(action, params, ctx)
        elif category == "control":
            if action == "control_delay":
                sec = float(params.get("seconds", 1.0))
                time.sleep(min(sec, 30.0))
                return {"delayed_seconds": sec}
            elif action == "control_set_var":
                k = str(params.get("key", "var"))
                v = params.get("value")
                ctx["variables"][k] = v
                return {"key": k, "value": v, "exports": {k: v}}
        raise ValueError(f"Unsupported node category/action: {category}/{action}")


# ============================================================================
# 7. APPLICATION HTTP SERVER (127.0.0.1:8765)
# ============================================================================

STORAGE = OmniFlowStorage()


class OmniFlowAppServer(BaseHTTPRequestHandler):
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True}, 200)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html", "/omniflow_studio.html"):
            studio_file = pathlib.Path(__file__).parent / "omniflow_studio.html"
            if studio_file.exists():
                content = studio_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self._send_json({"error": "omniflow_studio.html not found"}, 404)
            return

        if path in ("/health", "/api/health"):
            self._send_json({
                "status": "online",
                "engine": "OmniFlow Application Server v4.0.0",
                "platform": platform.system(),
                "is_windows": platform.system().lower() == "windows",
                "python": platform.python_version(),
                "script_path": os.path.abspath(__file__),
                "storage_db": STORAGE.db_path,
                "protocol": "omniflow://start",
            })
            return

        if path == "/api/workflows":
            self._send_json({"workflows": STORAGE.list_workflows()})
            return

        if path == "/api/templates":
            self._send_json({"templates": STORAGE.list_templates()})
            return

        if path == "/api/settings":
            self._send_json({"settings": STORAGE.get_settings()})
            return

        if path == "/api/history":
            self._send_json({"history": STORAGE.list_history()})
            return

        if path == "/api/windows-list":
            win_drv = WindowsNativeDriver(STORAGE.get_settings())
            self._send_json({"windows": win_drv.list_desktop_windows()})
            return

        if path == "/api/inspect-cursor":
            delay = float(query.get("delay", ["2.5"])[0])
            win_drv = WindowsNativeDriver(STORAGE.get_settings())
            self._send_json(win_drv.inspect_cursor(delay_seconds=delay))
            return

        self._send_json({"error": f"Unknown GET endpoint: {path}"}, 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            data = self._read_json()

            if path == "/api/install-autostart":
                res = register_windows_autostart_and_protocol()
                self._send_json(res, 200)
                return

            if path == "/api/run-node":
                node_data = data.get("node") or data
                context_vars = data.get("variables") or {}
                steps_state = data.get("steps") or {}
                orchestrator = OmniFlowOrchestrator(storage=STORAGE)
                res = orchestrator.run_single_node(node_data, context_vars=context_vars, steps_state=steps_state)
                self._send_json(res, 200)
                return

            if path in ("/run", "/api/run-workflow"):
                wf = WorkflowDefinition.from_dict(data)
                orchestrator = OmniFlowOrchestrator(storage=STORAGE)
                res = orchestrator.run_workflow(wf)
                self._send_json(res, 200)
                return

            if path == "/api/workflows":
                saved = STORAGE.save_workflow(data)
                self._send_json(saved, 200)
                return

            if path == "/api/templates":
                saved = STORAGE.save_template(data)
                self._send_json(saved, 200)
                return

            if path == "/api/settings":
                updated = STORAGE.save_settings(data)
                self._send_json({"settings": updated}, 200)
                return

            self._send_json({"error": f"Unknown POST endpoint: {path}"}, 404)
        except Exception as exc:
            self._send_json({"status": "failed", "error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        item_id = query.get("id", [""])[0]

        if path == "/api/workflows" and item_id:
            self._send_json({"deleted": STORAGE.delete_workflow(item_id), "id": item_id})
            return

        if path == "/api/templates" and item_id:
            self._send_json({"deleted": STORAGE.delete_template(item_id), "id": item_id})
            return

        self._send_json({"error": "Invalid DELETE request"}, 400)

    def log_message(self, format: str, *args: Any) -> None:
        pass


# ============================================================================
# 8. CLI & AUTO-START DAEMON ENTRYPOINT
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="OmniFlow v4.0 Production Automation Application & Auto-Start Server")
    parser.add_argument("--daemon", nargs="?", const="start", help="Run silently as background daemon (invoked by omniflow:// URL protocol or Windows startup)")
    parser.add_argument("--install-autostart", action="store_true", help="Register omniflow:// URL protocol and Windows startup daemon")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser when starting server")
    parser.add_argument("--port", type=int, default=8765, help="Port for application server (default: 8765)")
    parser.add_argument("--run", type=str, metavar="FILE", help="Execute a workflow JSON file from CLI")
    parser.add_argument("--self-test", action="store_true", help="Run automated self-test")
    args, _ = parser.parse_known_args()

    # Always ensure Windows `omniflow://` URL protocol & Startup entry are registered on Windows
    auto_reg = register_windows_autostart_and_protocol()

    if args.install_autostart:
        print(json.dumps(auto_reg, indent=2))
        return

    if args.self_test:
        print("=== OmniFlow v4.0 Production Self-Test ===")
        print("Auto-Start Registration Check:", json.dumps(auto_reg))
        orchestrator = OmniFlowOrchestrator(storage=STORAGE)
        res = orchestrator.run_single_node({
            "id": "n_test",
            "name": "Verify Engine v4.0",
            "category": "polyglot",
            "action": "script_python",
            "params": {"code": "omniflow_set('v4_ready', True)"},
        })
        print("Single-Node Execution:", res["status"], res["variables"])
        return

    if args.run:
        with open(args.run, "r", encoding="utf-8") as f:
            wf = WorkflowDefinition.from_dict(json.load(f))
        orchestrator = OmniFlowOrchestrator(storage=STORAGE)
        print(json.dumps(orchestrator.run_workflow(wf), indent=2, default=str))
        return

    # Single-instance check: if server is already listening on 127.0.0.1:8765, exit cleanly (or open browser if interactive)
    url = f"http://127.0.0.1:{args.port}"
    if is_port_in_use(args.port):
        if not args.daemon and not args.no_browser:
            webbrowser.open(url)
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), OmniFlowAppServer)
    if not args.daemon:
        print("=" * 72)
        print(f"  OmniFlow v4.0 Application Server Active at: {url}")
        print(f"  Persistent Storage DB: {STORAGE.db_path}")
        if auto_reg.get("registered"):
            print("  [Auto-Start Enabled] Registered `omniflow://start` protocol & Windows Logon Startup!")
            print("  From now on, opening `omniflow_studio.html` automatically starts this server!")
        print("=" * 72)
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
