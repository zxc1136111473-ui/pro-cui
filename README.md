# ComfyUI 服务器部署套件（无 GPU / API 客户端模式）

你的服务器没有 GPU，ComfyUI 只当「节点画布 + AI 反代客户端」用：
生图请求走你配置的 API（New API / Gemini / Grok 等），本机只跑界面和节点逻辑。

## 文件

```
comfyui-deploy/
  deploy.sh    一键部署脚本（交互菜单 + 非交互参数）
  Dockerfile   CPU 版 PyTorch 镜像（无 CUDA，省 ~4GB）
  .dockerignore  构建只带 requirements，不把 data/ backups/ 打进构建上下文（传服务器别漏了这个点文件）
```

## 怎么用（在服务器上）

```bash
# 把 comfyui-deploy/ 传到服务器（或 git clone 仓库后取这份）
cd comfyui-deploy
bash deploy.sh                    # 交互菜单
bash deploy.sh --install -y --port 8188   # 一条命令装完
```

装完访问 `http://服务器IP:8188/`。

## 菜单功能

```
1) 全新安装 / 重新部署（容器只有一个，换目录会替换现有这套）
2) 更新到最新版（源码 git pull + 插件更新 + 重建镜像 + 重建容器）
3) 体检 + 看访问地址
4) 改监听端口
5) 配 HTTPS + 访问密码（复用香水商城 Caddy，公网只走域名）
6) 插件管理（汉化 / APIimage / relayapi / 自定义）
7) 数据说明（源码在哪、怎么改代码、怎么接反代）
8) 一键备份（data/ 含插件/工作流/出图 + 配置 → backups/，只留最近 5 份）
9) 一键恢复（先校验备份包；端口和绑定方式保留本机当前的，本目录原来没配置时沿用备份里的）
10) 卸载（a 保留目录 / b 连目录删，backups/ 也会删）
11) 测试反代（容器 → New API 连通 + 令牌能用哪些出图模型）
l) 看日志（最近 200 行 / 实时跟随 / 只看本次启动的问题）
r) 重启容器（docker restart，不删容器，约 1 秒）   R) 重建容器（删了重新 run）   0) 退出
```

几个会改变实际行为的数值写在 `deploy.sh` 开头：探测超时 5 秒、日志轮转 10MB×3（只作用于这个容器）、
备份保留 5 份。要改先想清楚影响。

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

6. **CPU 版 torch 锁死**：镜像用 `PIP_CONSTRAINT` + `UV_CONSTRAINT`（Manager 默认用 uv）锁住 torch / torchvision / torchaudio，
   插件依赖要求别的 torch 版本时会直接报错，不会悄悄换成几 GB 的 CUDA 版。
   新插件有 pip 依赖时，写进 Dockerfile 后菜单选 2（会重建镜像）。
   菜单 1 / R / 2 / 4 / 5（改绑时）/ 9 都是删掉容器再新建，现场（含 Manager 里）装的包会丢；
   r 和装完插件后的重启只是 `docker restart`，包还在。容器用 `--stop-signal SIGINT`，ComfyUI 收到后正常退出，
   不用等 Docker 默认的 10 秒超时（这个设置在建容器时生效，老容器要菜单 R 或 2 重建一次）。

7. **升级部署套件**（换了新的 deploy.sh / Dockerfile）后先跑菜单 2：安装和更新会重建镜像，
   r / R / 4 / 5 / 6 / 9 不会，会继续用旧镜像。旧镜像重建后自动删除。

8. **更新失败自动退回**：菜单 2 构建镜像失败时，旧容器还在跑，脚本把源码和插件退回更新前的 commit，
   服务仍是旧版，不会等到下次重启才变成「新源码 + 旧依赖」起不来。

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