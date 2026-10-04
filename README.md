# ComfyUI 服务器部署套件（无 GPU / API 客户端模式）

你的服务器没有 GPU，ComfyUI 只当「节点画布 + AI 反代客户端」用：
生图请求走你配置的 API（New API / Gemini / Grok 等），本机只跑界面和节点逻辑。

## 文件

```
(仓库根目录)
  deploy.sh    一键部署脚本（交互菜单 + 非交互参数）
  Dockerfile   CPU 版 PyTorch 镜像（无 CUDA，省 ~4GB）
  .dockerignore  构建只带 requirements，不把 data/ backups/ 打进构建上下文（传服务器别漏了这个点文件）
  workflows/     内置工作流（11 个，按任务归并：同一任务的不同引擎 / 模式放进一个工作流，画布顶部下拉切换），
                 按类别放在子目录里（1-图片 / 2-文案与配音 / 3-视频），部署时保持子目录铺进 ComfyUI，已存在的不覆盖；文档里按编号 01~11 称呼
  custom-nodes/  本套件自带并自己维护的插件和节点（启动容器前铺进 data/custom_nodes，已存在的不覆盖）：
                 ComfyUI-relayapi（第三方插件的拷贝，含本地修改，见其 UPSTREAM.md）、pro-gemini-music、pro-poster（海报提示词 / 海报文案拆分）、
                 pro-ali（阿里生图/改图/配音/听写/写提示词/音色设计/声音克隆/音色管理）、pro-video（视频配音合成/取音轨/字幕生成/烧字幕/图片轮播）、
                 pro-image（促销标签叠加）、pro-flow（模式 / 选择分支 / 开关门：一个工作流里选引擎和模式）
  assets/        随套件铺进 data/input 的素材（无背景音乐.wav：08/10/11 默认 BGM，静音占位；请上传视频.mp4：11 里「上传视频」节点的占位，
                 ComfyUI 提交前会校验所有连着的节点，包括没选中的那一路，所以占位文件必须存在）
  tools/         gen_workflows.py：workflows/*.json 的生成脚本（改模板改这里，再 python3 tools/gen_workflows.py）
                 batch_run.py：按 CSV 批量跑任意工作流（见「批量跑」）
```

## 怎么用（在服务器上）

```bash
git clone https://github.com/zxc1136111473-ui/pro-cui.git && cd pro-cui   # 在服务器上
bash deploy.sh                    # 交互菜单
bash deploy.sh --install -y --port 8188   # 一条命令装完
```

装完访问 `http://服务器IP:8188/`。

## 菜单功能

```
1) 全新安装 / 重新部署（容器只有一个，换目录会替换现有这套）
2) 更新到最新版（先预览新提交和风险，确认后 git pull + 插件更新 + 重建镜像 + 重建容器）
3) 体检（访问地址、torch 是否 CPU 版、前端等 comfy 包版本和源码要求是否一致、源码是否在分支上）
4) 改监听端口
5) 配 HTTPS + 访问密码（复用香水商城 Caddy，公网只走域名）
6) 插件管理（汉化 / APIimage / relayapi / 自定义 / 检查插件依赖）
7) 数据说明（源码在哪、怎么改代码、怎么接反代）
8) 一键备份（data/ 含插件/工作流/出图 + 配置 → backups/，只留最近 5 份；可选不含出图）
9) 一键恢复（先校验备份包；端口和绑定方式保留本机当前的，本目录原来没配置时沿用备份里的）
10) 卸载（a 保留目录 / b 连目录删，backups/ 也会删）
11) 测试反代（容器 → New API 连通 + 令牌能用哪些出图模型）
12) 回到上次更新前（更新后运行出问题时用，按旧依赖重建镜像）
13) 空间占用（出图 / 上传 / 工作流 / 插件 / 备份 / 镜像各占多少，只看本项目）
l) 看日志（最近 200 行 / 实时跟随 / 只看本次启动的问题）
r) 重启容器（docker restart，不删容器，约 1 秒）   R) 重建容器（删了重新 run）   0) 退出
```

几个会改变实际行为的数值写在 `deploy.sh` 开头：探测超时 5 秒、日志轮转 10MB×3（只作用于这个容器）、
备份保留 5 份、更新预览每个仓库显示 3 条提交。要改先想清楚影响。

重启、重建、更新、恢复、改端口、改绑之前，会先看有没有任务在跑：有就提示「重启会丢掉它们，已发给 New API 的请求照样计费」，
默认不继续（`-y` 照常执行）。要重建镜像的（更新、回退、重新部署），构建完、删容器前会再查一次：
这时取消，镜像和代码都退回旧版，旧容器不动。
`--check` 在容器没跑或页面不通时返回非零（`--json` 的 ok 为 false），外部监控可以直接调。

**没有终端时**（cron、`ssh 主机 命令` 不带 `-t`、`nohup … &` 后台）：每个提问都按默认值走，默认「否」的不会执行，
所以有风险的更新、有任务时的重启、卸载都会自动取消（日志里能看到「没有终端，按默认」）；要强制执行加 `-y`。
没有终端时也进不了菜单，要用参数指定动作（`--update` / `--check` / `--backup` …）。

## 自检

```bash
python3 -m unittest discover -s tests    # 工作流与生成脚本是否一致、连线是否完整、字幕等纯函数
bash -n deploy.sh && shellcheck -S warning deploy.sh
```

改了 `tools/gen_workflows.py` 要先 `python3 tools/gen_workflows.py` 重新生成再提交，否则第一项会报。

## 核心设计（为什么这么做）

1. **官方主仓库，不拉分支。** 源码 git clone 到 `app/` 并挂载进容器——
   - 改代码：直接编辑 `app/` 下文件 → `docker restart comfyui` 生效，不用重建镜像
   - 更新：菜单 2（git pull 源码和插件 + 重建镜像，新版依赖才装得上）
   - 加功能优先用**插件**（custom node），不要改源码，更新不冲突

2. **数据目录全部挂载**（`data/`），容器重建不丢：
   - `data/models/` 模型文件（走 API 不需要；本地推理就放这）
   - `data/custom_nodes/` 插件（git clone 进来 = 安装）
   - `data/output/` 出图、`data/input/` 上传参考图
   - `data/user/` 网页里保存的工作流、界面设置、Manager 配置（一键备份会带上）
   - 从旧版套件升级：旧的出图、上传图、工作流还在 `app/output`、`app/input`、`app/user`，挂载后界面里看不到，需要的话手动搬：
     `cp -an app/user/. data/user/`（output / input 同理），再菜单 r 重启

3. **CPU 版 PyTorch 镜像**：无 CUDA，省 ~4GB 体积和显存需求。

4. **插件**（菜单 6 一键装，导入所需依赖已预装进镜像；例外：essentials 的抠图 / LUT / 像素化 / Seam Carving 节点缺 rembg 等依赖，不可用）：
   - 1 必备：`ComfyUI-Global-Translation`（汉化）、`ComfyUI-APIimage`（OpenAI 兼容出图/改图/局部重绘）、`ComfyUI-relayapi`（gemini 走 chat 出图，也能出视频）
   - 2 工具：`rgthree-comfy`（组静音、只跑选中输出、图片对比）
   - 3 可选：`ComfyUI-Custom-Scripts`、`ComfyUI_essentials`、`ComfyUI-VideoHelperSuite`
   - 不再预置：`ComfyUI-Logic`（已归档，核心自带 Switch/And/Or/Not）、`ComfyUI-Impact-Pack`（依赖本地模型）。已装的用菜单 6 → 7 卸载
   - 菜单 6 → 6 列出时带 `*` 的插件本地改过文件（例如汉化插件的开关会写它的 `config.json`）。
     更新时会自动暂存、拉完放回；和上游冲突时改用上游版本，你的改动留在该插件目录的 `git stash` 里（需要 git 2.33+，Ubuntu 22.04 起满足）

5. **ComfyUI-Manager 已内置**：官方改成 pip 包（版本跟 ComfyUI 源码里的 `manager_requirements.txt`），
   启动带 `--enable-manager`，不再 git clone；旧的 `custom_nodes/ComfyUI-Manager` 会被自动跳过，可以删掉。
   以 0.0.0.0 监听时（默认 `network_mode=public`），网页 Manager **装不了任何插件**（Registry 的也不行），
   只能更新 / 禁用 / 卸载已装插件和重启；装插件用菜单 6。确实要在网页里装，把 `data/user/__manager/config.ini`
   的 `network_mode` 改成 `personal_cloud`（等于任何能访问端口的人都能装插件）。
   **别用网页 Manager 的「更新 ComfyUI」**：它会把源码切到某个 tag（不在分支上）且不重建镜像，变成新源码配旧依赖，
   之后菜单 2 也拉不动。更新一律走菜单 2；体检（菜单 3）会查出这种情况。

6. **CPU 版 torch 锁死**：镜像用 `PIP_CONSTRAINT` + `UV_CONSTRAINT`（Manager 默认用 uv）锁住 torch / torchvision / torchaudio，
   插件依赖要求别的 torch 版本时会直接报错，不会悄悄换成几 GB 的 CUDA 版。
   新插件有 pip 依赖时：菜单 6 → 8 按镜像查缺哪些包、会改哪些已装包的版本（只看不装），写进 Dockerfile 后菜单选 1（回车保持原设置，只重建镜像、不拉代码）。
   菜单 1 / R / 2 / 4 / 5（改绑时）/ 9 / 12 都是删掉容器再新建，现场（含 Manager 里）装的包会丢；
   r 和装完插件后的重启只是 `docker restart`，包还在。容器用 `--stop-signal SIGINT`，ComfyUI 收到后正常退出，
   不用等 Docker 默认的 10 秒超时（这个设置在建容器时生效，老容器要菜单 R 或 2 重建一次）。

7. **升级部署套件**（换了新的 deploy.sh / Dockerfile）后先跑菜单 2：安装和更新会重建镜像，
   r / R / 4 / 5 / 6 / 9 不会，会继续用旧镜像。只换了套件、不想拉 ComfyUI 新代码时选菜单 1。旧镜像重建后自动删除。

8. **更新前预览、失败自动退回、可以回到上次更新前**：
   - 菜单 2 先 `git fetch`（不合并），列出每个仓库主线上的新提交，并标出风险：插件依赖变了；本地改过的文件上游也改了
     （插件可能冲突，冲突时用上游版本、改动进 stash；源码 pull 必然失败，要先 stash 或提交）；本地和上游分叉；源码不在分支上；
     fetch 失败（显示真实原因）。有风险默认不继续。
   - 构建镜像失败时，旧容器还在跑，脚本把源码和插件退回更新前的 commit，服务仍是旧版。
   - 构建成功但运行才出问题（插件加载失败、前端回归）：菜单 12 回到上次更新前，按旧依赖重建镜像。
     只记最近一次真正拉到新代码的更新（`.last-update`，构建前就写好，Ctrl+C 中断也能退）。回退时如果构建失败，
     仓库会恢复到回退前（和还在跑的容器一致），网络好了再选 12。新版如果升级过数据库，旧版日志里可能报 DB 错误，
     工作流不受影响；回退前会把 `comfyui.db.bkp` 另存为 `data/user/comfyui.db.pre-update`（旧版启动时会覆盖并删掉 .bkp）。

## 内置工作流、自带节点和补丁

**Key 存哪**：模板里不带 Key。每个 Relay API Settings 节点按**节点 id** 在服务器 `relay_config.json` 里取 Key（首次也可以在节点的 apikey 填一次，之后记住）：
`1` 网关（gemini-image 出图 / 文字）、`11` geminiweb（视频、Gemini 音乐）、`21` Suno、`31` 阿里百炼（DashScope，国内版 `dashscope.aliyuncs.com`）。

**老安装升级**：以前铺进去的是 26 个旧编号的工作流（含平铺文件和 1-一条龙 … 6-视频 六个子目录），新版是 11 个（1-图片 / 2-文案与配音 / 3-视频）。
部署只会铺入不存在的文件，旧的不会自动删，会和新的并存：把旧的子目录移走即可（在网页里改过的先另存）。旧→新对照见下表「合并自」。

**怎么用（所有工作流都一样）**：画布最上面是两个带 ★ 的节点——**「★ 先选模式」**（中文下拉：选引擎 / 选做法）和**「★ 只填这里：一句话需求」**
（`pro-ali` 的「阿里 写提示词」：第一个框填需求，第二个框是扩写指令，一般不用改）。选好模式、填好一句话，点运行。
不同模式用到的节点不一样，**没选中的那一路根本不会运行**（不调接口、不花钱，也不会因为没填它的 Key 报错）；节点标题里写了「只在 XX 模式用」的就是这个意思。

| 编号 | 工作流 | 合并自 | 模式 / 说明 | Key |
|---|---|---|---|---|
| 01 | 文生图 | 旧 01 / 03 / 14 | 引擎：网关 Gemini（自然、约 1K）/ 阿里 qwen-image（2K，中文字准）。比例在各自的生图节点里选（封面横图就选 16:9） | 1、31 |
| 02 | 海报 | 旧 10 / 11 / 17 | 网关不带商品图 / 网关带商品图（上传商品图）/ 阿里 2K 不带商品图。AI 按固定格式填标题、副标题、角标、画面元素，文字仍由 `pro-poster` 用「」原样嵌入；风格在「海报提示词」节点里选 | 1、31 |
| 03 | 商品改图与合成 | 旧 02 / 13 / 15 | 引擎（网关 / 阿里）× 做法（改商品图 / 把商品放进场景图）。一句话说怎么改（换背景、去水印、换色…）或放在哪、多大；「没要求改的部分保持原样」写死在扩写指令里 | 1、31 |
| 04 | 多尺寸套图 | 旧 18 | 同一张商品图、一句话需求（4 个尺寸共用），一次出 1:1 / 3:4 / 9:16 / 16:9 四个平台尺寸（约 75 秒） | 1、31 |
| 05 | 图片后处理 | 旧 06 / 24 | 放大 2 倍（Lanczos + 轻度锐化）/ 贴促销标签（最多 3 个，文字用真字体渲染一字不差）/ 先放大再贴标签。不用 AI、不用 Key | 无 |
| 06 | 商品一条龙 | 旧 12 | 一张商品图 → 白底主图（放大到 2048）+ 场景图 + 标题/卖点文案，一次跑完（约 1 分钟）；场景图那步只填「★ 场景一句话」 | 1、31 |
| 07 | 文案 | 旧 08 / 09 / 21 | 文字写文案 / 看图写文案（上传商品图）/ 把已有文案翻译成英日韩西。阿里 `qwen3.8-omni-flash`；看图前图会自动缩到最长边 768（大图传阿里很慢） | 31 |
| 08 | 配音 | 旧 16 / 25 / 26 | 音色来源：预置（49 个音色，instruct 模型可写语气指令，语速 0.5~2 倍本地变速）/ 设计新音色（文字描述 → 新音色）/ 克隆声音（上传 10~60 秒清晰人声，**只克隆本人或已获授权的声音**）。设计和克隆会在阿里账号里建音色，**没选中就不会运行**；音色 id 可粘进别处的「自定义音色」框 | 31 |
| 09 | 音乐 | 旧 05 / 07 | 引擎：Gemini Lyria（经 geminiweb，约 1 分钟 MP3，成功率约一半）/ Suno（需要 Suno 渠道，目前只用假服务测过连线）。一句话 → AI 写成音乐提示词（纯音乐） | 11、21、31 |
| 10 | 文生视频成片 | 旧 04 / 20 | 只出视频（保留 Veo 自带声音）/ 出视频再配音 + 烧字幕（+ 背景音乐）。Veo 每天约 3 个额度，用尽约 30 秒报错；Google 侧会间歇性卡住 | 11、31 |
| 11 | 视频成片 | 旧 19 / 22 / 23 | 上传视频 + 配音文案（替换原声）/ 上传视频 + 听写字幕（保留原声，阿里 `qwen3-asr-flash`）/ 图片轮播（最多 8 张，推拉 + 淡入淡出，时长跟配音走）+ 配音文案。字幕按配音里的**停顿**对齐并另存 `.srt`（可导入剪映）；不烧字幕时画面流直接复制（逐字节不变） | 31 |

**AI 写提示词**（01 / 02 / 03 / 04 / 06 / 07 / 08 / 09 / 10 带「★ 只填这里」）：阿里 `qwen3.8-flash`（07 用 omni），Settings 节点 id 31，和其他阿里工作流共用 Key。
出错（没填 Key、网络断了）会直接报红，不会拿空提示词去出图；连接失败自动重试一次。每类用不同的扩写指令（生图 / 生视频 / 改图 / 场景合成 / 配乐 / 音色描述 / 写文案 / 翻译），
改图类把「商品保持原样」写死在指令里；海报不让 AI 写整段，只让它填 4 个字段。没有跳过开关：想用自己写好的完整提示词，直接填进「一句话需求」，模型会忠于原意只做补充。
没有提示词可写的（05 后处理、11 视频成片）不带。

**模式节点怎么工作**（`pro-flow`）：「模式」节点输出「走第几路」和随模式变化的开关 / 指令文字；「选择分支」按它取一路输出、「开关门」关着时输出空——
它们都是**惰性**的（ComfyUI 的 lazy 机制），没选中的上游不会被执行。要点：保存 / 预览这类输出节点不能直接接在某一路的节点上（ComfyUI 会为了它强制执行那一路），
要经过选择分支再接；`ProVideoDub` 是唯一允许直接作为最终输出的节点。relayapi 的生成节点失败时媒体输出被 `ExecutionBlocker` 拦住、错误只在 `response` 里，所以「取媒体」和「取状态」是两个选择分支（合在一起状态也会被拦，失败时界面「成功」却什么都没有）。`tests/` 里有一个惰性执行模拟器，会逐个模式检查「没选中的引擎 / 设计 / 克隆 / 轮播不会运行」。

每个 relayapi / 阿里工作流都接了「状态」预览：relayapi 节点出错时不抛异常（作者为批量流程这样设计），错误只在 `response` 输出里，不接出来就会显示「成功」却没有结果。

**批量跑**：`python3 tools/batch_run.py <工作流.json> <表.csv> [--server http://127.0.0.1:8188] [--timeout 600]`。表头写 `节点ID:控件名`（如 `2:image`、`6:text1`，节点 id 看画布左上角/节点属性；`tools/batch_example.csv` 是 05 图片后处理「贴促销标签」的例子），值是本地图片路径会先上传，`out` 列是本行输出前缀（默认 `batch/行号`，落在 `data/output/batch/`）。逐行串行提交，已成功的行记在 `<表>.done`，中断后重跑自动跳过；随机种子每行自动换。转换器已对旧的 24 个模板逐个和前端导出的 API 格式比对一致。**合并后的工作流选模式**：加一列 `77:mode`，值写模式的完整中文名（如 `阿里 qwen-image（2K，中文字准）`）。

**内存与合成**：机器只有约 4GB 内存。ComfyUI 自带的 `GetVideoComponents` / `CreateVideo` 会把整段视频解码成浮点张量（10 秒 720p ≈ 2.6GB），实测**会被系统 OOM 杀掉**，所以视频合成一律用 `pro-video`（PyAV 复制画面包，内存几乎为零）。**烧字幕要中文字体**：容器里没有，`deploy.sh` 启动容器时会把宿主机的 `fonts-noto-cjk`（`/usr/share/fonts/opentype/noto`）只读挂进去；宿主机没装就先 `apt install fonts-noto-cjk`，再用菜单 R 重建容器，否则烧字幕会报「需要中文字体」。`LoadVideo` / `LoadAudio` 只能选 `data/input/` 里的文件：用节点上的上传按钮，或把 04/20 出的视频从 `data/output/` 拷到 `data/input/`。

**阿里接口**：服务器在德国、阿里在北京，链路偶尔慢：大图（1MB 的 PNG）上传曾超过 180 秒，缩到 768 后约 10 秒。阿里节点的超时是 生图 200 秒 / 配音 90 秒 / 下载 60 秒，**不重试**（重试会重复计费）。key 只会发往 `*.aliyuncs.com`。按量计费，2K 大图和 `qwen-image-3.0` 较贵。

**阿里未开通的产品**：MiniMax 配音（`MiniMax/speech-2.8-*`）在模型列表里能看到，但调用返回「产品未开通」，要在百炼控制台开通后才能用；`qwen-mt` 翻译需要另外的参数格式，用通用模型翻译已经够好。阿里听写（ASR）实测 6 秒音频从这台服务器约 20 秒（上传慢）。

**已知限制**：gemini-image 实际只出约 1K（要大图用 01 / 02 的阿里引擎，或 05 放大）；服务器只有 2 核，构建镜像时出图会 503 过载；官方「合作方」节点（走 Comfy 积分）填不了自己的 key，不可用；没有图生视频（Veo 通路没传参考图，阿里 key 里也没有视频模型）；没有抠图/透明底；自定义音色 id 存在阿里账号里，用「阿里 音色管理」节点列出/删除（画布的「音色管理」页就是调它）；11 的图片轮播只有缓慢推拉和淡入淡出两种效果。

**relayapi 由本仓库自己维护**：`custom-nodes/ComfyUI-relayapi/` 是上游（MIT）的拷贝，没有 `.git`，「更新插件」会跳过它；
本地改了一处：插件前端脚本在节点刚加载时会把工作流里保存的比例改成 1:1 / auto（比如 16:9 的封面比例在界面里打开就变 1:1），
现在只有用户之后接上/拆掉参考图才自动切换。来源、基于哪个提交、改了什么都写在 `custom-nodes/ComfyUI-relayapi/UPSTREAM.md`；
`relay_config.json`（存 API Key）已被忽略，不要提交。
老安装（relayapi 是 git clone 来的）不会被覆盖：把 `data/custom_nodes/ComfyUI-relayapi` 换成本目录的版本（保留里面的 `relay_config.json`）。

## 接反代 API

base_url 填 New API **根地址，不要带 `/v1`**（节点自己拼路径）：

| 模型 | 节点 | 填法 |
|---|---|---|
| gpt-image / grok-imagine-image | APIimage「OpenAI Image Generate」 | `base_url: http://<宿主机IP>:13000`，模型名填 `custom_model` |
| gemini-image（走 chat） | relayapi「Relay API Settings」→「Relay Image Generator」 | `custom_api_base: http://<宿主机IP>:13000`，`api_format: v1/chat/completions`，`task_type: image`，`custom_model` 填模型名 |
| 豆包 Seedream | APIimage「ModelArk Image Generate」 | 只有它要带 `/v1`：`http://<宿主机IP>:13000/v1` |

- 改图：Load Image 接 OpenAI 节点的 `image1`（多图再接 `image2`/`image3`），局部重绘把 MASK 接 `mask`，走 `/v1/images/edits`。
- 容器里的 `127.0.0.1` 是容器自己，填宿主机内网 IP 或 `172.17.0.1`（New API 要监听 0.0.0.0）。填之前先用菜单 11 从容器里测一遍地址和令牌。
- grok-imagine-image 走 New API 里指向 cliproxy 的 OpenAI 类型渠道，cliproxy 版本太旧可能没有出图接口，Grok 账号需有额度。
- **APIimage 的 Grok / Gemini / Qwen / GLM 节点别用**：Grok 走 gRPC 直连 api.x.ai，base_url 不生效；Gemini 每次先拿令牌请求 Google 官方、失败才走 base_url；Qwen / GLM 是各家原生协议，New API 转发不了。
- 给 ComfyUI 单独建一个限额 New API 令牌：节点里的 key 会写进工作流 JSON 和图片元数据；APIimage 的 Config Saver、relayapi 的 `relay_config.json` 会把 key 明文存在插件目录里。
- **8188 没有鉴权**：能访问的人都能从 `/api/history`、`/api/object_info` 读到 key，或直接用 relayapi 存的 key 出图。别直接暴露公网——Docker `-p` 会绕过 ufw，用菜单 5 改成只走 Caddy 域名 + 密码，或在云安全组限 IP。

## 让网页（如画布）跨域直连

浏览器带 `Authorization` 头访问会先发预检 `OPTIONS`，而 `basic_auth` 会把它回 401；`basic_auth` 在 Caddy 里排在 `handle` 之前，所以要把认证和反代包进 `handle`，预检单独放行。站点片段（`<画布域名>` 换成实际来源，`/ws` 不要求认证）：

```
<ComfyUI 域名> {
	@preflight method OPTIONS
	handle @preflight {
		header Access-Control-Allow-Origin "https://<画布域名>"
		header Access-Control-Allow-Headers "Authorization, Content-Type"
		header Access-Control-Allow-Methods "GET, POST, OPTIONS"
		header Access-Control-Max-Age 86400
		respond 204
	}
	header Access-Control-Allow-Origin "https://<画布域名>"
	handle {
		@needauth not path /ws /ws/*
		basic_auth @needauth {
			<账号> <密码哈希>
		}
		reverse_proxy 127.0.0.1:8188
	}
}
```

改前先备份，`caddy validate` 通过再 `systemctl reload caddy`。自测：`curl -si -X OPTIONS https://<ComfyUI 域名>/prompt -H "Origin: https://<画布域名>" -H "Access-Control-Request-Method: POST"` 应返回 204 且带 `access-control-allow-origin`。

## HTTPS + 访问密码（共享机组特殊处理）

这台服务器 80/443 被香水商城 Caddy 容器占用，不要另装 Caddy。菜单 5 会：

1. 问域名、用户名和密码（不回显，输两遍），用 `perfume-shop-caddy-1` 生成 bcrypt 哈希，打印要追加到香水商城 Caddyfile 的站点：
   ```
   comfy.你的域名 {
       basic_auth {
           admin <哈希>
       }
       reverse_proxy 172.18.0.1:8188
   }
   ```
   然后 `docker exec perfume-shop-caddy-1 caddy reload --config /etc/caddy/Caddyfile`（Caddy 2.8 以前写 `basicauth`）。
   脚本不会改香水商城的 Caddyfile，要你自己贴进去。
2. 自检（只读）：检查 Caddy 容器的网关是不是 `172.18.0.1`，并在 Caddy 容器里访问 `172.18.0.1:端口`；填了域名、贴好站点后，
   再从本机测一次域名：401 = 密码生效，200 = 免密能访问（设了密码却 200 就是没生效）。Caddy 访问不到时，改绑默认选「否」。
3. 问是否把端口改绑 `172.18.0.1`（`-p 172.18.0.1:8188:8188`）。改了之后公网 `http://服务器IP:8188` 直连不通，只能走域名 + 密码；
   再进菜单 5 可以改回所有网卡。注意这只挡公网：本机进程和本机其他容器（包括香水商城的）仍能免密访问，所以 ComfyUI 仍要用单独的限额令牌。
   香水商城网络没起来（172.18.0.1 不在本机）时，更新 / 重建 / 改端口 / 恢复都会先停下，不拉代码、不删旧容器；
   已改绑时进菜单 5 第一步就能改回所有网卡。容器已经没了、菜单进不去时，把 `.install.conf` 里的 bind 改成 `bind=""` 再跑。
   菜单 4 换端口后要同步改 Caddyfile 里的 `reverse_proxy`。

Safari / iPhone 上如果看不到出图进度和结果（WebKit 的 WebSocket 不带 basic auth 凭据），把 `/ws` 排除在鉴权外：
```
comfy.你的域名 {
    @auth not path /ws
    basic_auth @auth {
        admin <哈希>
    }
    reverse_proxy 172.18.0.1:8188
}
```
`/ws` 只推送队列状态和进度，不能提交任务，也读不到 key。Chrome / Firefox 不需要这样改。