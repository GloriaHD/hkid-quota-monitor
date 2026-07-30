"""跨模块共享的小工具。"""

from __future__ import annotations


def mask_email(addr: str) -> str:
    """邮箱脱敏后再打印。

    CI 日志在公开仓库里任何人可读且长期留存——名册加密了但日志打明文，
    等于把保护绕过去。所有涉及订阅者地址的日志一律走这里。
    """
    addr = (addr or "").strip()
    if "@" not in addr:
        return "***"
    local, _, domain = addr.partition("@")
    keep_local = local[:1] if local else ""
    dom_name, dot, tld = domain.partition(".")
    keep_dom = dom_name[:1] if dom_name else ""
    return f"{keep_local}***@{keep_dom}***{dot}{tld}" if dot else f"{keep_local}***@***"
