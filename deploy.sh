#!/usr/bin/env bash
# ============================================================================
# ComfyUI 一键部署脚本（无 GPU / API 客户端模式）
#
# 在你没有 GPU 的 VPS 上跑这一个脚本，把 ComfyUI 装成「节点画布 + AI 反代
# 客户端」：生图请求走你配置的 API（New API / Gemini / Grok 等），本机只跑
# 界面和节点逻辑，不需要显卡。
#
# 用法（在 VPS 上）：
#   git clone <你的仓库> && cd 目录 && bash deploy.sh
#   或者：bash deploy.sh --install -y --port 8188
#
# ★ 设计要点：
#   · 官方主仓库源码直接挂载进容器（/root/comfyui/app）—— 改代码直接改
#     宿主机文件，重启容器生效，git pull 随时更新，不重新构建镜像
#   · 数据目录（models / custom_nodes / output / input）挂载到宿主机，
#     装插件、放模型、导出图片都直接落盘，容器重建不丢
#   · 不内置本地模型 —— 需要本地推理的模型文件自己放进 models/ 即可，
#     走 API 插件的话完全不需要 GPU
# ============================================================================
set -euo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  {
  if [ "${ZFC_JSON:-0}" = "1" ]; then
    printf '{"ok":false,"error":"%s"}\n' "$(printf '%s' "$*" | tr -d '"' | tr '\n' ' ')"
  fi
  printf '%s✗ %s%s\n' "$RED" "$*" "$RST" >&2; exit 1; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }

# ── 非交互 / 机器可读 ──────────────────────────────────────────────────────
ZFC_YES=0
ZFC_JSON=0
ZFC_ACTION=""        # install|update|uninstall|check|port|plugins|https|data|menu
ZFC_DIR=""           # --dir：安装目录（默认脚本所在目录，或 /opt/comfyui）
ZFC_PORT=""          # --port：监听端口（默认 8188）
ZFC_GIT=""           # --git：源码仓库地址（默认官方主仓库）
ZFC_PLUGINS=""       # --plugins-set：逗号分隔要预装的插件组号（1,2 / all）
ZFC_BACKUP_FILE=""   # --restore 后面的备份文件路径

json_out() {
  [ "$ZFC_JSON" = "1" ] || return 0
  printf '{"ok":true,"action":"%s","detail":"%s"}\n' "${1:-}" "${2:-}"
}
if [ ! -r /dev/tty ]; then ZFC_YES=1; fi

SELF_PATH=""
self_cmd() {
  local p="${SELF_PATH:-}"
  if [ -z "$p" ]; then
    case "${BASH_SOURCE[0]:-}" in
      /dev/fd/*|/proc/*|"") : ;;
      *) [ -r "${BASH_SOURCE[0]}" ] && p="${BASH_SOURCE[0]}" ;;
    esac
  fi
  if [ -n "$p" ]; then printf 'bash %s' "$p"
  else printf 'bash %s/deploy.sh' "${APP_DIR:-/opt/comfyui}"; fi
}

ask() {
  local __var="$1" __prompt="$2" __def="${3:-}" __ans
  if [ "${ZFC_YES:-0}" = "1" ]; then
    [ -n "$__def" ] || die "非交互模式下缺少必填项：${__prompt}（用参数或环境变量给出来）"
    printf -v "$__var" '%s' "$__def"; return 0
  fi
  if [ -n "$__def" ]; then printf '%s [%s]: ' "$__prompt" "$__def"; else printf '%s: ' "$__prompt"; fi
  read -r __ans </dev/tty || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_opt() {
  local __var="$1" __prompt="$2" __def="${3:-}" __ans
  if [ "${ZFC_YES:-0}" = "1" ]; then
    printf -v "$__var" '%s' "$__def"; return 0
  fi
  if [ -n "$__def" ]; then printf '%s [%s]: ' "$__prompt" "$__def"; else printf '%s（留空 = 不开）: ' "$__prompt"; fi
  read -r __ans </dev/tty || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
askyn() {
  local __p="$1" __d="${2:-n}" __a
  if [ "${ZFC_YES:-0}" = "1" ]; then return 0; fi
  printf '%s (y/n) [%s]: ' "$__p" "$__d"
  read -r __a </dev/tty || true
  __a="${__a:-$__d}"
  [ "${__a,,}" = "y" ] || [ "${__a,,}" = "yes" ]
}

# ── Docker 相关 ────────────────────────────────────────────────────────────
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
DOCKER=""
docker_ok() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER="docker"
  elif $SUDO docker info >/dev/null 2>&1; then
    DOCKER="$SUDO docker"
  else
    return 1
  fi
  return 0
}

install_docker() {
  say "正在装 Docker（系统包管理器，不添第三方源）…"
  if [ -f /etc/debian_version ]; then
    $SUDO apt-get update -y >/dev/null 2>&1 || true
    $SUDO apt-get install -y docker.io docker-compose-v2 >/dev/null 2>&1 \
      || $SUDO apt-get install -y docker.io >/dev/null 2>&1 \
      || die "apt 装 Docker 失败。手动装：https://docs.docker.com/engine/install/"
  elif [ -f /etc/redhat-release ] || [ -f /etc/centos-release ]; then
    $SUDO yum install -y docker >/dev/null 2>&1 \
      || $SUDO dnf install -y docker >/dev/null 2>&1 \
      || die "yum/dnf 装 Docker 失败。手动装：https://docs.docker.com/engine/install/"
  else
    die "认不出这个发行版，请手动装 Docker：https://docs.docker.com/engine/install/"
  fi
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
  sleep 2
  docker_ok || die "Docker 装上了但起不来"
  ok "Docker 装好了：$($DOCKER --version)"
}

port_busy() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    grep -qE "[:.]${p}\$" <<<"$(ss -ltn 2>/dev/null | awk '{print $4}' || true)" && return 0
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -qE "[:.]${p}[[:space:]]" && return 0
  fi
  return 1
}
port_check() {
  local p="$1"
  [[ "$p" =~ ^[0-9]+$ ]] || die "端口要是数字（你填的是 $p）"
  [ "$p" -ge 1 ] && [ "$p" -le 65535 ] || die "端口要在 1~65535 之间（你填的是 $p）"
  if [ "$p" -lt 1024 ] && [ "$(id -u)" -ne 0 ]; then
    warn "$p 是特权端口（<1024），普通用户绑不上"
  fi
  return 0
}

# ── 状态文件 ───────────────────────────────────────────────────────────────
STATE_FILE=""
state_load() { STATE_FILE="$APP_DIR/.install.conf"; }
state_read() {
  local k="$1" d="${2:-}"
  [ -f "$STATE_FILE" ] || { printf '%s' "$d"; return; }
  sed -n "s/^$k=//p" "$STATE_FILE" | tail -1 | sed 's/^"//;s/"$//' || printf '%s' "$d"
}
state_write() {
  local k="$1" v="$2"
  [ -f "$STATE_FILE" ] || touch "$STATE_FILE"
  if grep -q "^$k=" "$STATE_FILE" 2>/dev/null; then
    sed -i.bak "s|^$k=.*|$k=\"$v\"|" "$STATE_FILE" && rm -f "$STATE_FILE.bak"
  else
    printf '%s="%s"\n' "$k" "$v" >> "$STATE_FILE"
  fi
}

CONTAINER="comfyui"
IMAGE="comfyui:local"

# ── 构建并启动容器 ─────────────────────────────────────────────────────────
run_container() {
  docker_ok || die "Docker 没就绪。先装：$(self_cmd) 里选 1（会自动装），或 apt install docker.io"
  local port src
  port="$(state_read port 8188)"
  src="$(state_read src '')"

  # 源码目录（官方仓库，挂载进容器 —— 改代码直接改这里）
  if [ -z "$src" ] || [ ! -d "$src" ] || [ ! -f "$src/main.py" ]; then
    local defsrc="$APP_DIR/app"
    if [ -n "$src" ] && [ ! -d "$src" ]; then warn "源码目录不存在：$src，改用 $defsrc"; fi
    src="$defsrc"
    if [ ! -f "$src/main.py" ]; then
      say "拉取 ComfyUI 官方源码到 $src …"
      local giturl="${ZFC_GIT:-https://github.com/comfyanonymous/ComfyUI.git}"
      git clone --depth 1 "$giturl" "$src" 2>/dev/null \
        || die "clone 官方仓库失败（网络？）。手动：git clone $giturl $src"
    fi
    state_write src "$src"
  fi

  # 数据目录
  for d in models custom_nodes output input; do
    mkdir -p "$APP_DIR/data/$d"
  done

  # 构建镜像（CPU 版 torch；源码不进镜像，运行时装挂载）
  if ! $DOCKER image inspect "$IMAGE" >/dev/null 2>&1; then
    say "构建镜像 $IMAGE（CPU 版 PyTorch，无 CUDA，约 2-3 分钟）…"
    [ -f "$APP_DIR/Dockerfile" ] || die "缺 Dockerfile：$APP_DIR/Dockerfile"
    $DOCKER build -t "$IMAGE" "$APP_DIR" >/dev/null \
      || die "构建失败。看上面报错"
  fi

  # 停旧容器
  $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true

  $DOCKER run -d --name "$CONTAINER" --restart unless-stopped \
    -p "$port:8188" \
    -v "$src:/opt/comfyui/app" \
    -v "$APP_DIR/data/models:/opt/comfyui/models" \
    -v "$APP_DIR/data/custom_nodes:/opt/comfyui/app/custom_nodes" \
    -v "$APP_DIR/data/output:/opt/comfyui/output" \
    -v "$APP_DIR/data/input:/opt/comfyui/input" \
    -w /opt/comfyui/app \
    --entrypoint python \
    "$IMAGE" main.py --listen 0.0.0.0 --port 8188 --cpu \
    >/dev/null || die "容器起不来。看上面报错（端口被占？用菜单 4 换端口）"

  # 等它就绪（首次启动装依赖可能要 1-3 分钟）
  local tries=0
  while [ $tries -lt 60 ]; do
    if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null; then
      ok "服务就绪：http://127.0.0.1:$port/（首次启动很慢属正常，依赖在安装）"
      json_out "install" "port=$port src=$src"
      return 0
    fi
    tries=$((tries + 5)); sleep 5
  done
  warn "60 秒内没等到 HTTP 200 —— 首次启动装依赖较慢，再等等；或看日志：$(self_cmd) 里选 l"
  json_out "install" "port=$port (slow-start)"
  return 0
}

# ── 插件管理 ───────────────────────────────────────────────────────────────
# 预装插件 = 把仓库 clone 进 data/custom_nodes/。卸载 = 删目录。
# 按「API 客户端模式（无 GPU）」精选：不做本地推理，所以不推荐 ControlNet/
# Impact-Pack 那类依赖本地模型的插件；这些是纯界面/编排/API 增强。
PLUGIN_CORE="
ComfyUI-Manager|https://github.com/ltdrdata/ComfyUI-Manager.git
ComfyUI-Global-Translation|https://github.com/a63976659/ComfyUI-Global-Translation.git
ComfyUI-APIimage|https://github.com/AyinMostima/ComfyUI-APIimage.git
"
PLUGIN_TOOLS="
ComfyUI-Custom-Scripts|https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git
ComfyUI_essentials|https://github.com/cubiq/ComfyUI_essentials.git
ComfyUI-Logic|https://github.com/theUpsider/ComfyUI-Logic.git
"
PLUGIN_EXTRA="
ComfyUI-Impact-Pack|https://github.com/ltdrdata/ComfyUI-Impact-Pack.git
ComfyUI-VideoHelperSuite|https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
"
plugin_install() {
  local name="$1" url="$2"
  if [ -d "$APP_DIR/data/custom_nodes/$name" ]; then
    say "  插件 $name 已装，跳过"
    return 0
  fi
  say "  安装插件 $name …"
  git clone --depth 1 "$url" "$APP_DIR/data/custom_nodes/$name" 2>/dev/null \
    || warn "  插件 $name 安装失败（网络？）"
}

plugin_install_set() {
  # 参数：多行 "name|url" 字符串（PLUGIN_CORE / PLUGIN_TOOLS / PLUGIN_EXTRA）
  # ★ read 读完 EOF 返回非零会让函数返回 1，被 set -e 杀脚本 —— 必须显式 return 0
  local name url
  while IFS='|' read -r name url; do
    [ -n "$name" ] && plugin_install "$name" "$url"
  done <<< "$1"
  return 0
}

do_plugins() {
  docker_ok || die "Docker 没就绪"
  # 非交互：--plugins-set 1,2 或 all
  if [ -n "${ZFC_PLUGINS:-}" ]; then
    for want in ${ZFC_PLUGINS//,/ }; do
      case "$want" in
        1) plugin_install_set "$PLUGIN_CORE" ;;
        2) plugin_install_set "$PLUGIN_TOOLS" ;;
        3) plugin_install_set "$PLUGIN_EXTRA" ;;
        all) plugin_install_set "$PLUGIN_CORE"; plugin_install_set "$PLUGIN_TOOLS"; plugin_install_set "$PLUGIN_EXTRA" ;;
        *) warn "未知组号 $want（用 1/2/3 或 all）" ;;
      esac
    done
    askyn "重启容器让插件生效？" "y" && run_container
    json_out "plugins" "$ZFC_PLUGINS"
    return 0
  fi
  say ""
  say "  ${BLD}插件管理${RST}（装到 $APP_DIR/data/custom_nodes/，重启容器生效）"
  say ""
  say "  1) 必备三件套：Manager + 汉化 + APIimage（接反代生图）"
  say "  2) 进阶工具：Custom-Scripts + essentials + Logic 节点"
  say "  3) 本地模型增强：Impact-Pack + VideoHelperSuite（要 GPU）"
  say "  4) 全部装（1+2+3）"
  say "  5) 自定义插件（填 git 地址）"
  say "  6) 列出已装插件"
  say "  7) 卸载插件（填名字）"
  say "  0) 返回"
  ask WHAT "选一个" "1"
  case "$WHAT" in
    1) plugin_install_set "$PLUGIN_CORE" ;;
    2) plugin_install_set "$PLUGIN_TOOLS" ;;
    3) plugin_install_set "$PLUGIN_EXTRA" ;;
    4) plugin_install_set "$PLUGIN_CORE"
       plugin_install_set "$PLUGIN_TOOLS"
       plugin_install_set "$PLUGIN_EXTRA" ;;
    5) ask PNAME "插件目录名（如 MyNode）" ""
       ask PURL "Git 仓库地址" ""
       [ -n "$PNAME" ] && [ -n "$PURL" ] || die "名字和地址都要给"
       plugin_install "$PNAME" "$PURL" ;;
    6) say ""; ls -1 "$APP_DIR/data/custom_nodes/" 2>/dev/null | grep -v '^$' || say "（空）" ;;
    7) ask PNAME2 "插件目录名" ""
       [ -n "$PNAME2" ] || die "给个名字"
       rm -rf "$APP_DIR/data/custom_nodes/$PNAME2"
       ok "已删除插件 $PNAME2" ;;
    *) return 0 ;;
  esac
  askyn "重启容器让插件生效？" "y" && run_container
  json_out "plugins" "done"
}

# ── 备份 / 恢复 ─────────────────────────────────────────────────────────────
# 备份什么：data/（插件+模型+输出+输入）+ .install.conf（端口/源码路径）。
# 源码 app/ 是 git 仓库，不打包（重新 git clone 即可，体积大）。
do_backup() {
  docker_ok || true
  local dest="${1:-}"
  if [ -z "$dest" ]; then
    dest="$APP_DIR/backups/comfyui-$(date +%Y%m%d-%H%M%S).tar.gz"
  fi
  mkdir -p "$(dirname "$dest")"
  say "备份数据目录（data/ + 配置）到 $dest …"
  tar czf "$dest" -C "$APP_DIR" \
    --exclude='backups' \
    data .install.conf 2>/dev/null \
    || die "备份失败"
  local size
  size="$(/bin/ls -lh "$dest" | awk '{print $5}')"
  ok "备份完成（$size）：$dest"
  json_out "backup" "$dest"
}

do_restore() {
  docker_ok || true
  local srcf="${1:-}"
  if [ -z "$srcf" ]; then
    say ""
    ls -1t "$APP_DIR/backups/"*.tar.gz 2>/dev/null | head -10 || true
    ask srcf "把备份文件完整路径贴过来（上面列表里挑一个）" ""
  fi
  [ -n "$srcf" ] && [ -f "$srcf" ] || die "备份文件不存在：$srcf"
  # 先停容器再恢复，避免写一半
  docker_ok && $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true
  say "恢复 $srcf …"
  tar xzf "$srcf" -C "$APP_DIR" 2>/dev/null || die "恢复失败（备份文件坏了？）"
  ok "数据已恢复"
  # 恢复后按备份里的端口/源码路径重新拉起
  run_container
  json_out "restore" "$srcf"
}

# ── 数据说明 ───────────────────────────────────────────────────────────────
do_data_info() {
  say ""
  say "  ${BLD}数据都在哪里？${RST}"
  say "  安装目录：$APP_DIR"
  say ""
  say "    app/                  ComfyUI 官方源码（git 仓库，改代码直接改这里）"
  say "    Dockerfile            镜像构建文件（CPU 版 torch）"
  say "    data/models/          模型文件（走 API 插件不需要；本地推理就放这）"
  say "    data/custom_nodes/    插件目录（git clone 进来 = 安装）"
  say "    data/output/          出图/导出文件"
  say "    data/input/           上传的参考图"
  say ""
  say "  ${BLD}改代码怎么生效：${RST}"
  say "  直接编辑 app/ 下的文件（nano/vim），然后："
  say "    docker restart comfyui        # 重启容器即生效"
  say "  或者菜单选 r"
  say ""
  say "  ${BLD}更新到官方最新：${RST}"
  say "    cd $APP_DIR/app && git pull && docker restart comfyui"
  say "  （插件同理：进各自目录 git pull）"
  say ""
  say "  ${BLD}接反代 API（APIimage 插件）：${RST}"
  say "  装好插件后在节点里填 base_url + key，例："
  say "    base_url: http://<你的New API>:13000/v1   或 你的反代域名"
  say "    api_key:  你的令牌"
}

# ── HTTPS 提示（共享机组：80/443 被香水商城 Caddy 占，复用其 Caddyfile）─────
do_https_hint() {
  say ""
  say "  ${BLD}配 HTTPS${RST} —— 这台共享机上 80/443 已被香水商城 Caddy 容器占用，"
  say "  不要在这里装新 Caddy。正确做法：在香水商城的 Caddyfile 里追加一段："
  say ""
  say "    comfy.你的域名 {"
  say "        reverse_proxy 172.18.0.1:$(state_read port 8188)"
  say "    }"
  say ""
  say "  然后热加载（注意是容器里的 Caddy，不是系统 Caddy）："
  say "    docker exec perfume-shop-caddy-1 caddy reload --config /etc/caddy/Caddyfile"
  say ""
  say "  证书由 Caddy 自动申请续期，和无限画布 wxhb.wqyhr.com 同一套逻辑。"
}

# ── 体检 ───────────────────────────────────────────────────────────────────
do_check() {
  docker_ok || { warn "Docker 没就绪"; return 1; }
  local port src
  port="$(state_read port 8188)"
  src="$(state_read src '')"
  say ""
  say "  容器："
  $DOCKER ps --filter "name=$CONTAINER" --format '    {{.Names}}  {{.Status}}  {{.Ports}}' || true
  local running=""
  running="$($DOCKER ps -q --filter "name=$CONTAINER" 2>/dev/null || true)"
  if [ -n "$running" ]; then
    say "  版本：$($DOCKER exec "$CONTAINER" sh -c 'cat /opt/comfyui/app/comfyui_version.py 2>/dev/null | grep __version__' 2>/dev/null | grep -oE '[0-9.]+' || echo '?')"
    say "  源码：$src"
    say "  插件：$(ls -1 "$APP_DIR/data/custom_nodes/" 2>/dev/null | grep -v '^$' | tr '\n' ' ' || echo 无)"
  fi
  say ""
  say "  访问地址："
  local ips=""
  ips="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$ips" ] && ips="$(ip -4 addr show 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | grep -v '^127\.' | head -1)"
  say "    http://${ips:-<本机IP>}:$port/"
  say ""
  if [ -n "$running" ]; then
    if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$port/" 2>/dev/null; then
      ok "HTTP 200 —— 服务正常"
    else
      warn "容器在跑但页面没响应（首次启动装依赖较慢）—— 菜单选 l 看日志"
    fi
  else
    warn "容器没在跑 —— 菜单选 1 部署/更新"
  fi
  json_out "check" "port=$port running=$([ -n "$running" ] && echo yes || echo no)"
}

# ── 卸载 ───────────────────────────────────────────────────────────────────
do_uninstall() {
  docker_ok || { warn "Docker 都没就绪，没什么可卸的"; return 0; }
  local wipe="${1:-0}"
  say ""
  if [ "$wipe" = "1" ]; then
    say "  ${RED}卸载并删掉安装目录${RST}（$APP_DIR，含源码/插件/数据）。"
  else
    say "  卸载：停容器，保留安装目录（源码/插件/数据都留着）。"
  fi
  askyn "确认卸载？" "n" || { say "取消"; return 0; }
  $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true
  ok "容器已删除"
  if [ "$wipe" = "1" ]; then
    rm -rf "$APP_DIR"
    ok "安装目录已删除：$APP_DIR"
  else
    say "安装目录保留：$APP_DIR（以后想再起，回这里跑 deploy.sh 选 1）"
  fi
  json_out "uninstall" "wipe=$wipe"
}

# ── 主流程 ─────────────────────────────────────────────────────────────────
PURGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --install)   ZFC_ACTION="install" ;;
    --update)    ZFC_ACTION="update" ;;
    --uninstall) ZFC_ACTION="uninstall" ;;
    --purge)     PURGE=1 ;;
    --check)     ZFC_ACTION="check" ;;
    --port)      ZFC_PORT="${2:?--port 后面要给个端口}"; shift ;;
    --dir)       ZFC_DIR="${2:?--dir 后面要给个目录}"; shift ;;
    --git)       ZFC_GIT="${2:?--git 后面要给仓库地址}"; shift ;;
    --plugins)   ZFC_ACTION="plugins" ;;
    --plugins-set) ZFC_PLUGINS="${2:?--plugins-set 后面要给组号，如 1,2 或 all}"; ZFC_ACTION="plugins"; shift ;;
    --backup)    ZFC_ACTION="backup" ;;
    --restore)   ZFC_ACTION="restore"; ZFC_BACKUP_FILE="${2:-}"; [ $# -gt 1 ] && shift ;;
    -y)          ZFC_YES=1 ;;
    --json)      ZFC_JSON=1 ;;
    --menu)      ZFC_ACTION="menu" ;;
    -h|--help)
      say "用法: $(self_cmd) [动作] [选项]"
      say ""
      say "动作："
      say "  （不带参数）        交互菜单"
      say "  --install           全新安装 / 重新部署"
      say "  --update            更新（源码 git pull + 重启容器）"
      say "  --check             体检 + 看访问地址"
      say "  --uninstall         卸载（加 --purge 连目录一起删）"
      say "  --plugins           插件管理"
      say "  --plugins-set <n>   非交互装插件组（1,2 或 all）"
      say "  --backup            备份数据（data/ + 配置）"
      say "  --restore           恢复备份（加文件路径参数）"
      say ""
      say "安装选项："
      say "  --dir <目录>        安装目录（默认脚本所在目录）"
      say "  --port <n>          监听端口（默认 8188）"
      say "  --git <url>         源码仓库（默认官方 https://github.com/comfyanonymous/ComfyUI.git）"
      say "  -y                  非交互（所有确认自动是）"
      say "  --json              末尾输出一行 JSON"
      exit 0 ;;
    -*) die "不认识的参数：$1（--help 看用法）" ;;
    *)  die "多余的参数：$1（--help 看用法）" ;;
  esac
  shift
done

# 安装目录
if [ -n "$ZFC_DIR" ]; then
  APP_DIR="$ZFC_DIR"
else
  case "${BASH_SOURCE[0]:-}" in
    /dev/fd/*|/proc/*|"") APP_DIR="/opt/comfyui" ;;
    *) APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P || echo /opt/comfyui)" ;;
  esac
fi
[ -f "$APP_DIR/deploy.sh" ] && SELF_PATH="$APP_DIR/deploy.sh"
mkdir -p "$APP_DIR/data"
state_load

say "${BLD}ComfyUI 部署脚本${RST}"
hr

# 显式动作直通
if [ "$ZFC_ACTION" = "uninstall" ]; then do_uninstall "$PURGE"; exit 0; fi
if [ "$ZFC_ACTION" = "check" ]; then do_check && exit 0 || exit $?; fi
if [ "$ZFC_ACTION" = "plugins" ]; then do_plugins; exit 0; fi
if [ "$ZFC_ACTION" = "backup" ]; then do_backup "${ZFC_BACKUP_FILE:-}"; exit 0; fi
if [ "$ZFC_ACTION" = "restore" ]; then do_restore "${ZFC_BACKUP_FILE:-}"; exit 0; fi
if [ "$ZFC_ACTION" = "update" ]; then
  docker_ok || die "Docker 没就绪"
  local src2; src2="$(state_read src '')"
  [ -n "$src2" ] && [ -d "$src2" ] || die "没有源码目录，先全新安装"
  say "更新源码（git pull）…"
  (cd "$src2" && git pull --ff-only 2>&1 | tail -2 || true)
  # 插件的更新也带上
  for d in "$APP_DIR"/data/custom_nodes/*/; do
    [ -d "$d/.git" ] && { say "更新插件 $(basename "$d") …"; (cd "$d" && git pull --ff-only 2>&1 | tail -1 || true); }
  done
  run_container
  ok "更新完成"
  exit 0
fi

# 已装过 → 菜单
INSTALLED=0
docker_ok && [ "$($DOCKER ps -aq --filter "name=$CONTAINER" 2>/dev/null | wc -l | tr -d ' ')" -ge 1 ] && INSTALLED=1

if [ "$INSTALLED" = "1" ] && [ "$ZFC_ACTION" != "install" ]; then
  say "这台机器上${BLD}已经装过${RST}了（容器 $CONTAINER，端口 $(state_read port 8188)）"
  say ""
  say "  1) ${BLD}全新安装${RST} / 再装一套（换端口换目录）/ 重新部署这一套"
  say "  2) ${BLD}更新到最新版${RST}（源码 git pull + 插件更新 + 重启容器）"
  say "  3) 体检 + 看访问地址"
  say "  4) 改监听端口"
  say "  5) 配 HTTPS 提示（共享机复用香水商城 Caddy）"
  say "  6) 插件管理（Manager / 汉化 / APIimage / 自定义）"
  say "  7) 数据说明（源码在哪、怎么改代码、怎么接反代）"
  say "  8) ${BLD}一键备份${RST}（插件+模型+输出+配置 → backups/）"
  say "  9) 一键恢复${RST}（从备份文件还原数据并重启）"
  say "  10) ${RED}卸载${RST}"
  say "  l) 看容器日志"
  say "  r) 重启容器"
  say "  0) 退出"
  ask WHAT "选一个" "1"
  case "$WHAT" in
    1) : ;;
    2) ZFC_ACTION="update"
       local src3; src3="$(state_read src '')"
       [ -n "$src3" ] && [ -d "$src3" ] || die "没有源码目录，先全新安装"
       say "更新源码（git pull）…"
       (cd "$src3" && git pull --ff-only 2>&1 | tail -2 || true)
       for d in "$APP_DIR"/data/custom_nodes/*/; do
         [ -d "$d/.git" ] && { say "更新插件 $(basename "$d") …"; (cd "$d" && git pull --ff-only 2>&1 | tail -1 || true); }
       done
       run_container
       ok "更新完成"
       exit 0 ;;
    3) do_check; exit 0 ;;
    4) ask NP "改成哪个端口" "$(state_read port 8188)"
       port_check "$NP"
       port_busy "$NP" && die "端口 $NP 被别的进程占着"
       state_write port "$NP"
       run_container
       ok "端口已改为 $NP"
       exit 0 ;;
    5) do_https_hint; exit 0 ;;
    6) do_plugins; exit 0 ;;
    7) do_data_info; exit 0 ;;
    8) do_backup; exit 0 ;;
    9) do_restore; exit 0 ;;
    10) say ""
        say "  a) 卸载但${GRN}保留目录${RST}（容器停掉，目录留着）"
        say "  b) 卸载并${RED}删掉整个目录${RST}（源码/插件/数据全没了）"
        ask UW "选一个" "a"
        do_uninstall "$([ "$UW" = "b" ] && echo 1 || echo 0)"; exit 0 ;;
    l|L) $DOCKER logs --tail 50 "$CONTAINER" 2>&1 || true; exit 0 ;;
    r|R) run_container; exit 0 ;;
    0) exit 0 ;;
    *) hr ;;
  esac
fi

# 全新安装流程
if [ "$INSTALLED" = "0" ] && [ "$ZFC_ACTION" != "install" ]; then
  say "这台机器上${BLD}还没装过${RST} —— 下面开始${BLD}全新安装${RST}。"
  say "（一条命令装完：${BLD}$(self_cmd) --install -y --port 8188${RST}）"
  hr
fi

# 0. Docker
if ! docker_ok; then
  warn "这台机器上没有可用的 Docker"
  if askyn "现在自动装一个（系统包管理器，不添第三方源）" "y"; then
    install_docker
  else
    die "那就先自己装 Docker 再来：https://docs.docker.com/engine/install/"
  fi
fi
ok "Docker 就绪"

# 1. 问目录 / 端口 / 源码
if [ -n "$ZFC_DIR" ]; then
  INSTALL_DIR="$ZFC_DIR"
else
  ask INSTALL_DIR "装到哪个目录" "$APP_DIR"
fi
mkdir -p "$INSTALL_DIR/data"
APP_DIR="$INSTALL_DIR"
state_load

if [ -n "$ZFC_PORT" ]; then PORT="$ZFC_PORT"; else PORT="$(state_read port 8188)"; fi
ask PORT "监听端口（默认 8188）" "$PORT"
port_check "$PORT"
port_busy "$PORT" && die "端口 $PORT 被别的进程占着（换一个）"

ask_opt GAURL "源码仓库（留空 = 官方 comfyanonymous/ComfyUI）" ""
[ -n "$ZFC_GIT" ] && GAURL="$ZFC_GIT"

say ""
say "  确认一下："
say "    目录：$APP_DIR"
say "    端口：$PORT"
say "    源码：${GAURL:-官方 https://github.com/comfyanonymous/ComfyUI.git}"
askyn "开始部署？" "y" || { say "取消"; exit 1; }

state_write port "$PORT"

run_container

say ""
ok "完事。访问地址：http://<这台机器的IP>:$PORT/"
say ""
say "${BLD}下一步建议${RST}：菜单选 6 装【Manager + 汉化 + APIimage】三件套，"
say "然后在节点里填你的反代 base_url + key 就能出图。"
say ""
say "以后要更新：菜单选 2（一条命令：$(self_cmd) --update）"
json_out "install" "port=$PORT"