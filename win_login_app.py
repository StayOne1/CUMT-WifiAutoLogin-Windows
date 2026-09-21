"""
CUMT 校园网自动登录 — Windows 托盘版 v1.3.0
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
v1.2.0 新增：
  · 离线时自动搜索并连接校园 Wi-Fi CUMT_Stu（netsh 实现，
    范围内自动切换/回连，无配置时自动创建开放网络配置文件）
v1.3.0 新增：
  · 备用 Wi-Fi 故障转移：设置页可扫描/手输备用 SSID 并保存密码，
    CUMT_Stu 关联失败或认证后仍无外网时自动切换到备用网络；
    惰性驻留——备用可用期间不主动切回校园网，断网才重走完整流程
"""

from __future__ import annotations

import sys
import os
import time
import json
import re
import ctypes
import traceback
import subprocess
import tempfile
import winreg
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

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
CURRENT_VERSION        = "v1.3.0-win"
_APPDATA               = os.environ.get("APPDATA") or os.path.expanduser("~")
CONFIG_PATH            = os.path.join(_APPDATA, "CUMTAutoLogin", "settings.json")
DEFAULT_CHECK_INTERVAL = 5   # 分钟
CAMPUS_SSID            = "CUMT_Stu"   # 校园 Wi-Fi，离线时自动连接
WIFI_CONNECT_WAIT      = 15    # 秒，发起连接后等待关联完成的最长时间
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
    "backup_ssid": "", "backup_password": "",
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
#  WLAN 管理（netsh 文本解析为纯函数，便于单元测试）
#  说明：netsh 的字段标签随系统语言本地化，但 SSID/Profile 名等
#  匹配目标均为 ASCII；解析只依赖 ASCII 字样与"SSID"/"所有用户
#  配置文件"/"身份验证"等字段写法，中英文系统均可用。
# ──────────────────────────────────────────────
def _run_netsh(args: list) -> str:
    """执行 netsh wlan 子命令并返回 stdout 文本，失败返回空串。

    CREATE_NO_WINDOW：打包成 exe 后调用 netsh 不会闪黑色控制台窗口。
    输出按字节接收、依次尝试 utf-8/gbk 解码——解析目标均为 ASCII，
    个别字符解码失败不影响匹配。
    """
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.run(["netsh", "wlan", *args],
                           capture_output=True, timeout=10, creationflags=flags)
        out = p.stdout or b""
        for enc in ("utf-8", "gbk"):
            try:
                return out.decode(enc)
            except UnicodeDecodeError:
                continue
        return out.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_wireless_ssid(netsh_interfaces_text: str) -> Optional[str]:
    """从 `netsh wlan show interfaces` 输出解析当前连接的 SSID，未连接返回 None。

    空白匹配用 [ \\t] 而非 \\s——\\s 会吞换行，导致断开状态下
    "SSID :"（冒号后为空）跨行捕获下一行 BSSID 的值。
    """
    m = re.search(r"^[ \t]*SSID[ \t]*:[ \t]*(\S.*)$", netsh_interfaces_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_profile_names(netsh_profiles_text: str) -> list:
    """从 `netsh wlan show profiles` 输出解析全部配置文件名（中英文系统）"""
    names = []
    for m in re.finditer(r"(?:All User Profile|所有用户配置文件)\s*:\s*(\S.*)$",
                         netsh_profiles_text, re.MULTILINE):
        names.append(m.group(1).strip())
    return names


def _parse_scan_networks(netsh_networks_text: str) -> list:
    """解析 `netsh wlan show networks` 扫描结果，返回 [(ssid, auth), ...]。

    头行形如 "SSID 3 : 名称"——行首锚定 + 编号，BSSID 行不会误匹配；
    auth 取该 SSID 块内的"身份验证/身份鉴别/Authentication"行，缺省空串。
    供精确匹配（子串匹配会让 CUMT_Stu_5G 误命中 CUMT_Stu）。
    """
    auth_re = re.compile(r"(?:身份验证|身份鉴别|Authentication)[ \t]*:[ \t]*(.+)$",
                         re.MULTILINE)
    heads = list(re.finditer(r"^[ \t]*SSID[ \t]+\d+[ \t]*:[ \t]*(.*)$",
                             netsh_networks_text, re.MULTILINE))
    nets = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(netsh_networks_text)
        am = auth_re.search(netsh_networks_text, m.end(), end)
        nets.append((m.group(1).strip(), am.group(1).strip() if am else ""))
    return nets


def _auth_kind(auth_str: str) -> str:
    """netsh 身份验证字段 → open / psk2 / sae3 / enterprise（中英文系统）。

    - WPA2/WPA3 过渡网络（"WPA2/WPA3-个人"）归 psk2：WPA2PSK profile 可连
    - 中文系统的 WPA3 显示为"WPA3-个人"，不含 SAE 字样，判据须按版本号
    - 企业级为 802.1X，密码 profile 连不上，调用方需拒绝
    """
    s = (auth_str or "").lower()
    if not s or "开放" in s or "open" in s:
        return "open"
    if "企业" in s or "enterprise" in s:
        return "enterprise"
    if "wpa3" in s and "wpa2" not in s:
        return "sae3"
    return "psk2"


def _wlan_profile_xml(ssid: str, password: Optional[str] = None,
                      sae: bool = False) -> str:
    """WLAN 配置文件 XML：无密码为开放网络（认证全走 Portal），
    有密码为 WPA2PSK/WPA3SAE 个人级 AES。

    SSID 与密码均做 XML 转义（防 AT&T 之类名称拼出非法 XML）；
    声明带 UTF-8 以支持中文 SSID（临时文件按 UTF-8 无 BOM 写出）。
    """
    esc = _xml_escape(ssid)
    if not password:
        security = (
            "    <authEncryption><authentication>open</authentication>"
            "<encryption>none</encryption><useOneX>false</useOneX></authEncryption>\n"
        )
    else:
        auth = "WPA3SAE" if sae else "WPA2PSK"
        security = (
            f"    <authEncryption><authentication>{auth}</authentication>"
            "<encryption>AES</encryption><useOneX>false</useOneX></authEncryption>\n"
            "    <sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
            f"<keyMaterial>{_xml_escape(password)}</keyMaterial></sharedKey>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
        f"  <name>{esc}</name>\n"
        f"  <SSIDConfig><SSID><name>{esc}</name></SSID></SSIDConfig>\n"
        "  <connectionType>ESS</connectionType>\n"
        "  <connectionMode>auto</connectionMode>\n"
        "  <MSM><security>\n"
        f"{security}"
        "  </security></MSM>\n"
        "</WLANProfile>\n"
    )


# ──────────────────────────────────────────────
#  网络 Worker（在独立 QThread 中运行）
#  重要：所有方法通过 Signal → Slot 机制调用，不跨线程直接调用
# ──────────────────────────────────────────────
class NetWorker(QObject):
    # 上行 Signal（主线程 → Worker）
    sig_check  = Signal()
    sig_login  = Signal(str, str, str)   # username, password, operator
    sig_logout = Signal()
    sig_scan   = Signal()                # 扫描周围 Wi-Fi（设置页选取备用网络用）
    sig_set_backup = Signal(str, str)    # 更新备用 Wi-Fi 凭据 (ssid, password)

    # 下行 Signal（Worker → 主线程）
    status = Signal(bool)        # True=已登录
    result = Signal(str, str)    # (action, message)
    scan_result = Signal(list)   # 扫描到的 SSID 列表

    def __init__(self, backup_ssid: str = "", backup_password: str = "") -> None:
        super().__init__()
        self.session = requests.Session()
        self.backup_ssid = backup_ssid
        self.backup_password = backup_password
        # 连接上行信号到对应槽（在 worker 所在线程执行）
        self.sig_check.connect(self._do_check)
        self.sig_login.connect(self._do_login)
        self.sig_logout.connect(self._do_logout)
        self.sig_scan.connect(self._do_scan)
        self.sig_set_backup.connect(self._set_backup)

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

    # ---------- Wi-Fi 管理（校园网/备用网共用同一连接逻辑） ----------
    def _current_wifi_ssid(self) -> Optional[str]:
        return _parse_wireless_ssid(_run_netsh(["show", "interfaces"]))

    def _connect_wifi(self, ssid: str, password: Optional[str] = None,
                      refresh: bool = False) -> bool:
        """连接指定 Wi-Fi，成功关联返回 True。

        - 已在该 SSID 上：直接 True（一次查询，开销极小；惰性驻留
          备用网在线时零成本，也不重置会话）
        - 不在信号范围（如在家/校外）或为企业级 802.1X：False，
          绝不动用户的网络
        - refresh=False：profile 存在即复用，不存在才创建（校园网
          开放网络，配置永不变化，与旧版行为一致）
        - refresh=True：先删旧 profile 再重建（备用网密码可能已在
          设置中变更，以设置为准）
        - 关联成功后重置 requests 会话：网络栈已切换，连接池里旧网
          的 keep-alive 死连接会让随后的探针/登录请求误报失败
        - 无线网卡不存在等任何异常：False，按原流程继续
        """
        try:
            if self._current_wifi_ssid() == ssid:
                return True

            nets = dict(_parse_scan_networks(_run_netsh(["show", "networks"])))
            if ssid not in nets or _auth_kind(nets[ssid]) == "enterprise":
                return False

            if refresh:
                _run_netsh(["delete", "profile", f"name={ssid}"])
            if refresh or ssid not in _parse_profile_names(_run_netsh(["show", "profiles"])):
                if not self._add_profile(ssid, password,
                                         _auth_kind(nets[ssid]) == "sae3"):
                    return False

            _run_netsh(["connect", f"name={ssid}", f"ssid={ssid}"])

            deadline = time.time() + WIFI_CONNECT_WAIT
            while True:
                if self._current_wifi_ssid() == ssid:
                    self.session = requests.Session()
                    return True
                if time.time() >= deadline:
                    return False
                time.sleep(1)
        except Exception:
            return False

    def _ensure_campus_wifi(self) -> bool:
        """确保已连接校园 Wi-Fi（CAMPUS_SSID）：开放网络、profile 缺失才创建"""
        return self._connect_wifi(CAMPUS_SSID)

    @staticmethod
    def _add_profile(ssid: str, password: Optional[str], sae: bool) -> Optional[str]:
        """创建用户级 WLAN 配置文件（无需管理员），失败返回 None"""
        try:
            fd, path = tempfile.mkstemp(suffix=".xml", prefix="cumt_wlan_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(_wlan_profile_xml(ssid, password, sae))
                _run_netsh(["add", "profile", f"filename={path}", "user=current"])
            finally:
                os.remove(path)
            return ssid
        except Exception:
            return None

    # ---------- 备用 Wi-Fi（校园网不可用时的故障转移） ----------
    def _set_backup(self, ssid: str, password: str) -> None:
        self.backup_ssid, self.backup_password = ssid, password

    def _do_scan(self) -> None:
        nets = _parse_scan_networks(_run_netsh(["show", "networks"]))
        self.scan_result.emit([s for s, _auth in nets])

    def _try_backup_wifi(self) -> bool:
        """切换到备用 Wi-Fi；连接成功且外网复探在线才返回 True。

        仅在真实发生切换并确认在线时 emit result("wifi", ssid)——
        驻留备用网在线时 _do_check 首行探针即为 True，走不到这里，
        结构上保证不会每个检测周期重复通知。未配置备用（SSID 为
        空）时零开销直接 False。
        """
        if not self.backup_ssid:
            return False
        if not self._connect_wifi(self.backup_ssid, self.backup_password, refresh=True):
            return False
        if not self._is_logged_in():
            return False
        self.result.emit("wifi", self.backup_ssid)
        return True

    def _do_check(self) -> None:
        logged = self._is_logged_in()
        if not logged:
            if self._ensure_campus_wifi():
                logged = self._is_logged_in()   # Wi-Fi 刚接好，复探一次外网
            else:
                # 仅在校园网"关联失败"（不在范围/连不上）时才切备用；
                # 连上了但不通走主线程自动登录 → _do_login 尾部兜底，
                # 不能在这里抢先切网，否则 Portal 认证这条正常补救
                # 路径会被绕过
                logged = self._try_backup_wifi()
        self.status.emit(logged)

    def _do_login(self, username: str, password: str, operator: str) -> None:
        self._ensure_campus_wifi()   # 认证前确保 Wi-Fi 就绪（已连接时开销极小）
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
            logged = self._is_logged_in()
            if not logged:
                # 认证失败/接口异常/外网仍不通：兜底切备用 Wi-Fi
                logged = self._try_backup_wifi()
            self.status.emit(logged)

    def _do_logout(self) -> None:
        # 已驻留备用 Wi-Fi 时校园认证本就未生效，短路避免对 Portal 白等超时。
        # 不能用 "SSID != CAMPUS_SSID" 判断——会误伤有线接校园网的用户
        if self.backup_ssid and self._current_wifi_ssid() == self.backup_ssid:
            self.result.emit("logout", "not_logged_in")
            return
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
    scan_requested = Signal()      # 请求扫描周围 Wi-Fi（由 App 转发到 worker 线程）

    def __init__(self, cfg: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CUMT 校园网登录 — 设置")
        self.setFixedSize(400, 545)
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

        # 备用 Wi-Fi：校园网不可用时自动切换的兜底网络
        bak_card = QFrame()
        bak_card.setStyleSheet("QFrame{background:white;border-radius:12px;}")
        bcl = QVBoxLayout(bak_card)
        bcl.setSpacing(10); bcl.setContentsMargins(16, 16, 16, 16)

        bak_title = QLabel("📶  备用 Wi-Fi")
        bak_title.setStyleSheet("font-size:14px; font-weight:bold; color:#1d1d1f;")
        bcl.addWidget(bak_title)
        bak_hint = QLabel("校园网连不上或认证失败时自动切换；备用可用期间不主动切回")
        bak_hint.setStyleSheet("color:#999; font-size:11px;")
        bak_hint.setWordWrap(True)
        bcl.addWidget(bak_hint)

        srow = QHBoxLayout(); srow.setSpacing(8)
        self.ssid_box = QComboBox()
        self.ssid_box.setEditable(True)      # 允许手动输入（信号当前不在范围内也能配置）
        self.ssid_box.setEditText(cfg.get("backup_ssid", ""))
        self.ssid_box.lineEdit().setPlaceholderText("扫描选择或手动输入 SSID")
        self.ssid_box.setStyleSheet(fstyle)
        self.scan_btn = QPushButton("扫描")
        self.scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scan_btn.setStyleSheet(
            "QPushButton{background:#4CAF50;color:white;border:none;border-radius:7px;"
            "padding:7px 14px;font-size:13px;}"
            "QPushButton:hover{background:#43A047;}"
            "QPushButton:disabled{background:#a5d6a7;}"
        )
        self.scan_btn.clicked.connect(self._scan)
        srow.addWidget(self.ssid_box, 1)
        srow.addWidget(self.scan_btn)
        bcl.addLayout(srow)

        self.bak_pw_edit = QLineEdit(cfg.get("backup_password", ""))
        self.bak_pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.bak_pw_edit.setPlaceholderText("备用 Wi-Fi 密码（开放网络留空）")
        self.bak_pw_edit.setStyleSheet(fstyle)
        bcl.addWidget(self.bak_pw_edit)

        lay.addWidget(bak_card)

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

    def _scan(self) -> None:
        """请求 App 转发到 worker 线程执行 netsh 扫描（避免 UI 卡顿）"""
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("扫描中...")
        self.scan_requested.emit()

    def fill_networks(self, ssids: list) -> None:
        """扫描结果到达：填充下拉框，保留用户已输入的文本"""
        cur = self.ssid_box.currentText()
        self.ssid_box.clear()
        self.ssid_box.addItems(ssids)
        self.ssid_box.setEditText(cur)
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("扫描")

    def _save(self) -> None:
        self.save_requested.emit({
            "username":       self.id_edit.text().strip(),
            "password":       self.pw_edit.text(),
            "operator":       self.op_box.currentText(),
            "autostart":      self.cb_autostart.isChecked(),
            "auto_login":     self.cb_autologin.isChecked(),
            "check_interval": self.spin.value(),
            "backup_ssid":    self.ssid_box.currentText().strip(),
            "backup_password": self.bak_pw_edit.text(),
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

        # Worker 线程（构造期注入备用 Wi-Fi 凭据，早于线程启动无竞态）
        self._thread = QThread()
        self._worker = NetWorker(self.cfg.get("backup_ssid", ""),
                                 self.cfg.get("backup_password", ""))
        self._worker.moveToThread(self._thread)
        self._worker.status.connect(self._on_status)
        self._worker.result.connect(self._on_result)
        self._worker.scan_result.connect(self._on_scan_result)
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
        elif action == "wifi":
            # msg 为备用 Wi-Fi 的 SSID；仅真实切换并确认在线时才会到达
            self.tray.showMessage(
                "已切换备用 Wi-Fi", f"校园网不可用，已连接 {msg}",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    # ── Wi-Fi 扫描（设置页备用网络选取）────────
    def _on_scan_requested(self) -> None:
        self._worker.sig_scan.emit()

    def _on_scan_result(self, ssids: list) -> None:
        # 窗口未创建则丢弃；窗口仅 hide 不销毁，存活期内填充安全
        if self._settings_win is not None:
            self._settings_win.fill_networks(ssids)

    # ── 设置窗口 ──────────────────────────────
    def _open_settings(self) -> None:
        if self._settings_win is None:
            self._settings_win = SettingsWindow(self.cfg)
            self._settings_win.save_requested.connect(self._on_save)
            self._settings_win.scan_requested.connect(self._on_scan_requested)
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
            self._settings_win.ssid_box.setEditText(self.cfg.get("backup_ssid", ""))
            self._settings_win.bak_pw_edit.setText(self.cfg.get("backup_password", ""))

        self._settings_win.show()
        self._settings_win.raise_()
        self._settings_win.activateWindow()

    def _on_save(self, new_cfg: dict) -> None:
        self.cfg = {**self.cfg, **new_cfg}
        save_config(self.cfg)
        set_auto_start(self.cfg["autostart"])
        self._restart_timer()
        # 推送备用 Wi-Fi 凭据到 worker 线程（后续故障转移即用新值）
        self._worker.sig_set_backup.emit(
            self.cfg.get("backup_ssid", ""), self.cfg.get("backup_password", ""))
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
