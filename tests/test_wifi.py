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
    _ssid_in_scan,
    _open_profile_xml,
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

    ok &= case("扫描结果·在范围内", _ssid_in_scan(ZH_NETWORKS_HIT), True)
    ok &= case("扫描结果·不在范围", _ssid_in_scan(ZH_NETWORKS_MISS), False)

    # 开放网络 profile XML 关键字段
    xml = _open_profile_xml()
    ok &= case("XML·SSID 名称", CAMPUS_SSID in xml and f"<name>{CAMPUS_SSID}</name>" in xml, True)
    ok &= case("XML·open/none 认证", "<authentication>open</authentication>" in xml
               and "<encryption>none</encryption>" in xml, True)

    print("=" * 40)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
