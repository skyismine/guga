"""guga:基于 SilverQuant 改造的 Akshare + VectorBT 量化预测分析系统。"""
__version__ = "0.1.0"

# 强制直连(绕开系统代理对 eastmoney/sina 等数据源的干扰)。
# 需在任意模块 import requests/akshare 之前全局生效,否则其内部 Session
# 会在 Windows 上读取注册表代理(系统开启了系统代理时)导致 ProxyError。
try:
    import requests as _req
    _req.sessions.Session.trust_env = False
except Exception:  # noqa: BLE001
    pass
