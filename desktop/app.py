from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from desktop.common import ROOT, VERSION, atomic_json, data_dir
from desktop.runtime import configure_node, ensure_node, ensure_webview2
from desktop.updates import UpdateService


def select_port(preferred: int) -> int:
    """Keep the configured port unless another listener or reservation owns it."""
    preferred = int(preferred)
    if not 1 <= preferred <= 65535:
        raise ValueError("服务端口必须在 1 到 65535 之间")
    with socket.socket() as sock:
        try:
            with socket.socket() as probe:
                probe.settimeout(.2)
                if probe.connect_ex(("127.0.0.1", preferred)) == 0:
                    raise OSError("Port already in use")
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def save_port(base: Path, config: dict, port: int) -> None:
    if config.get("port") != port:
        # Write only after our own backend confirms readiness, never on a failed start.
        config["port"] = port
        atomic_json(base / "config.json", config)


def configure_environment(base: Path) -> dict:
    file = base / "config.json"
    if file.is_file():
        config = json.loads(file.read_text(encoding="utf-8"))
    else:
        config = {"port": 8790, "username": "desktop", "password": secrets.token_urlsafe(32),
                  "jwt_secret": secrets.token_urlsafe(48)}
        atomic_json(file, config)
    if "converter_api_key" not in config:
        config["converter_api_key"] = "wb-" + secrets.token_urlsafe(32)
        atomic_json(file, config)
    os.environ.update({"WORKBUDDY_DESKTOP": "1", "WORKBUDDY_DESKTOP_DATA": str(base),
                       "ADMIN_DATABASE_URL": "sqlite:///" + (base / "workbuddy.db").as_posix(),
                       "ADMIN_GATEWAY_LIMIT_BACKEND": "memory", "ADMIN_HOST": "127.0.0.1",
                       "ADMIN_USERNAME": config["username"], "ADMIN_PASSWORD": config["password"],
                       "ADMIN_JWT_SECRET": config["jwt_secret"], "PYTHONUTF8": "1",
                       "CONVERTER_API_KEY": config["converter_api_key"],
                       "CONVERTER_LOG": str(base / "logs/converter.log")})
    configure_node(base)
    os.chdir(base)
    return config


def setup_logging(base: Path):
    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs / "desktop.log", maxBytes=4*1048576,
                                 backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def parent_watchdog():
    pid = int(os.environ.get("WB_DESKTOP_PARENT", "0"))
    if not pid or os.name != "nt":
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        os._exit(0)
    kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    os._exit(0)


def backend(port: int):
    base = data_dir()
    configure_environment(base)
    setup_logging(base)
    threading.Thread(target=parent_watchdog, daemon=True).start()
    import uvicorn
    from admin.server import app, _CONVERTER_EMBEDDED
    if not _CONVERTER_EMBEDDED:
        raise RuntimeError("内嵌网关未能加载，请检查打包依赖")
    from admin.db import init_db
    if not init_db():
        raise RuntimeError("本地数据库初始化失败")
    def runtime_watch():
        from desktop.runtime import node_dir
        for _ in range(600):
            if (node_dir(base) / "node.exe").is_file():
                from admin.turing_token import clear_cache, warmup
                clear_cache()
                warmup()
                return
            time.sleep(1)
    threading.Thread(target=runtime_watch, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False,
                loop="asyncio", http="h11", ws="none", timeout_graceful_shutdown=5)


SPLASH = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
body{margin:0;background:#f5f6f8;color:#16181d;font:15px 'Segoe UI','Microsoft YaHei',sans-serif;
display:grid;place-items:center;height:100vh}main{width:420px;padding:40px;background:white;
border-radius:24px;box-shadow:0 10px 40px #16241c0c}h1{font-size:26px}p{color:#656970;line-height:1.8}
.bar{height:5px;background:#e6f5ed;border-radius:6px;overflow:hidden}.bar:after{content:'';
display:block;width:40%;height:100%;background:#42c08e;animation:load 1.3s infinite}
@keyframes load{to{transform:translateX(260%)}}button{padding:10px;border:0;border-radius:8px;cursor:pointer}
</style><main><h1>WorkBuddy2API</h1><p id="status">正在启动本地服务…</p><div class="bar" id="bar"></div>
<p>首次运行会自动准备环境，以后直接打开。</p><button onclick="pywebview.api.retry_start()">重试启动</button></main>
</html>"""


class Bridge:
    """Expose methods only: pywebview recursively serializes public object attributes."""
    def __init__(self, controller):
        self._controller = controller

    def startup_status(self):
        return self._controller.startup_status()

    def desktop_login(self):
        return self._controller.desktop_login()

    def check_updates(self):
        return self._controller.check_updates()

    def update_status(self):
        return self._controller.update_status()

    def retry_runtime(self):
        return self._controller.retry_runtime()

    def retry_start(self):
        return self._controller.retry_start()

    def download_update(self, release_id):
        return self._controller.download_update(release_id)

    def apply_update(self):
        return self._controller.apply_update()


class DesktopAPI:
    def __init__(self, base: Path, config: dict):
        self.base, self.config = base, config
        self.window = None
        self.process = None
        self.port = 0
        self.message = "正在启动本地服务…"
        self.failed = False
        self.runtime_message = "正在准备 Node.js…"
        self.runtime_busy = False
        self.update_state = {"busy": False, "message": "", "ready": False}
        self.update_file = None
        self.updates = UpdateService(base)
        self.closing = threading.Event()
        self.start_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.update_lock = threading.Lock()

    def startup_status(self):
        return {"message": self.message, "failed": self.failed}

    def desktop_login(self):
        # 只校验 **path/host/port**，不比较完整 URL。
        #
        # 原来是 `!= f".../admin?desktop=1"` 的等值比较，而前端切换面板时会用
        # history.replaceState 把 `?tab=xxx` 写进地址栏（index.html 的 activateTab），
        # 于是 URL 变成 `/admin?desktop=1&tab=logs` → 合法的登录请求被**误拒**，
        # 用户看到"只允许本地桌面界面登录"。查询参数不该影响这个判定。
        #
        # 真正的安全边界是 `X-Desktop-Boot` nonce 握手（见 releases.py 的
        # secrets.compare_digest），这里只是附加的本地性检查。
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(self.window.get_current_url() or "")
            ok_host = parts.hostname in ("127.0.0.1", "localhost")
            ok_port = parts.port == self.port
            ok_path = parts.path.rstrip("/") == "/admin"
        except Exception:
            ok_host = ok_port = ok_path = False
        if not (ok_host and ok_port and ok_path):
            raise RuntimeError("只允许本地桌面界面登录")
        from datetime import datetime, timedelta, timezone
        import jwt
        # Local ownership authenticates the shell; no password form or login request.
        token = jwt.encode({"sub": self.config["username"],
                            "exp": datetime.now(timezone.utc) + timedelta(hours=24)},
                           self.config["jwt_secret"], algorithm="HS256")
        return {"token": token}

    def check_updates(self):
        result = self.updates.check()
        result.update(runtime=self.runtime_message, update=self.update_state)
        return result

    def update_status(self):
        return dict(self.update_state, runtime=self.runtime_message)

    def retry_runtime(self):
        if self.runtime_lock.acquire(blocking=False):
            threading.Thread(target=self._runtime_worker, daemon=True).start()
        return {"ok": True}

    def _runtime_worker(self):
        self.runtime_busy = True
        try:
            ensure_node(self.base, lambda message: setattr(self, "runtime_message", message))
            self.runtime_message = "Node.js 已就绪 · Python 与项目依赖已内置"
        except Exception as exc:
            self.runtime_message = str(exc)
            logging.exception("Node setup failed")
        finally:
            self.runtime_busy = False
            self.runtime_lock.release()

    def download_update(self, release_id):
        if not getattr(sys, "frozen", False):
            return {"error": "源码模式请使用 Releases 中的 EXE"}
        if not self.update_lock.acquire(blocking=False):
            return {"error": "更新下载正在进行"}
        self.update_state = {"busy": True, "message": "正在下载更新…", "ready": False}
        threading.Thread(target=self._download_worker, args=(release_id,), daemon=True).start()
        return {"ok": True}

    def _download_worker(self, release_id):
        try:
            self.update_file = self.updates.stage(release_id, lambda n, total: self.update_state.update(
                message=f"下载更新：{n / 1048576:.1f} / {total / 1048576:.1f} MB"))
            self.update_state.update(busy=False, ready=True, message="校验通过，点击重启安装")
        except Exception as exc:
            self.update_state.update(busy=False, ready=False, message=str(exc))
            logging.exception("Update download failed")
        finally:
            self.update_lock.release()

    def apply_update(self):
        if not self.update_file or not self.update_state["ready"]:
            return {"error": "请先完成下载和校验"}
        target = Path(sys.executable).resolve()
        # Preflight write permission before shutting down the current working client.
        probe = target.parent / f".wb-update-{os.getpid()}.tmp"
        try:
            probe.touch()
            probe.unlink()
            script = self.base / "updates" / "apply-update.ps1"
            script.write_text((ROOT / "desktop/apply-update.ps1").read_text(encoding="utf-8"),
                              encoding="utf-8-sig")
            # PyInstaller onefile uses a parent bootloader which owns the executable handle.
            parent_pid = int(os.environ.get("WB_BOOTLOADER_PID", os.getpid()))
            env = dict(os.environ, WB_UPDATE_SOURCE=str(self.update_file), WB_UPDATE_TARGET=str(target),
                       WB_UPDATE_PID=str(parent_pid), WB_UPDATE_LOG=str(self.base / "logs/update.log"))
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", str(script)], env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                             close_fds=True)
        except Exception as exc:
            return {"error": f"无法安装更新：{exc}"}
        threading.Thread(target=self.window.destroy, daemon=True).start()
        return {"ok": True}

    def retry_start(self):
        if self.closing.is_set():
            return {"error": "客户端正在关闭"}
        if self.start_lock.acquire(blocking=False):
            threading.Thread(target=self._start_worker, daemon=True).start()
        return {"ok": True}

    def _start_worker(self):
        try:
            if self.closing.is_set():
                return
            self.failed = False
            self.message = "正在启动本地服务…"
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            self.port = select_port(self.config.get("port", 8790))
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(ROOT / "desktop_entry.py"))
            command += ["--backend", str(self.port)]
            nonce = secrets.token_urlsafe(32)
            env = dict(os.environ, WB_DESKTOP_PARENT=str(os.getpid()), WB_DESKTOP_BOOT=nonce)
            output = (self.base / "logs/backend.log").open("w", encoding="utf-8")
            try:
                self.process = subprocess.Popen(command, env=env, stdout=output, stderr=output,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
            finally:
                output.close()
            # Closing during Popen must not leave a backend started after stop() ran.
            if self.closing.is_set():
                self.stop()
                return
            deadline = time.monotonic() + 90
            while not self.closing.is_set() and time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("本地服务启动失败，详细原因见用户目录 logs/backend.log")
                try:
                    request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/app/desktop-ready",
                                                      headers={"X-Desktop-Boot": nonce})
                    with urllib.request.urlopen(request, timeout=1) as response:
                        if not json.load(response).get("ready"):
                            continue
                    save_port(self.base, self.config, self.port)
                    self.window.load_url(f"http://127.0.0.1:{self.port}/admin?desktop=1")
                    if os.getenv("WB_DESKTOP_SMOKE"):
                        threading.Thread(target=self._smoke_test, daemon=True).start()
                    return
                except OSError:
                    self.closing.wait(.15)
            if not self.closing.is_set():
                raise RuntimeError("本地服务启动超时，可以点击重试")
        except Exception as exc:
            self.message = str(exc)
            self.failed = True
            logging.exception("Backend startup failed")
            try:
                self.window.evaluate_js("document.getElementById('status').textContent=" + json.dumps(self.message) +
                                        ";document.getElementById('bar').style.display='none'")
            except Exception:
                pass
        finally:
            self.start_lock.release()

    def _smoke_test(self):
        result = {"ok": False, "version": VERSION, "port": self.port}
        try:
            for _ in range(60):
                if self.closing.wait(.5):
                    break
                ready = self.window.evaluate_js("document.getElementById('app') && "
                    "!document.getElementById('app').classList.contains('hidden') && "
                    "document.getElementById('appVersion').textContent.includes('v" + VERSION + "')")
                if ready:
                    result.update(ok=True, title=self.window.evaluate_js("document.title"),
                                  footer=self.window.evaluate_js("document.getElementById('appVersion').textContent"))
                    break
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            atomic_json(Path(os.environ["WB_DESKTOP_SMOKE"]), result)
            self.window.destroy()

    def stop(self):
        self.closing.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def run():
    if os.name != "nt":
        raise RuntimeError("桌面 EXE 支持 Windows 10/11 x64")
    import msvcrt
    base = data_dir()
    # Use a per-user lock rather than a PID file (stale PID files cannot block startup).
    lock = (base / "desktop.lock").open("a+b")
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        ctypes.windll.user32.MessageBoxW(None, "WorkBuddy2API 已在运行，请查看已打开的窗口。", "WorkBuddy2API", 64)
        lock.close()
        return
    api = None
    try:
        config = configure_environment(base)
        setup_logging(base)
        ensure_webview2(base)
        import webview
        api = DesktopAPI(base, config)
        window = webview.create_window(f"WorkBuddy2API · v{VERSION}", html=SPLASH, js_api=Bridge(api),
                                      width=1280, height=860, min_size=(900, 600),
                                      background_color="#f5f6f8", text_select=True)
        api.window = window
        window.events.closed += api.stop
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.start(lambda: (api.retry_runtime(), api.retry_start()), gui="edgechromium",
                      private_mode=False, storage_path=str(base / "webview"),
                      icon=str(ROOT / "desktop/assets/app.ico"))
    except Exception as exc:
        logging.exception("Desktop failed")
        ctypes.windll.user32.MessageBoxW(None, str(exc) + f"\n\n数据和日志：{base}", "WorkBuddy2API", 16)
    finally:
        if api:
            api.stop()
        lock.close()
