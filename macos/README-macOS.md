# 在 macOS 上装这个围棋程序

> **说明**：这份文档里的步骤，作者只验证过脚本的语法和换行符，**没有在真 Mac 上跑过**
> （打包机的开发环境是 Windows）。按顺序做完大概 10 分钟，卡住了把报错贴出来。

---

## 为什么不能直接给你一个 .app

两个绕不过去的限制：

1. **macOS 的可执行文件没法在 Windows 上生成。** PyInstaller / py2app 都不是交叉编译器，
   苹果的签名和公证更是必须在 macOS 上做。
2. **KataGo 官方从来不发 macOS 预编译包。** 把 v1.0 到 v1.18.2 全部 61 个 release 的
   资源清单扫过一遍，`mac`/`osx`/`darwin` 匹配数是 **0** —— 只有 linux-x64 和 windows-x64。
   所以 Mac 上必须现场编译（Homebrew 帮你编）。

第 2 条决定了 .app 里**没法自带引擎**，只能去调 brew 装的那个。好在 Python 这边
这个项目零第三方依赖、全用标准库，所以也不用打包 Python。

结论：这里给你的是一个**双击就能跑的 .app**，它内部指向项目目录，引擎和 Python 来自 brew。

---

## 第 1 步：装 Homebrew（已经装过就跳过）

终端里跑：

```bash
brew --version
```

没反应或者提示 command not found，就去 <https://brew.sh> 照它首页那行命令装。

## 第 2 步：装引擎和 Python

```bash
brew install katago python
```

这一步**要从源码编译 KataGo，会比较久**（十几分钟到半小时，看你机器）。

装完确认一下：

```bash
katago version
python3 -V
```

两个都有输出就对了，`katago version` 应该报 `1.18.2` 左右。

> ⚠️ **一个还没验过的点**：Homebrew 编出来的 katago 用哪个计算后端（Metal / OpenCL /
> 纯 CPU 的 Eigen）我在这边没法确认。如果是纯 CPU，每手可能要十几秒。真是那样的话
> 告诉我，我把 `maxVisits` 调低或者换个更小的模型。

## 第 3 步：把项目拷到 Mac

整个 `Go` 文件夹拷过去。**建议连 `vendor/models/` 一起拷**（两个模型共 136MB）——
拷过去的话第 4 步的下载会自动跳过；不拷也行，它会自己下。

可以不拷的：`vendor/engines/`（那是 Windows 的 50MB 二进制，Mac 上用不上）、
`__pycache__/`、`gtp_logs/`。

拷贝方式随意：移动硬盘、`scp -r`、rsync 都行。

> AirDrop 或浏览器下载会给文件打上隔离标记，第一次双击可能被 Gatekeeper 拦。
> 见下面「出问题」一节。

## 第 4 步：生成 .app

在项目目录里：

```bash
chmod +x macos/install_macos.command
./macos/install_macos.command
```

第一次跑要下约 180MB（两个模型 136MB + 三段自然音 43MB，进度会打百分号），
跑完装到 **`~/Applications/围棋.app`**。

如果它报「找不到 katago」，说明 brew 装的位置不在脚本找的那两个目录里，把
`which katago` 的输出发我。

> 装在 `~/Applications` 而不是项目目录里是有讲究的：.app 内部有指向项目目录的
> 软链接，要是 .app 本身也在项目里就成环了（项目 → .app → 项目），备份和同步
> 工具会绕不出来。放外面就断了。这个位置也不需要管理员权限。

## 第 5 步：用

去**应用程序**文件夹双击 `围棋`。

- 第一次启动要十几秒——引擎在加载 120MB 的神经网络，这是正常的。
- 起来之后会自动打开浏览器。**如果浏览器没自动开**，手动访问
  <http://127.0.0.1:8731/>
- 网页里点「退出程序」关掉后台服务。双击启动没有终端窗口，所以只能这么退。

---

## 东西都放哪

| 内容 | 位置 |
|---|---|
| .app | `~/Applications/围棋.app` |
| 存档（棋谱） | `~/Library/Application Support/Go/games/` |
| 对弈记录 | 同上，`history.jsonl` |
| 启动日志 | 同上，`启动日志.txt` |
| 程序代码 | 项目目录（原处，没被复制） |

存档不放进 .app 里面是有意的：bundle 里可能是只读的，Mac 还会把没签名的包挂到
随机只读路径上跑，写进去的棋谱会莫名其妙不见。

`.app` 里是指向项目目录的**软链接**，所以：

- **改了代码不用重装**，重开一次就生效。
- **项目挪了地方**，得重跑一次第 4 步。

## 出问题

**双击没反应 / 弹「来自身份不明的开发者」**

终端里跑一句去掉隔离标记：

```bash
xattr -dr com.apple.quarantine ~/Applications/围棋.app
```

或者右键点它 → 打开 → 再点「打开」。

**双击了但浏览器一片空白 / 连不上**

看日志：

```bash
open ~/Library/Application\ Support/Go/启动日志.txt
```

或者干脆在终端里手动跑，这样报错直接打在屏幕上：

```bash
./macos/Go
```

（`macos/Go` 就是 .app 里的那个启动器，逻辑一模一样。）

**想完全卸载**

```bash
rm -rf ~/Applications/围棋.app
rm -rf ~/Library/Application\ Support/Go   # 里面有棋谱，删之前先拷出来
```
