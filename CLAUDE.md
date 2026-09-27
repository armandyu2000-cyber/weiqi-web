# CLAUDE.md — 围棋（weiqi-web）

本地网页版围棋。对手是 KataGo，18 档人机棋力（业余 9 级 ~ 9 段），也能让黑白各选各的
档位互下。零第三方依赖，全标准库。

公开库：<https://github.com/armandyu2000-cyber/weiqi-web>　面向用户的完整文档在
[`README.md`](README.md)，本文件只写「改代码前必须知道的」。

## 跑

```bash
python setup.py                              # 首次：下引擎/模型/自然音（约 180MB），可重复执行
python server.py                             # 启动，http://127.0.0.1:8731/

python -m unittest test_rules test_server    # 182 个测试，约 0.5 秒，不碰 GPU
python e2e_check.py                          # 真引擎端到端，几十秒，不进日常测试
```

`test_server.py` 用假引擎，随便跑；`e2e_check.py` 要真引擎和 GPU，改引擎交互才跑。

## 分层与边界

| 文件 | 职责 |
|---|---|
| `rules.py` | 围棋规则：气 / 提子 / 自杀 / 劫 / 数子。纯函数，不碰引擎 |
| `katago.py` | GTP 客户端：引擎当子进程，stdin/stdout 收发 |
| `server.py` | HTTP 服务 + 对局状态（`Session` 类）。**唯一有状态的地方** |
| `web/board.js` | 画棋盘、报点击，不管对局逻辑 |
| `web/app.js` | 页面状态与交互，不重写围棋规则 |
| `web/audio.js` | 背景音 |

**引擎不是裁判。** 合法性和胜负全归 `rules.py`。实测 KataGo 的 `play` 不检查轮次
（黑连下两手会被接受），`final_score` 在中国规则下返回的是神经网络估算值而不是数子结果。
引擎只回答「你觉得这手该下哪」。

## 设计要点

**两种模式共用一个 `Session`。** `machine_play` 一个布尔量切换人机 / 机机，不是两套代码。
切模式、切执色都**保留棋局**不重开。两个模式各记各的档位，切走再切回来原样还给你。

**前端一手一请求。** 机机对局是前端驱动的循环，不是后端线程 —— 所以暂停 / 继续天然就有，
不用加推送。代价：关掉标签页这盘就停在那儿。

**复盘是纯前端计算。** 服务端返回的每一手都带「被提掉的子」，所以看第 N 手只要从空盘
往前放一遍，前进后退不问服务器。

**存档 id 是秒级的。** 「刚存完就从某一手重开」会撞上同一秒，`save()` 撞了必须往后加一位
—— 不然接续的那局会**盖掉原局**。

**数据目录三个来源**（`_data_dir()`）：`GO_DATA_DIR` 环境变量 > 冻结态走用户目录
（macOS `~/Library/Application Support/Go`，Windows `%LOCALAPPDATA%\Go`）> 源码运行走项目目录。
打包后存档不写进 bundle —— 那里可能是只读的。**代码和模型永远从 `ROOT` 读**，只有写出
去的东西才跟着 `DATA` 走。

## 别踩（都是实测出来的，改之前先读）

- **`kata-set-params`（复数）会把引擎整个搞崩**，进程直接退出。只能 `kata-set-param` 逐个设。
- **档位的杠杆不是 `maxVisits`。** 要让搜索真参与选点，得同时把 `PiklLambda` 从 1e8 压到
  0.08、把 `RootExploreProbWeightless` 从 0 抬到 0.8。级位档搜十几个 visit 就收手是
  **设计如此**，不是 bug。
- **测档位别用 `genmove`。** 它每问一次就落一子，于是每个档位测的是不同局面，数会乱跳。
  用 `kata-search_analyze`（只搜不下，局面固定）。
- **别用 `kata-search`。** 它给的是采样手，同局面连问两次会变。`kata-search_analyze`
  的 info 行里 order 0 才是确定的。
- **`-override-config` 写错 key 只往 stderr 嘟囔一句然后装没事**，而 stderr 被吞了。
  所以 `e2e_check.py` 第 0 节回读 `kata-get-param` 确认 —— 那条挂了就说明延迟没真关掉。
- **引擎 stderr 绝不能并进 stdout。** 启动横幅是非 GTP 文本，会把读循环噎住。
- **CSS 里必须有 `[hidden] { display: none !important }`。** `.levels` / `.row` 显式设了
  `display`，类选择器会盖掉 `[hidden]` 自带的 `display: none`，于是 `el.hidden = true`
  一点效果都没有。这个坑不报错，只让该藏的按钮继续杵在那儿。
- **Windows 上 OpenCL 后端是坏的**（`CL_OUT_OF_RESOURCES`，而显存其实是空的），别调参
  去救，用 DirectML。**线程数保持 cfg 里的 1** —— DirectML 下 8 线程反而更慢。
- **高段位不保证比低段位强**（实测见过 7 段输 5 段）。human SL 是按「某段位的人会怎么下」
  采样的，不做强度校准。这是预期内的，不是 bug。

## 平台

`katago.ENGINE` 按平台自动挑，**不用手改**：Windows → `directml`，macOS → `macos`。

macOS 那项的可执行文件不在 `vendor/` 里 —— KataGo 官方从不发 macOS 预编译包
（v1.0~v1.18.2 全部 61 个 release，一个 mac 资源都没有），只能 `brew install katago`，
所以 `engine_paths()` 会退回 PATH 上找。**配置文件两个平台都必须下**：brew 那个包只给
一个二进制，不带 `gtp_human5k_example.cfg`，而关键设置全在里面。

**整条 Mac 路径至今没在真机上验证过**（`macos/README-macOS.md` 里挂着三个 ⚠️：
brew 的 katago 走哪个后端、每手多少秒、Gatekeeper 拦不拦）。`macos/首次运行.md` 是给
作者本人的取数清单 —— 有 Mac 可跑时照那份走，跑完把结果回填进 `README-macOS.md`。
Mac 上**做不出**能拷给别人的安装包：引擎必须在目标机上 brew 现场编译。

## 不进仓库的目录

`vendor/`（引擎 + 模型 + 自然音，`setup.py` 下载）、`dist/` `build/`（打包产物）、
`games/` `gtp_logs/`（运行产物）、`nature/`（运行时副本）、`docs/superpowers/`，
以及 **`joseki/`** —— 两本第三方定式辞典 SGF（石田芳夫 / Kogo's），版权不在本项目。
`joseki/` 在代码里**零引用**，删掉不影响运行。见 `.gitignore`。

## 约定

- 注释写「为什么」，不写「做了什么」。反直觉的决定要附实测数据 —— 这个项目里绝大多数
  注释都是这个性质，**别当成冗余删掉**。
- 用户可见的输出（启动脚本的 `print`）只用 ASCII。Windows 控制台默认 GBK，`✓` / `✗`
  会抛 `UnicodeEncodeError`，把干完活的脚本搞崩。
- 公开文档一律中文。
