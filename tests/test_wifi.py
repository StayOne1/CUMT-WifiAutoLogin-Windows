# -*- coding: utf-8 -*-
"""WLAN netsh 文本解析纯函数的单元测试。

样本覆盖中英文系统、连接/断开状态。运行：
    uv run python tests/test_wifi.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from win_login_app import (
    CAMPUS_SSID,
    _parse_wireless_ssid,
    _parse_profile_names,
    _parse_scan_networks,
    _auth_kind,
    _wlan_profile_xml,
)

# ── netsh wlan show interfaces 输出样本 ──────────────────
ZH_CONNECTED = """系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 12345678-abcd-ef01-2345-6789abcdef01
    物理地址               : aa:bb:cc:dd:ee:ff
    状态                   : 已连接
    SSID                   : CUMT_Stu
    BSSID                  : a0:b1:c2:d3:e4:f5
    网络类型               : 结构
    无线电类型             : 802.11ax
    身份验证               : 开放
    已接收字节数           : 1234567
"""

ZH_DISCONNECTED = """系统上有 1 个接口:

    名称                   : WLAN
    描述                   : Intel(R) Wi-Fi 6 AX201 160MHz
    物理地址               : aa:bb:cc:dd:ee:ff
    状态                   : 已断开连接
    SSID :
    BSSID                  : 不可用
"""

EN_CONNECTED = """
    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201
    State                  : connected
    SSID                   : CUMT_Stu
    BSSID                  : a0:b1:c2:d3:e4:f5
    Radio type             : 802.11ax
    Authentication         : Open
"""

EN_CONNECTED_OTHER = """
    State                  : connected
    SSID                   : HomeWiFi_5G
    BSSID                  : a0:b1:c2:d3:e4:f5
"""

# 只有 BSSID 行（带值）、无 SSID 行——不应误匹配
ONLY_BSSID = """
    State                  : disconnected
    BSSID                  : a0:b1:c2:d3:e4:f5
    SSID :
"""

# ── netsh wlan show profiles 输出样本 ────────────────────
ZH_PROFILES = """系统上的接口配置文件:

组策略配置文件（仅当启用了组策略时显示）:
----------------------------------------
    <无>

用户配置文件
-------------
    所有用户配置文件     : CUMT_Stu
    所有用户配置文件     : HomeWiFi_5G
    所有用户配置文件     : CMCC-EDU
"""

EN_PROFILES = """
User profiles
-------------
    All User Profile     : CUMT_Stu
    All User Profile     : HomeWiFi_5G
"""

# ── netsh wlan show networks 输出样本 ────────────────────
ZH_NETWORKS_HIT = """此系统上可见的网络数: 3

SSID 1 : CUMT_Stu
    网络类型              : 结构
    身份验证              : 开放

SSID 2 : CMCC-EDU
    身份验证              : WPA2-个人

SSID 3 : ChinaNet
    身份验证              : WPA2-个人
"""

ZH_NETWORKS_MISS = """此系统上可见的网络数: 2

SSID 1 : HomeWiFi_5G
    身份验证              : WPA2-个人

SSID 2 : ChinaNet
    身份验证              : WPA2-个人
"""

EN_NETWORKS = """
There are 3 networks currently visible:

SSID 1 : CUMT_Stu
    Network type             : Infrastructure
    Authentication           : Open
    Encryption               : None
    BSSID 1                  : a0:b1:c2:d3:e4:f5
         Signal              : 86%
         Radio type          : 802.11ax
         Channel             : 6

SSID 2 : HomeWiFi_5G
    Authentication           : WPA2-Personal

SSID 3 : AT&T 5G
    Authentication           : WPA3-Personal
"""

# 中英文混合坑位：中文 SSID、含空格、WPA3、WPA2/WPA3 过渡、企业级
ZH_NETWORKS_VARIANTS = """此系统上可见的网络数: 5

SSID 1 : CUMT_Stu
    身份验证              : 开放

SSID 2 : 宿舍路由器
    身份验证              : WPA2-个人

SSID 3 : ChinaNet-5G
    身份验证              : WPA3-个人

SSID 4 : My Home WiFi
    身份验证              : WPA2/WPA3-个人

SSID 5 : Office-8021X
    身份验证              : WPA2-企业
"""


def case(name, got, expected):
    ok = got == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: 期望 {expected!r}, 实际 {got!r}")
    return ok


def main():
    ok = True
    p = _parse_wireless_ssid

    ok &= case("中文·已连接 → SSID", p(ZH_CONNECTED), CAMPUS_SSID)
    ok &= case("中文·已断开 → None", p(ZH_DISCONNECTED), None)
    ok &= case("英文·已连接 → SSID", p(EN_CONNECTED), CAMPUS_SSID)
    ok &= case("英文·连的其他网 → 原样返回", p(EN_CONNECTED_OTHER), "HomeWiFi_5G")
    ok &= case("仅有 BSSID 行 → None（防误匹配）", p(ONLY_BSSID), None)

    ok &= case("中文·配置文件列表", _parse_profile_names(ZH_PROFILES),
               [CAMPUS_SSID, "HomeWiFi_5G", "CMCC-EDU"])
    ok &= case("英文·配置文件列表", _parse_profile_names(EN_PROFILES),
               [CAMPUS_SSID, "HomeWiFi_5G"])

    # ── 扫描结果解析（备用 Wi-Fi 选取/连接的依据） ──────────
    ns = _parse_scan_networks
    ok &= case("扫描解析·中文", ns(ZH_NETWORKS_HIT),
               [(CAMPUS_SSID, "开放"), ("CMCC-EDU", "WPA2-个人"),
                ("ChinaNet", "WPA2-个人")])
    ok &= case("扫描解析·英文·BSSID 不误匹配", ns(EN_NETWORKS),
               [("CUMT_Stu", "Open"), ("HomeWiFi_5G", "WPA2-Personal"),
                ("AT&T 5G", "WPA3-Personal")])
    ok &= case("扫描解析·中文/空格/过渡/企业", ns(ZH_NETWORKS_VARIANTS),
               [(CAMPUS_SSID, "开放"), ("宿舍路由器", "WPA2-个人"),
                ("ChinaNet-5G", "WPA3-个人"), ("My Home WiFi", "WPA2/WPA3-个人"),
                ("Office-8021X", "WPA2-企业")])
    ok &= case("扫描解析·精确匹配（_5G 不误命中）",
               CAMPUS_SSID in dict(ns("SSID 1 : CUMT_Stu_5G\n    身份验证 : 开放\n")), False)
    ok &= case("扫描解析·空输出", ns(""), [])

    # ── 身份验证字段分类（决定 profile 形状/是否可连） ──────────
    ak = _auth_kind
    ok &= case("auth·开放", ak("开放"), "open")
    ok &= case("auth·Open", ak("Open"), "open")
    ok &= case("auth·空串", ak(""), "open")
    ok &= case("auth·WPA2-个人", ak("WPA2-个人"), "psk2")
    ok &= case("auth·WPA2-Personal", ak("WPA2-Personal"), "psk2")
    ok &= case("auth·WPA2/WPA3 过渡 → psk2", ak("WPA2/WPA3-个人"), "psk2")
    ok &= case("auth·WPA3-个人 → sae3", ak("WPA3-个人"), "sae3")
    ok &= case("auth·WPA3-Personal → sae3", ak("WPA3-Personal"), "sae3")
    ok &= case("auth·WPA2-企业 → enterprise", ak("WPA2-企业"), "enterprise")
    ok &= case("auth·WPA2-Enterprise", ak("WPA2-Enterprise"), "enterprise")

    # ── profile XML 关键字段（校园开放网络 + 带密码备用网络） ──
    xml = _wlan_profile_xml(CAMPUS_SSID)
    ok &= case("XML·SSID 名称", CAMPUS_SSID in xml and f"<name>{CAMPUS_SSID}</name>" in xml, True)
    ok &= case("XML·open/none 认证", "<authentication>open</authentication>" in xml
               and "<encryption>none</encryption>" in xml
               and "keyMaterial" not in xml, True)
    ok &= case("XML·UTF-8 声明", 'encoding="UTF-8"' in xml, True)

    xml2 = _wlan_profile_xml("AT&T 5G", "a<b>&c")
    ok &= case("XML·WPA2 明文密码", "<authentication>WPA2PSK</authentication>" in xml2
               and "<encryption>AES</encryption>" in xml2
               and "<keyType>passPhrase</keyType>" in xml2
               and "<protected>false</protected>" in xml2
               and "<keyMaterial>a&lt;b&gt;&amp;c</keyMaterial>" in xml2, True)
    ok &= case("XML·SSID 转义两处", xml2.count("AT&amp;T 5G"), 2)
    ok &= case("XML·WPA3SAE", "<authentication>WPA3SAE</authentication>"
               in _wlan_profile_xml("Dorm", "pass123", sae=True), True)
    ok &= case("XML·中文 SSID", "<name>宿舍路由器</name>"
               in _wlan_profile_xml("宿舍路由器", "pass123"), True)

    print("=" * 40)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
