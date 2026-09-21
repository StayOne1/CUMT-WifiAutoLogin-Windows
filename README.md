<h1 align="center"> CUMT（矿大）中国矿业大学校园网自动登录 Windows 版</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2B-0078D4?logo=windows&logoColor=white" />
  <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/PySide6-GUI-41CD52?logo=qt" />
  <img src="https://img.shields.io/badge/uv-环境管理-DE5FE9" />
  <img src="https://img.shields.io/github/license/sjwty/CUMT-Campus-Login-Mac" />
</p>

<p align="center">
  这是一个为中国矿业大学（CUMT）学生开发的校园网自动登录工具（Windows 托盘版）。它能够帮助您快速、方便地登录校园网，支持多种运营商，并提供自动登录功能。基于 <a href="https://github.com/sjwty/CUMT-Campus-Login-Mac">sjwty/CUMT-Campus-Login-Mac</a> 移植，支持后台常驻、定时检测、断线自动重连。
</p>

---

##  快速开始

```bash
# 1. 安装 uv（已安装可跳过）
winget install astral-sh.uv

# 2. 同步依赖（自动创建虚拟环境，必要时自动下载 Python）
uv sync

# 3. 运行
uv run win_login_app.py
```

或直接双击 `run_win.bat`（自动完成依赖同步并启动）。

### 自行打包 exe

```bash
uv add --dev pyinstaller
uv run python -m PyInstaller cumt_win.spec --noconfirm

# 产物：dist/CUMT校园网登录.exe（单文件，依赖已全部打包，双击即用）
```

---

##  功能特性

| 功能 | 说明 |
|------|------|
|  **托盘常驻** | 不占任务栏，只在右下角托盘显示状态图标，左键可弹出菜单 |
|  **定时自动检测** | 每隔 N 分钟检测登录状态（默认 5 分钟，可调） |
|  **断线自动重连** | 检测到掉线立即重新登录，系统通知告知结果 |
|  **Wi-Fi 自动连接** | 离线时自动搜索并连接校园 Wi-Fi CUMT_Stu（信号范围内自动切换/回连，无需管理员） |
|  **备用 Wi-Fi 故障转移** | 设置页可扫描/手输入备用 SSID 并保存密码；CUMT_Stu 连不上或认证失败时自动切换，备用可用期间不主动切回 |
|  **多运营商** | 校园网 / 中国电信 / 中国移动 / 中国联通 |
|  **可视化设置** | 随时修改账号、密码、检测间隔 |
|  **开机自启** | 勾选后写入注册表（HKCU Run 键），开机静默运行，无需手动打开 |

**托盘图标颜色：**

| 🟢 绿色 = 已登录 | 🔴 红色 = 未登录 | 🟡 黄色 = 检测/登录中 |

---

##  项目结构

| 文件 | 说明 |
|------|------|
| `win_login_app.py` | 全部核心逻辑（托盘 UI + 网络 + 配置） |
| `run_win.bat` | 一键运行脚本（uv 同步依赖 + 启动） |
| `pyproject.toml` / `uv.lock` | uv 依赖管理 |
| `cumt_win.spec` | PyInstaller 打包配置（onedir exe） |
| `使用说明_win.txt` | 面向用户的详细说明 |
| [docs/认证协议分析](docs/CUMT校园网认证协议分析.md) | 校园网认证协议技术文档 |

---

##  配置文件位置

| 类型 | 路径 |
|------|------|
| 账号设置 | `%APPDATA%\CUMTAutoLogin\settings.json` |
| 错误日志 | `%APPDATA%\CUMTAutoLogin\error.log` |

---

##  注意事项

- 需连接矿大校园网（`10.2.5.251` 可达）才能登录
- 密码（校园网及备用 Wi-Fi）以明文 JSON 存储在本地，请注意安全
- 备用 Wi-Fi 不支持隐藏 SSID（连接前须能在扫描结果中出现）
- 仅供个人便捷上网，请遵守学校网络规定

---

##  License

MIT License · 基于 [MuQY1818/CUMT_Net_Auto_Login](https://github.com/MuQY1818/CUMT_Net_Auto_Login) · 由 [sjwty/CUMT-Campus-Login-Mac](https://github.com/sjwty/CUMT-Campus-Login-Mac)（macOS 版）移植
