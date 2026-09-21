# -*- coding: utf-8 -*-
"""备用 Wi-Fi 故障转移场景测试。

在主线程直接构造 NetWorker（信号直连、同步派发），monkeypatch
_run_netsh / requests.Session / WIFI_CONNECT_WAIT / time.sleep，
配合 FakeSession 模拟各故障场景。注意：_connect_wifi 成功切换后
会用 requests.Session() 换新会话——必须 patch 成可控标记，
否则测试会发起真实网络请求。运行：
    uv run python tests/test_failover.py
"""
import sys
import os
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QCoreApplication   # 须先有 QCoreApplication 再建 QObject

import win_login_app
from win_login_app import NetWorker, CAMPUS_SSID
from test_is_logged_in import FakeSession, FakeResponse

_app = QCoreApplication.instance() or QCoreApplication([])

ONLINE  = FakeSession({"generate_204": (204, "", None)})   # 探针直接通过
OFFLINE = FakeSession({})                                  # 全部超时


class LoginResponse(FakeResponse):
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class LoginSession:
    """_do_login 的 Portal 请求带 headers 且调用 raise_for_status；
    路由 {子串: (status, text)}，未命中抛超时"""

    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kwargs):
        for pat, (status, text) in self.routes.items():
            if pat in url:
                return LoginResponse(status, text, url)
        raise TimeoutError(f"no route: {url}")


class FakeNetsh:
    """_run_netsh 假实现：按子命令分发文本；connect 即视为关联成功，
    除非目标 ssid 等于 fail_ssid（模拟关联超时不换网）"""

    def __init__(self, networks_text, profiles_text="", current=None, fail_ssid=None):
        self.networks_text = networks_text
        self.profiles_text = profiles_text
        self.current = current
        self.fail_ssid = fail_ssid
        self.calls = []        # 每次调用的完整参数列表
        self.added_xml = ""    # 最近一次 add profile 的 XML 内容

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["show", "interfaces"]:
            return f"    SSID                   : {self.current}" if self.current \
                else "    SSID :"
        if args[:2] == ["show", "networks"]:
            return self.networks_text
        if args[:2] == ["show", "profiles"]:
            return self.profiles_text
        if args[0] == "connect":
            ssid = next(a.split("=", 1)[1] for a in args if a.startswith("ssid="))
            if ssid != self.fail_ssid:
                self.current = ssid
            return ""
        if args[0] == "delete":
            return ""
        if args[0] == "add":
            for a in args:
                if a.startswith("filename="):
                    with open(a.split("=", 1)[1], encoding="utf-8") as f:
                        self.added_xml = f.read()
            return "已将配置文件添加到接口 WLAN。"
        return ""


def install_fake(fake):
    win_login_app._run_netsh = fake
    return fake


def case(name, got, expected):
    ok = got == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: 期望 {expected!r}, 实际 {got!r}")
    return ok


def make_worker(backup_ssid="BakAP", backup_password="pass123"):
    w = NetWorker(backup_ssid, backup_password)
    out = {"status": [], "result": []}
    w.status.connect(lambda v: out["status"].append(v))
    w.result.connect(lambda a, m: out["result"].append((a, m)))
    return w, out


SCAN_BOTH = f"""此系统上可见的网络数: 2

SSID 1 : {CAMPUS_SSID}
    身份验证              : 开放

SSID 2 : BakAP
    身份验证              : WPA2-个人
"""

SCAN_BACKUP_ONLY = """此系统上可见的网络数: 1

SSID 1 : BakAP
    身份验证              : WPA2-个人
"""

SCAN_NONE = """此系统上可见的网络数: 1

SSID 1 : OtherNet
    身份验证              : WPA2-个人
"""


def main():
    ok = True

    # 全局 monkeypatch：轮询零等待 + sleep 免睡 + 会话重置返回可控标记
    real_time = win_login_app.time
    win_login_app.time = types.SimpleNamespace(time=real_time.time,
                                               sleep=lambda s: None)
    win_login_app.WIFI_CONNECT_WAIT = 0
    win_login_app.requests = types.SimpleNamespace(Session=lambda: ONLINE)

    # ① 在线 → _do_check 零 netsh 调用
    w, out = make_worker()
    w.session = ONLINE
    fake = install_fake(FakeNetsh(SCAN_BOTH))
    w._do_check()
    ok &= case("在线 → 零 netsh、status=True",
               (out["status"], fake.calls), ([True], []))

    # ② 离线 + 校园在扫描中 + 关联成功 → 复探在线，不碰备用
    w, out = make_worker()
    w.session = OFFLINE
    fake = install_fake(FakeNetsh(SCAN_BOTH, current="OtherNet"))
    w._do_check()
    ok &= case("校园可连 → 走校园、status=True",
               (out["status"], out["result"]), ([True], []))
    ok &= case("校园可连 → connect 目标为校园",
               [c for c in fake.calls if c[0] == "connect"],
               [["connect", f"name={CAMPUS_SSID}", f"ssid={CAMPUS_SSID}"]])

    # ③ 离线 + 校园不在范围 + 备用在 → 切备用（删旧建新 profile）
    w, out = make_worker()
    w.session = OFFLINE
    fake = install_fake(FakeNetsh(SCAN_BACKUP_ONLY, current="OtherNet"))
    w._do_check()
    ok &= case("校园缺失 → 切备用、status=True",
               (out["status"], out["result"]), ([True], [("wifi", "BakAP")]))
    ok &= case("切备用 → 先删旧 profile",
               [c for c in fake.calls if c[0] == "delete"],
               [["delete", "profile", "name=BakAP"]])
    ok &= case("切备用 → profile XML 含明文密码",
               "<keyMaterial>pass123</keyMaterial>" in fake.added_xml, True)

    # ④ 校园与备用都不在范围 → status=False，不动网络
    w, out = make_worker()
    w.session = OFFLINE
    fake = install_fake(FakeNetsh(SCAN_NONE, current="OtherNet"))
    w._do_check()
    ok &= case("都不在范围 → status=False", out["status"], [False])
    ok &= case("都不在范围 → 零 connect/delete/add",
               [c for c in fake.calls if c[0] in ("connect", "delete", "add")], [])

    # ⑤ 未配置备用 → _try_backup_wifi 零开销直接 False
    w, _ = make_worker(backup_ssid="")
    fake = install_fake(FakeNetsh(SCAN_BACKUP_ONLY, current="OtherNet"))
    ok &= case("未配置备用 → False 且零 netsh",
               (w._try_backup_wifi(), fake.calls), (False, []))

    # ⑥ 校园在范围但关联超时 → 兜底切备用
    w, out = make_worker()
    w.session = OFFLINE
    fake = install_fake(FakeNetsh(SCAN_BOTH, current="OtherNet",
                                  fail_ssid=CAMPUS_SSID))
    w._do_check()
    ok &= case("校园关联失败 → 兜底切备用",
               (out["status"], out["result"]), ([True], [("wifi", "BakAP")]))

    # ⑦ 手动登录：Portal 不可达 + 探针离线 + 备用可达 → 尾部切备用
    w, out = make_worker()
    w.session = FakeSession({})   # 一切请求超时
    fake = install_fake(FakeNetsh(SCAN_BACKUP_ONLY, current="OtherNet"))
    w._do_login("123", "pw", "校园网")
    ok &= case("登录全失败 → 尾部切备用",
               (out["status"][-1:], out["result"][1:]), ([True], [("wifi", "BakAP")]))
    ok &= case("登录全失败 → 先报登录错误",
               out["result"][0][0] == "login"
               and out["result"][0][1].startswith("error:"), True)

    # ⑧ 登录成功 → 直接绿，不动 Wi-Fi
    w, out = make_worker()
    w.session = LoginSession({"801": (200, 'dr1({"result":"1"})')})
    fake = install_fake(FakeNetsh(SCAN_BOTH, current=CAMPUS_SSID))
    w._do_login("123", "pw", "校园网")
    ok &= case("登录成功 → status=True 不碰备用",
               (out["status"], out["result"]), ([True], [("login", "success")]))

    # ⑨ 惰性驻留：备用在线 → 不切换、不重复通知
    w, out = make_worker()
    w.session = ONLINE
    fake = install_fake(FakeNetsh(SCAN_BOTH, current="BakAP"))
    w._do_check()
    ok &= case("驻留备用在线 → 不动网络不重复通知",
               (out["status"], out["result"],
                [c for c in fake.calls if c[0] == "connect"]),
               ([True], [], []))

    # ⑩ 扫描 → SSID 列表
    w, _ = make_worker()
    scans = []
    w.scan_result.connect(lambda ssids: scans.append(ssids))
    install_fake(FakeNetsh(SCAN_BOTH))
    w._do_scan()
    ok &= case("_do_scan → SSID 列表", scans, [[CAMPUS_SSID, "BakAP"]])

    # ⑪ 驻留备用时注销 → 短路，不对 Portal 白等
    w, out = make_worker()
    w.session = ONLINE
    fake = install_fake(FakeNetsh(SCAN_BOTH, current="BakAP"))
    w._do_logout()
    ok &= case("驻留备用注销 → 短路",
               (out["result"], [c for c in fake.calls if c[0] != "show"]),
               ([("logout", "not_logged_in")], []))

    print("=" * 40)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
