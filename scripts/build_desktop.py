"""Reproducible Windows x64 onefile packaging; never includes .env, logs or account data."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _check_interpreter_layout() -> None:
    """拒绝会在打包后崩溃的解释器布局。

    ⚠️ 这是一次真实踩坑的固化：conda 环境的 OpenSSL / SQLite 动态库放在
    `Library\\bin\\`（libssl-3-x64.dll / libcrypto-3-x64.dll / sqlite3.dll），
    而 PyInstaller 只扫描 `DLLs/` 与解释器根目录 —— 于是打出来的 EXE
    **构建成功、双击即崩**：

        ImportError: DLL load failed while importing _ssl: 找不到指定的程序。

    因为 uvicorn 会 import ssl，整个后端起不来、界面永远连不上。
    官方 CPython 把 OpenSSL 静态链进 `_ssl.pyd`，没有这个问题。

    判据：解释器目录下存在 `Library\\bin`（conda 布局），且 `DLLs\\_ssl.pyd`
    旁边没有可用的 libssl。命中就**直接失败并给出正确做法**，
    而不是产出一个坏 EXE。
    """
    base = Path(sys.base_prefix)
    conda_layout = (base / "Library" / "bin").is_dir()
    if not conda_layout:
        return
    # 官方 CPython 不会有 Library\bin；这里是 conda。
    has_split_ssl = ((base / "Library" / "bin" / "libssl-3-x64.dll").exists()
                     or (base / "Library" / "bin" / "libcrypto-3-x64.dll").exists())
    if not has_split_ssl:
        return
    raise SystemExit(
        "检测到 conda/mamba 解释器布局，直接打包会产出「双击即崩」的 EXE：\n"
        f"  解释器: {sys.executable}\n"
        f"  分离的动态库: {base / 'Library' / 'bin'}\n\n"
        "原因：conda 把 libssl / libcrypto / sqlite3 放在 Library\\bin，"
        "PyInstaller 不会扫描该目录，\n"
        "      于是 EXE 里缺少这些 DLL，运行时 import ssl 失败：\n"
        "      ImportError: DLL load failed while importing _ssl\n\n"
        "正确做法 —— 用**官方 CPython** 建虚拟环境再构建：\n"
        "  py -3.11 -m venv .desktop-build\n"
        "  .\\.desktop-build\\Scripts\\python.exe -m pip install -r requirements-desktop-lock.txt\n"
        "  .\\.desktop-build\\Scripts\\python.exe scripts\\build_desktop.py\n"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", default="dist", help="Output folder; useful while an older EXE is running")
    args = parser.parse_args()
    destination = (ROOT / args.dist_dir).resolve()
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SystemExit("Use Windows x64 with a 64-bit Python interpreter")
    if sys.maxsize <= 2**32:
        raise SystemExit("A 64-bit Python interpreter is required")
    _check_interpreter_layout()
    import resvg_py
    from PIL import Image
    version = json.loads((ROOT / "desktop/version.json").read_text(encoding="utf-8"))["version"]
    parts = tuple(int(v) for v in version.split("."))
    if len(parts) != 3:
        raise SystemExit("desktop/version.json must contain a stable x.y.z version")
    assets = ROOT / "desktop/assets"
    png = resvg_py.svg_to_bytes(svg_string=(assets / "app.svg").read_text(encoding="utf-8"),
                                width=256, height=256)
    (assets / "app.png").write_bytes(png)
    Image.open(assets / "app.png").save(assets / "app.ico", format="ICO",
                                       sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)])
    (ROOT / "build").mkdir(exist_ok=True)
    resource = f'''VSVersionInfo(
      ffi=FixedFileInfo(filevers={parts + (0,)}, prodvers={parts + (0,)},
          mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
      kids=[StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'WorkBuddy2API'),
        StringStruct('FileDescription', 'WorkBuddy2API Desktop'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('ProductName', 'WorkBuddy2API'),
        StringStruct('ProductVersion', '{version}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])'''
    (ROOT / "build/version-resource.txt").write_text(resource, encoding="utf-8")
    env = dict(os.environ, PYTHONUTF8="1")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(destination),
                    str(ROOT / "desktop/WorkBuddy2API.spec")], cwd=ROOT, env=env, check=True)
    executable = destination / f"WorkBuddy2API-{version}-windows-x64.exe"
    if not _verify_executable(executable):
        raise SystemExit(
            f"打包出的 EXE 无法自检通过：{executable}\n"
            "最常见原因是解释器布局导致缺少 SSL/SQLite 动态库（见上面的 conda 提示）。"
        )
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    executable.with_suffix(".exe.sha256").write_text(f"{digest}  {executable.name}\n", encoding="ascii")
    print(f"Built {executable} ({executable.stat().st_size / 1048576:.1f} MB)")


def _verify_executable(executable: Path) -> bool:
    """产物自检：**实际运行** EXE 的模块加载，而不是只看文件存不存在。

    为什么必须做：曾经出现过「构建 exit 0、产物 26.8MB、双击即崩」的情况
    （conda 布局缺 libssl）。只看退出码会把坏包发出去，所以这里真的把
    EXE 拉起来、让它 import 关键模块并回写 JSON，然后再判定成败。

    用 `--smoke-test` 之外的单进程方式：EXE 支持 `--backend <port>`，
    我们起它并轮询就绪端点，能做到端到端验证。
    """
    import tempfile
    import time
    import urllib.request

    port = 18791
    data_dir = Path(tempfile.mkdtemp(prefix="wb2api-verify-"))
    log_path = data_dir / "selfcheck.log"
    env = dict(os.environ, WORKBUDDY_DESKTOP_DATA=str(data_dir),
               PYTHONUTF8="1")
    # ⚠️ 输出**重定向到文件**，不要用 PIPE。
    # onefile 的 EXE 会派生子进程（PyInstaller bootloader → 真正的后端），
    # `terminate()` 只杀得掉父进程，子进程仍持有管道写端；
    # 此时 `proc.stdout.read()` 会**永久阻塞**（自检卡死、构建永不结束）。
    # 写文件既没有这个死锁，也便于失败时回看完整日志。
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_fh:
        proc = subprocess.Popen([str(executable), "--backend", str(port)],
                                env=env, stdout=log_fh,
                                stderr=subprocess.STDOUT)
        ready = False
        output = ""
        try:
            # 探针必须选**不设鉴权/nonce 门槛**的端点，否则自检会永远失败：
            #  `/api/app/desktop-ready` 要求 `X-Desktop-Boot` 匹配 WB_DESKTOP_BOOT，
            #  裸探测只会拿到 404 —— 那样即使 EXE 完全正常也会被判为坏包。
            #  这里探 `/api/app/version`（路由层就绪）与 `/admin`（静态资源可用）。
            deadline = time.time() + 60
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/api/app/version", timeout=2) as r:
                        if r.status == 200 and b"version" in r.read():
                            with urllib.request.urlopen(
                                    f"http://127.0.0.1:{port}/admin", timeout=5) as r2:
                                if r2.status == 200 and len(r2.read()) > 1000:
                                    ready = True
                                    break
                except Exception:
                    time.sleep(1.0)
        finally:
            _kill_tree(proc)
            try:
                output = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                output = ""
            shutil.rmtree(data_dir, ignore_errors=True)

    if ready:
        print("Self-check: EXE 启动成功，后端与静态资源均可用")
        return True
    print("Self-check FAILED: EXE 未能启动。输出如下：")
    for line in (output or "").splitlines()[-25:]:
        print("   ", line)
    return False


def _kill_tree(proc) -> None:
    """结束进程**及其子进程**（onefile EXE 会有 bootloader 子进程）。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    main()
