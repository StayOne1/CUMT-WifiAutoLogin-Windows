"""
CUMT 校园网自动登录 — Windows 托盘版 v1.1.1
由 macOS 版 v2.1.1 移植，网络逻辑与 Mac 版完全一致。
差异：
  · 配置路径改为 %APPDATA% 下的 CUMTAutoLogin 目录
  · 托盘左键手动弹出菜单；UI 字体/UA 适配 Windows
  · 开机自启：写入 HKCU 注册表 Run 键（无需管理员权限）
  · Python 3.9 兼容；Signal 驱动 worker，跨线程安全
v1.1.1 修复：
  · 在线检测改为端到端外网探针，不再信任 Portal 状态页——
    修复开机自启后"假绿灯"（显示已登录但外网不通）且不自动重连
  · 启动阶段增加快速重试（500ms/5s/20s/60s），应对开机网络未就绪
"""

from __future__ import annotations

import sys
import os
import time
import json
import re
import ctypes
import traceback
import winreg
from typing import Optional

import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QCheckBox, QMessageBox,
    QSystemTrayIcon, QMenu, QSpinBox,
)
from PySide6.QtCore  import Qt, QTimer, QSize, QRect, QPoint, Signal, QObject, QThread
from PySide6.QtGui   import QIcon, QPainter, QColor, QFont, QPixmap, QAction, QCursor

# ──────────────────────────────────────────────
#  常量
# ──────────────────────────────────────────────
CURRENT_VERSION        = "v1.1.1-win"
_APPDATA               = os.environ.get("APPDATA") or os.path.expanduser("~")
CONFIG_PATH            = os.path.join(_APPDATA, "CUMTAutoLogin", "settings.json")
DEFAULT_CHECK_INTERVAL = 5   # 分钟
PORTAL_HOST            = "10.2.5.251"
PORTAL_URL             = f"http://{PORTAL_HOST}/"
WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "Referer": PORTAL_URL,
    "User-Agent": WINDOWS_UA,
}
OPERATOR_SUFFIX = {
    "校园网": "@xyw", "中国电信": "@telecom",
    "中国移动": "@cmcc", "中国联通": "@unicom",
}

# ──────────────────────────────────────────────
#  图标
# ──────────────────────────────────────────────
def _dot_icon(color: str) -> QIcon:
    """必须在 QApplication 创建之后调用"""
    px = QPixmap(22, 22)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(3, 3, 16, 16)
    p.end()
    return QIcon(px)


# 注意：ICON_* 不在模块顶层创建，必须在 QApplication 实例化后才能创建 QPixmap
ICON_ONLINE:  Optional[QIcon] = None
ICON_OFFLINE: Optional[QIcon] = None
ICON_BUSY:    Optional[QIcon] = None

# ──────────────────────────────────────────────
#  Windows 开机自启（HKCU 注册表 Run 键，无需管理员权限）
# ──────────────────────────────────────────────
RUN_KEY_PATH   = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "CUMT校园网登录"

def _autostart_command() -> str:
    """自启命令：打包后指向 exe；源码运行指向 venv 的 pythonw.exe（无控制台窗口）"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable          # 兜底：无 pythonw 时用 python.exe
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'

def set_auto_start(enable: bool) -> None:
    try:
        if enable:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                                    0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        pass                              # 取消自启时值本就不存在，视为成功

# ──────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────
DEFAULT_CFG: dict = {
    "username": "", "password": "", "operator": "校园网",
    "autostart": False, "auto_login": True,
    "check_interval": DEFAULT_CHECK_INTERVAL,
}

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return {**DEFAULT_CFG, **json.load(f)}
        except Exception:
            pass
    return dict(DEFAULT_CFG)

def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ──────────────────────────────────────────────
#  网络 Worker（在独立 QThread 中运行）
#  重要：所有方法通过 Signal → Slot 机制调用，不跨线程直接调用
# ──────────────────────────────────────────────
class NetWorker(QObject):
    # 上行 Signal（主线程 → Worker）
    sig_check  = Signal()
    sig_login  = Signal(str, str, str)   # username, password, operator
    sig_logout = Signal()

    # 下行 Signal（Worker → 主线程）
    status = Signal(bool)        # True=已登录
    result = Signal(str, str)    # (action, message)

    def __init__(self) -> None:
        super().__init__()
        self.session = requests.Session()
        # 连接上行信号到对应槽（在 worker 所在线程执行）
        self.sig_check.connect(self._do_check)
        self.sig_login.connect(self._do_login)
        self.sig_logout.connect(self._do_logout)

    # ---------- 内部实现（运行在 worker 线程）----------
    # 外网探针：只有拿到真实的端到端外网响应才认定在线。
    # 不再把 Portal 页面的"注销/已登录"字样当作依据——eportal 状态与网关
    # 放行可能脱同步（典型：开机后残留会话页，外网不通、认证后台无设备），
    # 旧逻辑据此误报"已登录"且不再自动重连。登录接口幂等，误判为未登录
    # 至多多登录一次，无害；误判为已登录则会假绿卡死。
    _ONLINE_PROBES = (
        # (url, 期望状态码, 响应必须包含的关键字; 关键字 None 表示只看状态码)
        ("http://connect.rom.miui.com/generate_204", 204, None),
        ("http://www.baidu.com", 200, "百度一下"),
    )

    def _is_logged_in(self) -> bool:
        for url, want_code, keyword in self._ONLINE_PROBES:
            try:
                r = self.session.get(url, timeout=3)
                if r.status_code != want_code or PORTAL_HOST in r.url:
                    continue          # 被重定向/劫持到 Portal 一律视为不在线
                if keyword is None or keyword in r.text:
                    return True
            except Exception:
                pass
        return False

    def _do_check(self) -> None:
        logged = self._is_logged_in()
        self.status.emit(logged)

    def _do_login(self, username: str, password: str, operator: str) -> None:
        full = username + OPERATOR_SUFFIX.get(operator, "@xyw")
        ts   = int(time.time() * 1000)
        cb   = f"dr{ts}"
        url  = (
            f"http://{PORTAL_HOST}:801/eportal/?c=Portal&a=login"
            f"&callback={cb}&login_method=1"
            f"&user_account={full}&user_password={password}"
            f"&wlan_user_ip=&wlan_user_mac=000000000000"
            f"&wlan_ac_ip=&wlan_ac_name=&jsVersion=3.0&_={ts}"
        )
        hdr = {**BASE_HEADERS, "Host": f"{PORTAL_HOST}:801"}
        login_success = False
        try:
            r = self.session.get(url, headers=hdr, timeout=8)
            r.raise_for_status()
            raw  = r.text
            js   = raw[raw.index("(") + 1 : raw.rindex(")")]
            d    = json.loads(js)
            res  = d.get("result", "")
            code = d.get("ret_code", "")
            msg  = d.get("msg", "")

            if res == "1":
                self.result.emit("login", "success")
                login_success = True
            elif res == "0":
                if code == "1":
                    self.result.emit("login", "wrong_pwd")
                elif code == "2" or "在线数量超过限制" in msg:
                    self.result.emit("login", "already")
                    login_success = True
                else:
                    self.result.emit("login", f"fail:{msg}")
            else:
                self.result.emit("login", f"fail:{msg}")
        except Exception as e:
            self.result.emit("login", f"error:{e}")

        # 如果接口明确返回登录成功，我们直接广播 True 状态，而无需再次请求服务器，避免潜在的响应状态同步延迟
        if login_success:
            self.status.emit(True)
        else:
            self.status.emit(self._is_logged_in())

    def _do_logout(self) -> None:
        if not self._is_logged_in():
            self.result.emit("logout", "not_logged_in")
            return
        # 获取 IP / MAC
        ip = mac = ""
        try:
            r = self.session.get(PORTAL_URL, timeout=5)
            m = re.search(r"user_ip\s*=\s*['\"](.+?)['\"]", r.text)
            if m: ip = m.group(1)
            m = re.search(r"user_mac\s*=\s*['\"](.+?)['\"]", r.text)
            if m: mac = m.group(1)
        except Exception:
            pass

        ts  = int(time.time() * 1000)
        cb  = f"dr{ts}"
        url = (
            f"http://{PORTAL_HOST}:801/eportal/?c=Portal&a=logout"
            f"&callback={cb}&login_method=1"
            f"&user_account=drcom&user_password=123&ac_logout=0"
            f"&wlan_user_ip={ip}&wlan_user_ipv6=&wlan_vlan_id=1"
            f"&wlan_user_mac={mac}&wlan_ac_ip=&wlan_ac_name="
            f"&jsVersion=3.0&_={ts - 22}"
        )
        hdr = {**BASE_HEADERS, "Host": f"{PORTAL_HOST}:801"}
        logout_success = False
        try:
            r   = self.session.get(url, headers=hdr, timeout=8)
            raw = r.text
            js  = raw[raw.index("(") + 1 : raw.rindex(")")]
            d   = json.loads(js)
            if d.get("result") == "1":
                self.session = requests.Session()
                self.result.emit("logout", "success")
                logout_success = True
            else:
                self.result.emit("logout", f"fail:{d.get('msg','')}")
        except Exception as e:
            self.result.emit("logout", f"error:{e}")

        # 注销成功后直接广播 False 状态，避免因服务器会话注销同步延迟导致状态瞬间被拉回绿灯
        if logout_success:
            self.status.emit(False)
        else:
            self.status.emit(self._is_logged_in())

# ──────────────────────────────────────────────
#  自定义复选框
# ──────────────────────────────────────────────
class CustomCheckBox(QCheckBox):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        f = QFont(); f.setPixelSize(13)
        painter.setFont(f)
        painter.setPen(QColor("#333"))
        painter.drawText(28, self.height() // 2 + 5, self.text())
        sz   = 18
        rect = QRect(0, (self.height() - sz) // 2, sz, sz)
        painter.setBrush(QColor("#4CAF50" if self.isChecked() else "#fff"))
        painter.setPen(QColor("#4CAF50" if self.isChecked() else "#ccc"))
        painter.drawRoundedRect(rect, 4, 4)
        if self.isChecked():
            painter.setPen(QColor("#fff"))
            bf = QFont(); bf.setPixelSize(12); bf.setBold(True)
            painter.setFont(bf)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "✓")

    def sizeHint(self) -> QSize:
        return QSize(120, 30)

# ──────────────────────────────────────────────
#  设置窗口
# ──────────────────────────────────────────────
class SettingsWindow(QMainWindow):
    save_requested = Signal(dict)

    def __init__(self, cfg: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CUMT 校园网登录 — 设置")
        self.setFixedSize(400, 430)
        self.setWindowFlags(Qt.WindowType.Window)

        root = QWidget()
        self.setCentralWidget(root)
        root.setStyleSheet("background:#f5f5f7; font-size:13px;")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        title = QLabel("⚙️  账号设置")
        title.setStyleSheet("font-size:18px; font-weight:bold; color:#1d1d1f;")
        lay.addWidget(title)

        card = QFrame()
        card.setStyleSheet("QFrame{background:white;border-radius:12px;}")
        cl = QVBoxLayout(card)
        cl.setSpacing(12); cl.setContentsMargins(16, 16, 16, 16)

        fstyle = (
            "QLineEdit,QComboBox{border:1px solid #ddd;border-radius:7px;"
            "padding:7px 10px;background:#fafafa;font-size:13px;}"
            "QLineEdit:focus,QComboBox:focus{border-color:#4CAF50;}"
        )
        spstyle = (
            "QSpinBox{border:1px solid #ddd;border-radius:7px;"
            "padding:7px 10px;background:#fafafa;font-size:13px;}"
        )

        def _row(label: str, widget: QWidget) -> None:
            r = QHBoxLayout()
            lb = QLabel(label); lb.setFixedWidth(60); lb.setStyleSheet("color:#555;")
            r.addWidget(lb); r.addWidget(widget)
            cl.addLayout(r)

        self.id_edit = QLineEdit(cfg.get("username", ""))
        self.id_edit.setPlaceholderText("请输入学号")
        self.id_edit.setStyleSheet(fstyle)
        _row("学号", self.id_edit)

        self.pw_edit = QLineEdit(cfg.get("password", ""))
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText("请输入密码")
        self.pw_edit.setStyleSheet(fstyle)
        _row("密码", self.pw_edit)

        self.op_box = QComboBox()
        self.op_box.addItems(["校园网", "中国电信", "中国移动", "中国联通"])
        self.op_box.setCurrentText(cfg.get("operator", "校园网"))
        self.op_box.setStyleSheet(fstyle)
        _row("运营商", self.op_box)

        self.spin = QSpinBox()
        self.spin.setRange(1, 60)
        self.spin.setValue(cfg.get("check_interval", DEFAULT_CHECK_INTERVAL))
        self.spin.setSuffix(" 分钟")
        self.spin.setStyleSheet(spstyle)
        _row("检测间隔", self.spin)

        crow = QHBoxLayout()
        self.cb_autostart = CustomCheckBox("开机自启")
        self.cb_autostart.setChecked(cfg.get("autostart", False))
        self.cb_autologin = CustomCheckBox("自动登录")
        self.cb_autologin.setChecked(cfg.get("auto_login", True))
        crow.addWidget(self.cb_autostart)
        crow.addWidget(self.cb_autologin)
        crow.addStretch()
        cl.addLayout(crow)

        lay.addWidget(card)

        brow = QHBoxLayout(); brow.setSpacing(10)
        cancel = QPushButton("取消")
        cancel.setStyleSheet(
            "QPushButton{background:#e0e0e0;color:#333;border:none;border-radius:8px;"
            "padding:10px 20px;font-size:14px;}QPushButton:hover{background:#d0d0d0;}"
        )
        cancel.clicked.connect(self.hide)
        save = QPushButton("保存")
        save.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;border:none;border-radius:8px;"
            "padding:10px 20px;font-size:14px;font-weight:bold;}"
            "QPushButton:hover{background:#43A047;}"
        )
        save.clicked.connect(self._save)
        brow.addStretch(); brow.addWidget(cancel); brow.addWidget(save)
        lay.addLayout(brow)

        ver = QLabel(f"Windows 版  {CURRENT_VERSION}")
        ver.setStyleSheet("color:#bbb;font-size:11px;")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(ver)

    def _save(self) -> None:
        self.save_requested.emit({
            "username":       self.id_edit.text().strip(),
            "password":       self.pw_edit.text(),
            "operator":       self.op_box.currentText(),
            "autostart":      self.cb_autostart.isChecked(),
            "auto_login":     self.cb_autologin.isChecked(),
            "check_interval": self.spin.value(),
        })
        self.hide()

# ──────────────────────────────────────────────
#  主应用
# ──────────────────────────────────────────────
class CUMTApp(QApplication):
    def __init__(self, argv: list) -> None:
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)

        # QPixmap 必须在 QApplication 创建之后才能初始化
        global ICON_ONLINE, ICON_OFFLINE, ICON_BUSY
        ICON_ONLINE  = _dot_icon("#4CAF50")
        ICON_OFFLINE = _dot_icon("#F44336")
        ICON_BUSY    = _dot_icon("#FFC107")

        self.cfg = load_config()
        self.logged_in = False

        # Worker 线程
        self._thread = QThread()
        self._worker = NetWorker()
        self._worker.moveToThread(self._thread)
        self._worker.status.connect(self._on_status)
        self._worker.result.connect(self._on_result)
        self._thread.start()

        # 系统托盘
        self.tray = QSystemTrayIcon(ICON_BUSY, self)
        self.tray.setToolTip("CUMT 校园网登录")
        self._build_menu()
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        # 设置窗口（延迟创建）
        self._settings_win: Optional[SettingsWindow] = None

        # 定时检测
        self._check_timer = QTimer(self)
        self._check_timer.timeout.connect(self._timer_check)
        self._restart_timer()

        # 启动阶段快速探测几轮：开机自启时无线连接/网络栈可能尚未就绪，
        # 且此时 Portal 残留状态最易失真；快速重试以尽早完成真实认证
        for _delay in (500, 5000, 20000, 60000):
            QTimer.singleShot(_delay, self._timer_check)

    # ── 菜单 ──────────────────────────────────
    def _build_menu(self) -> None:
        # 保留实例引用：Windows 上左键需手动 popup
        menu = self._menu = QMenu()
        menu.setStyleSheet(
            "QMenu{background:#fff;border:1px solid #ddd;border-radius:8px;"
            "padding:4px;font-size:13px;}"
            "QMenu::item{padding:8px 20px;border-radius:5px;}"
            "QMenu::item:selected{background:#f0f0f0;}"
            "QMenu::separator{height:1px;background:#eee;margin:4px 8px;}"
        )
        self.act_status = QAction("⏳ 正在检测...", self)
        self.act_status.setEnabled(False)
        menu.addAction(self.act_status)
        menu.addSeparator()

        self.act_login = QAction("🔗 立即登录", self)
        self.act_login.triggered.connect(self._manual_login)
        menu.addAction(self.act_login)

        act_logout = QAction("🔌 注销", self)
        act_logout.triggered.connect(self._manual_logout)
        menu.addAction(act_logout)

        menu.addSeparator()
        act_settings = QAction("⚙️  设置...", self)
        act_settings.triggered.connect(self._open_settings)
        menu.addAction(act_settings)
        menu.addSeparator()

        act_quit = QAction("✕ 退出", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)

    # ── 定时器 ────────────────────────────────
    def _restart_timer(self) -> None:
        ms = self.cfg.get("check_interval", DEFAULT_CHECK_INTERVAL) * 60 * 1000
        self._check_timer.start(ms)

    def _timer_check(self) -> None:
        """定时触发：先检测状态，若未登录且开启自动登录则自动登录"""
        self.tray.setIcon(ICON_BUSY)
        self.act_status.setText("⏳ 正在检测...")

        # 先发起一次 check；_on_status 收到结果后决定是否登录
        self._pending_auto_login = True
        self._worker.sig_check.emit()

    # ── 手动操作 ──────────────────────────────
    def _manual_login(self) -> None:
        u = self.cfg.get("username", "")
        p = self.cfg.get("password", "")
        if not u or not p:
            self.tray.showMessage(
                "CUMT 校园网", "请先在设置中填写学号和密码",
                QSystemTrayIcon.MessageIcon.Warning, 3000,
            )
            self._open_settings()
            return
        self.tray.setIcon(ICON_BUSY)
        self.act_status.setText("⏳ 正在登录...")
        self._pending_auto_login = False
        self._worker.sig_login.emit(u, p, self.cfg.get("operator", "校园网"))

    def _manual_logout(self) -> None:
        self.tray.setIcon(ICON_BUSY)
        self.act_status.setText("⏳ 正在注销...")
        self._pending_auto_login = False
        self._worker.sig_logout.emit()

    # ── 信号处理（主线程）────────────────────
    def _on_status(self, logged_in: bool) -> None:
        self.logged_in = logged_in
        if logged_in:
            self.tray.setIcon(ICON_ONLINE)
            self.tray.setToolTip("CUMT 校园网 — 已登录 ✅")
            self.act_status.setText("✅ 已登录校园网")
            self.act_login.setText("🔗 重新登录")
        else:
            self.tray.setIcon(ICON_OFFLINE)
            self.tray.setToolTip("CUMT 校园网 — 未登录 ❌")
            self.act_status.setText("❌ 未登录")
            self.act_login.setText("🔗 立即登录")
            # 若未登录且允许自动登录，触发登录
            if getattr(self, "_pending_auto_login", False) and self.cfg.get("auto_login"):
                u = self.cfg.get("username", "")
                p = self.cfg.get("password", "")
                if u and p:
                    self._pending_auto_login = False
                    self._worker.sig_login.emit(u, p, self.cfg.get("operator", "校园网"))

    def _on_result(self, action: str, msg: str) -> None:
        if action == "login":
            if msg == "success":
                self.tray.showMessage(
                    "登录成功", "🎉 已成功登录校园网",
                    QSystemTrayIcon.MessageIcon.Information, 3000,
                )
            elif msg == "already":
                pass  # 已登录，静默
            elif msg == "wrong_pwd":
                self.tray.showMessage(
                    "登录失败", "账号或密码错误，请检查设置",
                    QSystemTrayIcon.MessageIcon.Critical, 5000,
                )
            else:
                detail = msg.split(":", 1)[-1]
                self.tray.showMessage(
                    "登录失败", f"错误：{detail}",
                    QSystemTrayIcon.MessageIcon.Warning, 4000,
                )
        elif action == "logout":
            if msg == "success":
                self.tray.showMessage(
                    "已注销", "成功退出校园网",
                    QSystemTrayIcon.MessageIcon.Information, 2000,
                )
            elif msg == "not_logged_in":
                self.tray.showMessage(
                    "提示", "您当前未登录",
                    QSystemTrayIcon.MessageIcon.Information, 2000,
                )
            elif msg.startswith("fail:") or msg.startswith("error:"):
                detail = msg.split(":", 1)[-1]
                self.tray.showMessage(
                    "注销失败", detail,
                    QSystemTrayIcon.MessageIcon.Warning, 3000,
                )

    # ── 设置窗口 ──────────────────────────────
    def _open_settings(self) -> None:
        if self._settings_win is None:
            self._settings_win = SettingsWindow(self.cfg)
            self._settings_win.save_requested.connect(self._on_save)
        else:
            # 同步最新配置到窗口
            self._settings_win.id_edit.setText(self.cfg.get("username", ""))
            self._settings_win.pw_edit.setText(self.cfg.get("password", ""))
            self._settings_win.op_box.setCurrentText(self.cfg.get("operator", "校园网"))
            self._settings_win.spin.setValue(
                self.cfg.get("check_interval", DEFAULT_CHECK_INTERVAL)
            )
            self._settings_win.cb_autostart.setChecked(self.cfg.get("autostart", False))
            self._settings_win.cb_autologin.setChecked(self.cfg.get("auto_login", True))

        self._settings_win.show()
        self._settings_win.raise_()
        self._settings_win.activateWindow()

    def _on_save(self, new_cfg: dict) -> None:
        self.cfg = {**self.cfg, **new_cfg}
        save_config(self.cfg)
        set_auto_start(self.cfg["autostart"])
        self._restart_timer()
        self.tray.showMessage(
            "设置已保存",
            f"检测间隔：每 {self.cfg['check_interval']} 分钟",
            QSystemTrayIcon.MessageIcon.Information, 2000,
        )

    # ── 托盘点击 ──────────────────────────────
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Windows：左键 Trigger 需手动弹出菜单；右键 Context 由
        # setContextMenu 自动弹出，勿重复处理（否则出现双菜单）
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self._menu.isVisible():
                self._menu.hide()   # 防连续点击出现双菜单
            self._menu.popup(QCursor.pos())

    # ── 退出 ──────────────────────────────────
    def _quit_app(self) -> None:
        self._check_timer.stop()
        self._thread.quit()
        self._thread.wait(2000)
        self.quit()

# ──────────────────────────────────────────────
#  入口
# ──────────────────────────────────────────────
def _install_excepthook() -> None:
    """窗口化运行（console=False）时未捕获异常无处输出：写日志 + 弹窗兜底"""
    def _hook(tp, val, tb) -> None:
        text = "".join(traceback.format_exception(tp, val, tb))
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(os.path.join(os.path.dirname(CONFIG_PATH), "error.log"),
                      "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        try:
            QMessageBox.critical(None, "程序错误", text[-1500:])
        except Exception:
            ctypes.windll.user32.MessageBoxW(
                None, text[-1500:], "CUMT校园网登录", 0x10)
    sys.excepthook = _hook


def _app_icon_path() -> str:
    """定位 app.ico：打包(onefile)时在解压资源目录，源码运行在仓库 assets/"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", "app.ico")


if __name__ == "__main__":
    _install_excepthook()
    app = CUMTApp(sys.argv)
    app.setApplicationName("CUMT校园网登录")
    app.setApplicationVersion(CURRENT_VERSION)
    app.setFont(QFont("Microsoft YaHei UI", 13))
    _icon_path = _app_icon_path()
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))   # 任务栏/窗口图标
    sys.exit(app.exec())
