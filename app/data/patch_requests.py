"""数据层:修复 requests 请求头,让 akshare 能够稳定访问行情源。

东财等行情源会拦截默认 UA(python-requests),通过给 requests 的
HTTPAdapter 注入浏览器 UA 进行修复(SilverQuant reader 层同样受益)。
"""
import requests
from requests.adapters import HTTPAdapter

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_original_send = HTTPAdapter.send


def _patched_send(self, request, *args, **kwargs):
    request.headers["User-Agent"] = UA
    request.headers.setdefault("Accept", "*/*")
    request.headers.setdefault("Connection", "keep-alive")
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
