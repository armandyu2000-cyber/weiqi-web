"""打一个双击就能跑的 Windows 程序。产物在 dist\\围棋\\ 里，双击 围棋.exe 即可。

跑法：python windows\\build_exe.py

改完源码要重新跑一次 —— exe 里装的是打包那一刻的代码副本，不会跟着源码走。

用 Python 写而不是 .bat：exe 名字里有中文，.bat 要迁就 cmd 的代码页，很容易
在别人机器上变成乱码文件名。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "围棋"
DIST = ROOT / "dist"

# 塞进 exe 的目录：web 是前端三件套，vendor 是模型、引擎和内置自然音。
# 路径必须原样保留 —— katago.py / server.py 是按「跟 .py 同级」来找它们的，
# 而打包后它们被放在同一个 _internal 目录里，正好还对得上。
#
# 只带 directml 一个引擎：这台机器的 RTX 3060 上 OpenCL 后端一搜索就崩
# （见 katago.py 顶上的说明）。Eigen 是纯 CPU 的退路，占 20MB，用不上就不带。
BUNDLE = ["web", "vendor/models", "vendor/engines/directml", "vendor/sounds"]


def main():
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        # 留控制台窗口：出错总得有个地方看。双击启动没有终端的话，服务起不来
        # 就是「点了没反应」，什么都查不了。
        "--console",
        "--distpath", str(DIST),
        # 变异文件和 .spec 都塞进 build/，别让项目根目录变乱
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--exclude-module", "tkinter",
    ]
    for folder in BUNDLE:
        args += ["--add-data", f"{ROOT / folder}{';'}{folder}"]
    args.append(str(ROOT / "server.py"))

    print("开始打包……（要塞 190MB 的模型，要几分钟）")
    done = subprocess.run(args, cwd=ROOT)
    if done.returncode != 0:
        print(f"\n打包失败，PyInstaller 退出码 {done.returncode}")
        return done.returncode

    # setup.py 下载完把压缩包留在了原地，会被原样搬进去 —— 装好的东西不需要
    # 自己的安装包，白占 23MB。
    for junk in (DIST / NAME / "_internal" / "vendor").rglob("*.zip"):
        junk.unlink()

    return report()


def report():
    """打完先自己检查一遍：缺东西的话现在说，别等双击了才发现。"""
    app = DIST / NAME
    exe = app / f"{NAME}.exe"
    missing = []
    if not exe.exists():
        missing.append(f"{exe}（主程序没生成）")
    for need in ["_internal/web/index.html",
                 "_internal/vendor/models/b18c384nbt-humanv0.bin.gz",
                 "_internal/vendor/models/g170-b15c192-s497233664-d149638345.bin.gz",
                 "_internal/vendor/engines/directml/katago.exe",
                 "_internal/vendor/engines/directml/gtp_human5k_example.cfg",
                 "_internal/vendor/sounds/雨.mp3"]:
        if not (app / need).exists():
            missing.append(need)

    if missing:
        print("\n打包完了但缺东西，双击也跑不起来：")
        for m in missing:
            print("  -", m)
        return 1

    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"\n好了：{exe}")
    print(f"大小 {size / 1024 / 1024:.0f}MB。双击那个 exe 就能下棋。")
    print(f"整个 {NAME} 文件夹可以随便挪（桌面、U 盘都行），但别只挪 exe 一个。")
    print("存档和对弈记录写在 %LOCALAPPDATA%\\Go\\，重新打包不会丢。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
