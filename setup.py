"""下载 KataGo 引擎与两个神经网络到 vendor/。

可重复执行：大小正确的文件直接跳过。
网络策略：先直连，失败或明显过慢则自动改走本地代理 127.0.0.1:7892。
设了 HTTPS_PROXY 环境变量的话，直连那一轮也会走环境变量里的代理。

Windows 下两个引擎：DirectML 走 GPU，Eigen 纯 CPU 作为驱动出问题时的退路。
选哪个在 katago.py 里，一处切换。

macOS 下不下引擎 —— KataGo 官方从不发 macOS 预编译包，只能 brew install katago。
这里只下配置文件（brew 的包不带）和两个模型。
"""

import os
import platform
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
IS_MAC = platform.system() == "Darwin"
KATAGO_BIN = "katago" if IS_MAC else "katago.exe"
# 直连失败才走它。本机那个代理在别的机器上不存在，所以留个环境变量能改 ——
# 换台机器跑不用改代码。
PROXY = os.environ.get("GO_PROXY", "http://127.0.0.1:7892")
STALL_SECONDS = 20  # 这么多秒还没下到 1MB 就认为这条线路不通

GH = "https://github.com/lightvector/KataGo/releases/download"
RAW = "https://raw.githubusercontent.com/lightvector/KataGo"

# (url, 落盘文件名, 精确字节数或 None, vendor 下的子目录)

# 两个模型两个平台通用
MODEL_DOWNLOADS = [
    (
        f"{GH}/v1.15.0/b18c384nbt-humanv0.bin.gz",
        "b18c384nbt-humanv0.bin.gz", 99_066_230, "models",
    ),
    (
        "https://katagoarchive.org/g170/neuralnets/"
        "g170-b15c192-s497233664-d149638345.bin.gz",
        "g170-b15c192-s497233664-d149638345.bin.gz", 36_934_186, "models",
    ),
]

# Windows 下两个引擎。DirectML 走 DirectX 12，不碰 OpenCL —— 这台机器的 OpenCL
# 后端一搜索就报 CL_OUT_OF_RESOURCES（显存其实是空的，是驱动层的问题，配置救不了），
# 所以干脆不下 OpenCL 版，下一个在这台机器上必崩的东西只会让人困惑。
WINDOWS_DOWNLOADS = [
    (
        f"{GH}/v1.18.1/katago-v1.18.1-onnx1.24.4-directml-windows-x64.zip",
        "katago-directml.zip", 23_584_379, "engines/directml",
    ),
    (
        f"{GH}/v1.18.1/katago-v1.18.1-eigenavx2-windows-x64.zip",
        "katago-eigen.zip", 5_899_607, "engines/eigen",
    ),
]

# macOS 不下引擎：KataGo 官方从不发 macOS 预编译包（61 个 release 一个都没有），
# 只能 brew install katago。但配置文件必须下 —— brew 那个包只给一个二进制，
# 不带 gtp_human5k_example.cfg，而 humanSLProfile / delayMove 这些关键设置都在里面。
# 跟 Windows 共用同一份（v1.18.1 的），省得两边设置各自漂移。
MAC_DOWNLOADS = [
    (
        f"{RAW}/v1.18.1/cpp/configs/gtp_human5k_example.cfg",
        "gtp_human5k_example.cfg", None, "engines/macos",
    ),
]

# 内置的自然音：三段实录，做背景声垫在棋局底下。
#
# 来源是 archive.org，item 级标的就是 CC0 1.0（等于公有领域，不用署名、可商用）。
# 只下 MP3 不下 Ogg 是有意的：Safari 不支持 Ogg Vorbis，而这个项目有 macOS 版，
# 挑 Ogg 会让 Mac 上静默没有自然音。为此多花一倍体积认了。
#
# 下完落在 vendor/sounds/，程序第一次启动时由 server.seed_nature() 拷进
# 数据目录的 nature/。那里用户能随手删改，见那个函数的注释。
SOUND_DOWNLOADS = [
    (
        "https://archive.org/download/relaxingrainsounds/Rain%20Sounds.mp3",
        "雨.mp3", 6_289_928, "sounds",
    ),
    (
        "https://archive.org/download/naturesounds-soundtheraphy/"
        "Birds%20With%20Ocean%20Waves%20on%20the%20Beach.mp3",
        "海浪.mp3", 20_288_176, "sounds",
    ),
    (
        "https://archive.org/download/naturesounds-soundtheraphy/"
        "Relaxing%20Nature%20Sounds%20-%20Trickling%20Stream%20Sounds"
        "%20%26%20Birds.mp3",
        "溪流.mp3", 16_909_936, "sounds",
    ),
]

ENGINES = ["macos"] if IS_MAC else ["directml", "eigen"]
DOWNLOADS = (MODEL_DOWNLOADS + SOUND_DOWNLOADS
             + (MAC_DOWNLOADS if IS_MAC else WINDOWS_DOWNLOADS))
MODELS = [d[1] for d in MODEL_DOWNLOADS]
SOUNDS = [d[1] for d in SOUND_DOWNLOADS]


def _fetch(url, dest, via_proxy):
    """流式下载到 dest。socket 超时 15 秒，卡住就抛异常由调用方换线路。"""
    handlers = []
    if via_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "go-setup/1.0"})

    with opener.open(req, timeout=15) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        started = time.time()
        next_tick = 10
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            out.write(chunk)
            got += len(chunk)
            if got < 1_000_000 and time.time() - started > STALL_SECONDS:
                raise TimeoutError(f"太慢：{STALL_SECONDS} 秒只下了 {got} 字节")
            if total:
                pct = got * 100 // total
                # 只在跨过 10% 刻度时打一行。用 \r 原地刷新好看，但输出被重定向到
                # 文件时几百次刷新会糊成一条巨长的乱码行，不值当。
                if pct >= next_tick:
                    next_tick = pct - pct % 10 + 10
                    print(f"    {pct:3d}%  {got/1e6:6.1f} / {total/1e6:.1f} MB", flush=True)


def download(url, name, expect, subdir):
    folder = VENDOR / subdir
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name

    if dest.exists() and (expect is None or dest.stat().st_size == expect):
        print(f"[跳过] {name}")
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    for via_proxy in (False, True):
        route = f"代理 {PROXY}" if via_proxy else "直连"
        print(f"[下载] {name}  ({route})", flush=True)
        try:
            _fetch(url, tmp, via_proxy)
            actual = tmp.stat().st_size
            if expect is not None and actual != expect:
                raise ValueError(f"大小不符：{actual} != {expect}")
            tmp.replace(dest)  # 校验过了才改名，中断不会留下半个“完整”文件
            return dest
        except Exception as exc:
            print(f"    失败（{route}）：{exc}")
            tmp.unlink(missing_ok=True)

    raise SystemExit(f"两条线路都没下成：{name}\n请手动下载后放到 {folder}/")


def main():
    for url, name, expect, subdir in DOWNLOADS:
        path = download(url, name, expect, subdir)
        if path.suffix == ".zip":
            target = path.parent
            if (target / KATAGO_BIN).exists():
                print(f"[跳过] {name} 已解压")
            else:
                print(f"[解压] {name}")
                with zipfile.ZipFile(path) as zf:
                    zf.extractall(target)

    # 只用 ASCII 标记。Windows 控制台默认 GBK，✓/✗ 这类符号会直接抛
    # UnicodeEncodeError 把脚本搞崩 —— 明明活都干完了。
    print()
    for name in ENGINES:
        folder = VENDOR / "engines" / name
        exe = folder / KATAGO_BIN
        if exe.exists():
            where = str(exe.relative_to(ROOT))
        else:
            # Mac 上 katago 是 brew 装的，只会出现在 PATH 上，不在 vendor 里
            found = shutil.which(KATAGO_BIN)
            where = f"{found} (PATH)" if found else ""
        cfgs = sorted(p.name for p in folder.glob("*.cfg"))
        print(f"引擎 {name:<7}", f"[ok] {where}" if where else "[!! 没找到]",
              f"配置 {len(cfgs)} 个")
    for name in MODELS:
        p = VENDOR / "models" / name
        print(f"模型 {p.name[:44]:<44}", "[ok]" if p.exists() else "[!! 没找到]")
    for name in SOUNDS:
        p = VENDOR / "sounds" / name
        print(f"自然音 {p.name:<44}", "[ok]" if p.exists() else "[!! 没找到]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
