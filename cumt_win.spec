# -*- mode: python ; coding: utf-8 -*-
# CUMT 校园网自动登录 — Windows .exe 打包配置（onedir）
# 注意：本 spec 为 PyInstaller 6.x 语法，与 cumt_mac.spec 的 5.x 语法不兼容
import os

icon_src = os.path.join('assets', 'app.ico')   # 仓库暂无此文件，exists 兜底为 None

a = Analysis(
    ['win_login_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
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
    [],
    exclude_binaries=True,      # onedir 关键项
    name='CUMT校园网登录',
    debug=False,
    strip=False,
    upx=True,                   # 未安装 UPX 时自动跳过，不会失败
    console=False,              # 不显示终端窗口
    icon=icon_src if os.path.exists(icon_src) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CUMT校园网登录',
)
