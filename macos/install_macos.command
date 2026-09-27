#!/bin/bash
# 在 macOS 上把本项目装成一个双击就能跑的 .app。
#
# 用法：chmod +x macos/install_macos.command && ./macos/install_macos.command
#       之后双击它也行（.command 在 Finder 里就是双击运行的）。
#
# 可重复执行：已经装过就重装一遍，改了代码重跑这个就生效。
#
# 为什么不打包 Python：这个项目零第三方依赖，全用标准库，所以系统/brew 的
# python3 直接就能跑。真正没法自带的是 KataGo 引擎 —— 官方从不发 macOS 预编译
# 包，必须在目标机器上编译（brew install katago）。

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
APP_NAME="围棋"
BUNDLE_ID="local.weiqi-web"

# 装在 ~/Applications 而不是项目目录里，这一步是有讲究的：
# .app 里面有指向项目目录的软链，要是 .app 本身也在项目目录里，就成环了
# （项目 -> .app -> 项目），任何递归遍历 —— 备份、同步工具、find -L —— 都会绕不出来。
# 放外面就断了。~/Applications 也不需要管理员权限。
APP="$HOME/Applications/$APP_NAME.app"

say() { printf '%s\n' "$*"; }
die() { printf '\n[停] %s\n' "$*" >&2; exit 1; }

say "项目目录：$PROJECT"
say ""

# --- 1. 检查依赖 ------------------------------------------------------------

if ! command -v katago >/dev/null 2>&1; then
  for c in /opt/homebrew/bin/katago /usr/local/bin/katago; do
    [ -x "$c" ] && PATH="$(dirname "$c"):$PATH" && break
  done
fi
command -v katago >/dev/null 2>&1 \
  || die "找不到 katago。先在终端里跑：brew install katago"

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
[ -n "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "找不到 python3。先在终端里跑：brew install python"

say "引擎    ：$(command -v katago)"
say "Python  ：$PY  ($("$PY" -V 2>&1))"

# --- 2. 下模型和配置（已经下过就跳过）---------------------------------------

say ""
say "下载模型、配置和内置自然音（第一次要下约 180MB）……"
( cd "$PROJECT" && "$PY" setup.py ) || die "setup.py 没跑通，看上面的报错"

# --- 3. 拼 .app -------------------------------------------------------------

say ""
say "生成 $APP_NAME.app ……"
mkdir -p "$HOME/Applications"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 代码整份软链进去，不复制 —— 模型有 136MB，复制一份纯属浪费；而且软链的话
# 改了代码立刻生效，不用重装。项目挪了地方就重跑一次本脚本。
# 先删再建，不用 ln -f/-n：目标不存在时那几个开关的语义各平台不一致。
rm -rf "$APP/Contents/Resources/app"
ln -s "$PROJECT" "$APP/Contents/Resources/app"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleExecutable</key><string>Go</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cp "$HERE/Go" "$APP/Contents/MacOS/Go"
chmod +x "$APP/Contents/MacOS/Go"

# 本地的包加个隔离标记会让 Gatekeeper 弹「来自身份不明的开发者」。
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

say ""
say "装好了：$APP"
say "去「应用程序」文件夹双击它，或者直接：open \"$APP\""
say ""
say "存档 / 对弈记录 / 启动日志在："
say "    $HOME/Library/Application Support/Go"
say ""
say "退出：网页里点「退出程序」，或者活动监视器里结束 python3。"
