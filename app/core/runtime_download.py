"""Select a reachable runtime route and verify the package before installation."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import time
from urllib.request import Request

from app.core.runtime_package import RUNTIME_RELEASE_URL, _validated_http_url, sha256_file


MAGIC = b"7z\xbc\xaf\x27\x1c"
PROBE_BYTES = 64 * 1024


def runtime_download_sources(url: str) -> tuple[tuple[str, str], ...]:
    url = _validated_http_url(url, field="环境包下载地址")
    # Never send a custom/private URL or its credentials to a public proxy.
    if url != RUNTIME_RELEASE_URL:
        return (("自定义下载源", url),)
    return (
        ("加速线路 1", f"https://gh-proxy.com/{url}"),
        ("加速线路 2", f"https://ghproxy.net/{url}"),
        ("GitHub 官方", url),
    )


def _check_response(response, size: int, offset: int, *, probe: bool = False) -> bool:
    status = int(response.status)
    length = response.headers.get("Content-Length")
    if status == 206:
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
        if not match:
            raise IOError("下载线路返回了无效的分段范围")
        start, end, total = map(int, match.groups())
        if start != offset or total != size or not start <= end < total:
            raise IOError("下载线路返回的文件大小或断点位置不匹配")
        if length is not None and int(length) != end - start + 1:
            raise IOError("下载线路返回的分段长度不匹配")
        if not probe and end != size - 1:
            raise IOError("下载线路没有返回完整的剩余分段")
        return True
    if status != 200:
        raise IOError(f"HTTP {status}")
    if length is not None and int(length) != size:
        raise IOError("下载线路返回的文件大小不匹配")
    return False


def download_runtime_package(url, target, *, size, sha256, open_response, progress):
    """open_response enforces the caller's HTTPS redirect policy. No auth is sent."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    if partial.exists() and partial.stat().st_size == size and sha256_file(partial) == sha256:
        partial.replace(target)
        return "本地完整缓存"
    sources = runtime_download_sources(url)
    headers = {"User-Agent": "LingJing-Runtime-Downloader", "Accept-Encoding": "identity"}
    failures = []

    def probe(source):
        label, address = source
        started = time.monotonic()
        request = Request(address, headers={**headers, "Range": f"bytes=0-{min(PROBE_BYTES, size) - 1}"})
        try:
            with open_response(request, timeout=8) as response:
                _check_response(response, size, 0, probe=True)
                sample = response.read(min(PROBE_BYTES, size))
                if len(sample) != min(PROBE_BYTES, size) or not sample.startswith(MAGIC):
                    raise IOError("下载线路没有返回有效的 7z 环境包")
            return (time.monotonic() - started, source, None)
        except Exception as exc:
            return (float("inf"), source, str(exc))

    if len(sources) > 1:
        progress(0, "自动选择下载线路", "正在测试加速线路和 GitHub，最多读取每条线路 64 KB")
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            results = list(pool.map(probe, sources))
        sources = []
        for _elapsed, source, error in sorted(results, key=lambda result: result[0]):
            if error is None:
                sources.append(source)
            else:
                failures.append(f"{source[0]}：{error}")

    for label, address in sources:
        try:
            if partial.exists() and partial.stat().st_size >= size:
                if partial.stat().st_size == size and sha256_file(partial) == sha256:
                    partial.replace(target)
                    return label
                partial.unlink()
            offset = partial.stat().st_size if partial.exists() else 0
            progress(int(offset * 100 / size), "连接下载线路", label)
            request_headers = dict(headers)
            if offset:
                request_headers["Range"] = f"bytes={offset}-"
            with open_response(Request(address, headers=request_headers), timeout=20) as response:
                resumed = _check_response(response, size, offset)
                if not resumed:
                    offset = 0
                with partial.open("ab" if resumed and offset else "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        if offset + len(chunk) > size:
                            raise IOError("下载数据超过环境包大小")
                        output.write(chunk)
                        offset += len(chunk)
                        progress(int(offset * 100 / size), "下载运行环境", f"{label} · {offset / 1024 / 1024:.1f} MB")
            if offset != size:
                raise IOError("下载中断，保留断点并尝试下一条线路")
            progress(100, "校验运行环境", f"{label} · 正在校验 SHA256")
            if sha256_file(partial) != sha256:
                partial.unlink()
                raise IOError("SHA256 校验失败，已丢弃损坏的下载文件")
            partial.replace(target)
            return label
        except Exception as exc:
            failures.append(f"{label}：{exc}")
            progress(0, "正在切换下载线路", f"{label} 未完成，尝试其他可用线路")
    raise IOError("所有下载线路均不可用。若源文件返回 404，加速线路也无法取得文件；请检查发布状态或导入本地环境包。\n" + "\n".join(failures))
