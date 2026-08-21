"""guga:基于 SilverQuant 改造的 Akshare + VectorBT 量化预测分析系统。"""
__version__ = "0.1.0"

# 强制直连(绕开系统代理对 eastmoney/sina 等数据源的干扰)。
# 注意:不能只设 Session.trust_env=False 类属性——requests 的 Session.__init__
# 会以实例属性 self.trust_env=True 覆盖类属性(requests 2.34.x 实测),
# 导致补丁静默失效,系统一开代理(如 Clash)且代理失联时全部请求 ProxyError。
# 因此这里包装 __init__,在每个实例创建后强制关闭 trust_env,
# 使环境变量/Windows 注册表代理一律不生效。需在 import requests/akshare 之前执行。
try:
    import requests as _req

    if not getattr(_req.sessions.Session, "_direct_conn_patched", False):
        _orig_session_init = _req.sessions.Session.__init__

        def _patched_session_init(self, *args, **kwargs):
            _orig_session_init(self, *args, **kwargs)
            self.trust_env = False

        _req.sessions.Session.__init__ = _patched_session_init
        _req.sessions.Session._direct_conn_patched = True
except Exception:  # noqa: BLE001
    pass
