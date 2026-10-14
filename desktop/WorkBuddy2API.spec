# Run through scripts/build_desktop.py; only explicitly listed product assets are included.
from pathlib import Path
import json
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
version = json.loads((root / 'desktop/version.json').read_text(encoding='utf-8'))['version']
name = f'WorkBuddy2API-{version}-windows-x64'
datas = [(str(root / 'admin/static'), 'admin/static'),
         (str(root / 'turing_helper.js'), '.'),
         (str(root / 'desktop/version.json'), 'desktop'),
         (str(root / 'desktop/apply-update.ps1'), 'desktop'),
         (str(root / 'desktop/assets/app.ico'), 'desktop/assets'),
         (str(root / 'desktop/assets/app.svg'), 'desktop/assets'),
         (str(root / 'LICENSE'), '.')]
datas += collect_data_files('webview')
hidden = collect_submodules('admin') + collect_submodules('desktop')
hidden += ['uvicorn.logging', 'uvicorn.loops.asyncio', 'uvicorn.protocols.http.h11_impl',
           'uvicorn.lifespan.on', 'sqlalchemy.dialects.sqlite', 'sqlalchemy.dialects.mysql.pymysql',
           'converter', 'anthropic_adapter', 'responses_adapter', 'responses_projection',
           'desensitize', 'upstream_compat', 'wb_install', 'wb_asar', 'wb_envcheck',
           'webview.platforms.edgechromium', 'webview.platforms.winforms', 'clr', 'pythonnet']
a = Analysis([str(root / 'desktop_entry.py')], pathex=[str(root)], datas=datas, hiddenimports=hidden,
             excludes=['tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3',
                       'cairosvg', 'resvg_py', 'PIL', 'numpy', 'scipy', 'matplotlib', 'pytest',
                       'IPython', 'watchfiles', 'websockets', 'httptools',
                       # 排除 setuptools / pkg_resources：
                       # proxy_tools（pywebview 的依赖）会把它带进来，触发 PyInstaller 的
                       # `pyi_rth_pkgres` 运行时钩子，而该钩子在 `jaraco.text` 里
                       # `import jaraco.context` → 需要 `backports` 命名空间包，
                       # 打出来的 EXE 于是**双击即崩**：
                       #   ModuleNotFoundError: No module named 'backports'
                       # 本项目从不使用 pkg_resources（仅构建期工具依赖），
                       # 因此直接排除，连带去掉那个运行时钩子。
                       'setuptools', 'pkg_resources', '_distutils_hack'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=name, debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'desktop/assets/app.ico'), version=str(root / 'build/version-resource.txt'))
