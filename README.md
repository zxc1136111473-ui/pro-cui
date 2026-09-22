# ComfyUI 服务器部署套件（无 GPU / API 客户端模式）

你的服务器没有 GPU，ComfyUI 只当「节点画布 + AI 反代客户端」用：
生图请求走你配置的 API（New API / Gemini / Grok 等），本机只跑界面和节点逻辑。

## 文件

```
comfyui-deploy/
  deploy.sh    一键部署脚本（交互菜单 + 非交互参数）
  Dockerfile   CPU 版 PyTorch 镜像（无 CUDA，省 ~4GB）
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
1) 全新安装 / 再装一套 / 重新部署
2) 更新到最新版（源码 git pull + 插件更新 + 重启容器）
3) 体检 + 看访问地址
4) 改监听端口
5) 配 HTTPS 提示（共享机复用香水商城 Caddy）
6) 插件管理（Manager / 汉化 / APIimage / 自定义）
7) 数据说明（源码在哪、怎么改代码、怎么接反代）
8) 卸载（a 保留目录 / b 连目录删）
l) 看日志   r) 重启容器   0) 退出
```

## 核心设计（为什么这么做）

1. **官方主仓库，不拉分支。** 源码 git clone 到 `app/` 并挂载进容器——
   - 改代码：直接编辑 `app/` 下文件 → `docker restart comfyui` 生效，不用重建镜像
   - 更新：`cd app && git pull && docker restart comfyui`
   - 加功能优先用**插件**（custom node），不要改源码，更新不冲突

2. **数据目录全部挂载**（`data/`），容器重建不丢：
   - `data/models/` 模型文件（走 API 不需要；本地推理就放这）
   - `data/custom_nodes/` 插件（git clone 进来 = 安装）
   - `data/output/` 出图、`data/input/` 上传参考图

3. **CPU 版 PyTorch 镜像**：无 CUDA，省 ~4GB 体积和显存需求。

4. **插件三件套**（菜单 6 一键装）：
   - `ComfyUI-Manager` — 插件管理器
   - `ComfyUI-Global-Translation` — 中文汉化
   - `ComfyUI-APIimage` — 接反代 API（Gemini/Grok/OpenAI 兼容，文生图/编辑/局部重绘）

## 接反代 API（APIimage 插件）

装好插件后在节点里填：
```
base_url: http://<你的New API>:13000/v1   或  你的反代域名
api_key:  你的令牌
```
`cliproxy` 渠道（grok-imagine-image）接口兼容，但 Grok 账号需有额度；
`gemini-image` 走 chat 接口（APIimage 已适配各厂商差异）。

## HTTPS（共享机组特殊处理）

这台服务器 80/443 被香水商城 Caddy 容器占用，不要另装 Caddy。
在香水商城的 Caddyfile 追加：
```
comfy.你的域名 {
    reverse_proxy 172.18.0.1:8188
}
```
然后 `docker exec perfume-shop-caddy-1 caddy reload --config /etc/caddy/Caddyfile`。