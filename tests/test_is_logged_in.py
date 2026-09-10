# -*- coding: utf-8 -*-
"""_is_logged_in 假绿灯回归测试。

复现 bug：开机自启后应用显示"已登录"，但网关实际未放行（外网不通、
认证后台无设备）。旧检测把 Portal 页面状态（标题"注销"/含"已登录"）
和弱校验的 baidu 文本当作在线证明，而 eportal 状态与网关放行可能脱同步。

运行：uv run python tests/test_is_logged_in.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from win_login_app import NetWorker, PORTAL_HOST


class FakeResponse:
    apparent_encoding = "utf-8"   # 忠实模拟 requests.Response 的该属性

    def __init__(self, status_code=200, text="", url="http://example.com/"):
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    """url_pattern -> (status_code, text)；未命中或标记 raise 则抛异常模拟超时"""

    def __init__(self, routes, raise_on=()):
        self.routes = routes          # {子串: (status, text, 最终url)}
        self.raise_on = raise_on      # 命中这些子串时抛超时异常

    def get(self, url, timeout=None):
        for pat, (status, text, final_url) in self.routes.items():
            if pat in url:
                if any(p in url for p in self.raise_on):
                    raise TimeoutError(f"connect timeout: {url}")
                return FakeResponse(status, text, final_url or url)
        raise TimeoutError(f"no route: {url}")


def make_worker(session):
    """绕过 __init__（不启动 Qt 信号），只注入 session 后调用被测方法"""
    w = NetWorker.__new__(NetWorker)
    w.session = session
    return w


# 典型 Dr.com Portal 登录页（含密码框、baidu 快捷链接字样）
PORTAL_LOGIN_PAGE = (
    '<html><head><title>欢迎登录校园网</title></head><body>'
    '<script src="http://' + PORTAL_HOST + '/eportal/interface?cmd=login"></script>'
    '<a href="http://www.baidu.com">baidu</a>'
    '<input type="password" name="user_password"></body></html>'
)

# eportal 残留会话状态页：认证库仍认为该 IP 在线（假绿灯场景 ①）
PORTAL_STALE_STATUS_PAGE = (
    '<html><head><title>注销页 - 校园网</title></head>'
    '<body>您已登录，<a href="logout">注销</a></body></html>'
)

# 网关透明拦截、以 200 返回的伪 baidu 页（含 baidu 字样，假绿灯场景 ②）
INTERCEPTED_FAKE_BAIDU = (
    '<html><body>网络异常，请登录后访问 <a href="http://www.baidu.com">baidu</a></body></html>'
)

REAL_BAIDU = '<html><head><title>百度一下，你就知道</title></head></html>'


def case(name, worker, expected):
    got = worker._is_logged_in()
    tag = "PASS" if got == expected else "FAIL"
    print(f"[{tag}] {name}: 期望 {expected}, 实际 {got}")
    return got == expected


def main():
    ok = True

    # ① eportal 残留状态页（标题含"注销"、正文含"已登录"）但外网不通 —— 必须判为未登录
    w = make_worker(FakeSession(
        {"baidu": (200, "", None), PORTAL_HOST: (200, PORTAL_STALE_STATUS_PAGE, None)},
        raise_on=("baidu",),
    ))
    ok &= case("eportal残留状态页+外网不通 → 不得假绿", w, False)

    # ② 网关以 200 拦截 baidu 且页面含 "baidu" 字样 —— 不得假绿
    w = make_worker(FakeSession(
        {"baidu": (200, INTERCEPTED_FAKE_BAIDU, None)},
    ))
    ok &= case("拦截页含baidu字样 → 不得假绿", w, False)

    # ③ Portal 正常登录页（含密码框）+ 外网不通 —— 未登录
    w = make_worker(FakeSession(
        {"baidu": (200, "", None), PORTAL_HOST: (200, PORTAL_LOGIN_PAGE, None)},
        raise_on=("baidu",),
    ))
    ok &= case("Portal登录页 → 未登录", w, False)

    # ④ 真在线：204 探针成功 —— 在线
    w = make_worker(FakeSession(
        {"generate_204": (204, "", None), "baidu": (200, REAL_BAIDU, None)},
    ))
    ok &= case("204探针成功 → 在线", w, True)

    # ⑤ 真在线：baidu 返回真实标题 —— 在线
    w = make_worker(FakeSession(
        {"generate_204": (200, "", None), "baidu": (200, REAL_BAIDU, None)},
    ))
    ok &= case("baidu真实标题 → 在线", w, True)

    # ⑥ 完全断网（全部超时）—— 未登录
    w = make_worker(FakeSession({}, raise_on=("baidu", "generate_204", PORTAL_HOST)))
    ok &= case("全部不可达 → 未登录", w, False)

    print("=" * 40)
    print("全部通过" if ok else "存在失败用例")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
