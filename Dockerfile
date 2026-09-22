# ComfyUI 无 GPU 部署镜像（API 客户端模式）
#
# 设计目标：服务器没有 GPU，ComfyUI 只当「节点画布 + API 客户端」用
# （生图请求走 AI 反代，如 New API / Gemini / Grok）。
# 所以：
#   - 用 CPU 版 PyTorch（体积小、无 CUDA 依赖、不吃显存）
#   - 不内置任何本地模型（models/ 目录留空，走 API 插件）
#   - 源码挂载自宿主机（改代码方便，git pull 即可更新）
#   - 官方 requirements.txt 在构建时安装（含新版需要的 sqlalchemy/alembic），
#     运行容器即开即用，不在启动时临时装依赖
#
# 构建：docker build -t comfyui:local .
# 运行：见 deploy.sh（自动生成 docker run 命令）

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COMFYUI_DIR=/opt/comfyui

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# CPU 版 PyTorch（官方 CPU index，不带 CUDA 运行库，省 ~4GB）
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        torch torchvision torchaudio \
    && pip install numpy

# 官方 requirements.txt（构建时用宿主机源码里的，部署脚本负责先 clone 源码）
# ★ 新版 ComfyUI 依赖 sqlalchemy + alembic（本地 SQLite 库），
#   必须在构建时装好，否则容器启动报 ModuleNotFoundError
# ★ deploy.sh 的 run_container 保证「先克隆源码到 app/ 再构建镜像」，
#   所以这里的 COPY 一定能找到 requirements.txt
COPY app/requirements.txt /tmp/comfyui-requirements.txt
RUN pip install -r /tmp/comfyui-requirements.txt

# ★ 预装 APIimage 插件依赖（google-genai / xai_sdk / dashscope），
#   避免容器每次启动时现场 pip install（省 10-15 秒启动时间）
RUN pip install google-genai xai_sdk dashscope

# 工作目录挂载点：宿主机源码 + 数据
WORKDIR /opt/comfyui
# 数据目录（由 deploy.sh 建好并挂载，镜像里先建好避免权限问题）
RUN mkdir -p /opt/comfyui/models /opt/comfyui/custom_nodes /opt/comfyui/output /opt/comfyui/input

EXPOSE 8188

# 启动参数：监听所有网卡，API 模式（无本地模型）
# --cpu：无 GPU 环境强制 CPU 模式（否则 model_management 默认找 CUDA 崩溃）
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu"]