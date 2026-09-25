# ComfyUI 无 GPU 部署镜像（API 客户端模式）
#
# 设计目标：服务器没有 GPU，ComfyUI 只当「节点画布 + API 客户端」用
# （生图请求走 AI 反代，如 New API / Gemini / Grok）。
# 所以：
#   - 用 CPU 版 PyTorch（体积小、无 CUDA 依赖、不吃显存）
#   - 不内置任何本地模型（models/ 目录留空，走 API 插件）
#   - 源码挂载自宿主机（改代码重启即生效；更新走 deploy.sh --update，会重建镜像装新依赖）
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
    && pip install numpy \
    && pip freeze | grep -E '^(torch|torchvision|torchaudio)==' > /etc/pip-constraints.txt

# ★ 锁死 CPU 版 torch：之后所有 pip install 都不许替换它 —— 否则插件一要求新版 torch，
#   PyPI 就换成带 CUDA 的版本（多几 GB）。真有冲突会直接报错，而不是悄悄换掉。
#   Manager 默认用 uv 给插件装依赖，uv 不读 PIP_CONSTRAINT，所以 UV_CONSTRAINT 也要设
ENV PIP_CONSTRAINT=/etc/pip-constraints.txt \
    UV_CONSTRAINT=/etc/pip-constraints.txt

# 官方 requirements.txt（构建时用宿主机源码里的，部署脚本负责先 clone 源码）
# ★ 新版 ComfyUI 依赖 sqlalchemy + alembic（本地 SQLite 库），
#   必须在构建时装好，否则容器启动报 ModuleNotFoundError
# ★ deploy.sh 的 run_container 保证「先克隆源码到 app/ 再构建镜像」，
#   所以这里的 COPY 一定能找到 requirements.txt
COPY app/requirements.txt /tmp/comfyui-requirements.txt
RUN pip install -r /tmp/comfyui-requirements.txt

# ★ ComfyUI-Manager 新版是 pip 包（版本由官方 manager_requirements.txt 固定），
#   启动加 --enable-manager 启用，不再 git clone 到 custom_nodes
COPY app/manager_requirements.txt /tmp/comfyui-manager-requirements.txt
RUN pip install -r /tmp/comfyui-manager-requirements.txt

# ★ 预装 deploy.sh 插件组的依赖，避免容器启动时现场 pip install（容器重建就丢）：
#   APIimage：google-genai / xai_sdk / dashscope
#   VideoHelperSuite：opencv-python-headless / imageio-ffmpeg（顶层 import cv2，缺了导入失败）
RUN pip install google-genai xai_sdk dashscope opencv-python-headless imageio-ffmpeg

# 工作目录挂载点：宿主机源码（数据目录由 deploy.sh 挂到 app/ 下）
WORKDIR /opt/comfyui

EXPOSE 8188

# 启动参数：监听所有网卡，API 模式（无本地模型）
# --cpu：无 GPU 环境强制 CPU 模式（否则 model_management 默认找 CUDA 崩溃）
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", "--cpu", "--enable-manager"]