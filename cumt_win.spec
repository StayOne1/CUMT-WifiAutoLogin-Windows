# -*- mode: python ; coding: utf-8 -*-
# CUMT 校园网自动登录 — Windows .exe 打包配置（onefile 单文件）
# 注意：本 spec 为 PyInstaller 6.x 语法；onefile 模式无 COLLECT，
#       全部依赖（Python/Qt DLL 等）打包进单个 exe，启动时解压到临时目录。
import os

icon_src = os.path.join('assets', 'app.ico')   # exe 文件图标（scripts/gen_icon.py 生成）

a = Analysis(
    ['win_login_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # 打包 app.ico，供运行时 setWindowIcon（任务栏/窗口图标）使用
        ('assets/app.ico', 'assets'),
    ],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngine',
        'PySide6.QtMultimedia',
        'PySide6.Qt3DCore',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'tkinter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,                 # onefile：依赖全部并入 EXE
    a.datas,
    [],
    name='CUMT校园网登录',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                   # 未安装 UPX 时自动跳过，不会失败
    upx_exclude=[],
    runtime_tmpdir=None,        # 默认解压到 %TEMP%
    console=False,              # 不显示终端窗口
    icon=icon_src if os.path.exists(icon_src) else None,
)
