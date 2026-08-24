"""数据层:修复 requests 请求头,让 akshare 能够稳定访问行情源。

- 东财等行情源会拦截默认 UA(python-requests),通过给 requests 的
  HTTPAdapter 注入浏览器 UA 进行修复(SilverQuant reader 层同样受益)。
- 强制默认超时:akshare 内部大量 `requests.get(url)` 不传 timeout,遇到连接假死
  (SSL 读阻塞,如东财 RemoteDisconnected / 新浪无响应)会**永久挂起**;
  这里在所有请求未显式指定 timeout 时兜底注入默认值,把挂死降级为快速失败,
  由上层 _retry / 快照回退 / 多源切换兜底,保证页面与报告不阻塞。
"""
import requests
from requests.adapters import HTTPAdapter

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 默认网络超时(秒):行情接口通常 1-3s 内返回,15s 足够覆盖慢源且避免假死
DEFAULT_TIMEOUT = 15.0

_original_send = HTTPAdapter.send


def _patched_send(self, request, *args, **kwargs):
    request.headers["User-Agent"] = UA
    request.headers.setdefault("Accept", "*/*")
    request.headers.setdefault("Connection", "keep-alive")
    if kwargs.get("timeout") is None:   # 未显式传超时则兜底,防止读阻塞永久挂起
        kwargs["timeout"] = DEFAULT_TIMEOUT
    return _original_send(self, request, *args, **kwargs)


def install():
    HTTPAdapter.send = _patched_send
    # 兼容部分模块直接使用 requests.utils.default_headers
    requests.utils.default_headers = lambda: {
        "User-Agent": UA,
        "Accept-Encoding": "gzip, deflate, zstd",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }


install()
