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
#   · 官方主仓库源码直接挂载进容器（/opt/comfyui/app）—— 改代码直接改
#     宿主机文件，重启容器生效；更新走 --update（git pull + 重建镜像装新依赖）
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
  [[ "$p" =~ ^[0-9]+$ ]] || die "端口要是数字（你填的是 ${p}）"
  [ "$p" -ge 1 ] && [ "$p" -le 65535 ] || die "端口要在 1~65535 之间（你填的是 ${p}）"
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
  local v
  v="$(sed -n "s/^$k=//p" "$STATE_FILE" | tail -1 | sed 's/^"//;s/"$//')"
  printf '%s' "${v:-$d}"
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
# 共享机上占着 80/443 的香水商城 Caddy 容器，及它访问宿主机用的网关地址
CADDY_CONTAINER="perfume-shop-caddy-1"
CADDY_GW="172.18.0.1"

# 会改变实际行为的边界值（改之前先跟用户确认，见仓库 AGENTS.md）
PROBE_TIMEOUT=5      # 网络探测超时（秒）：反代自测、Caddy 自检；失败只提示，不改任何东西
LOG_TAIL=200         # 菜单 l 默认看最近多少行
LOG_MAX_SIZE="10m"   # 容器日志轮转：单个文件上限 ×
LOG_MAX_FILE=3       #               文件数（最多 30MB，只作用于本容器）
BACKUP_KEEP=5        # 一键备份只保留最近几份（只清本目录 backups/comfyui-*.tar.gz）

# 绑定地址在不在本机（香水商城网络没起 / 换了机器时不在）；空 = 所有网卡，总是可以
bind_ok() {
  [ -n "${1:-}" ] || return 0
  command -v ip >/dev/null 2>&1 || return 0
  ip -4 -o addr show | grep -F " inet $1/" >/dev/null
}
bind_die() {
  die "绑定地址 $1 不在本机（香水商城网络没起来？）—— 等它起来再重跑；或把 $STATE_FILE 里的 bind 改成 bind=\"\"（改回所有网卡，无鉴权）再重跑"
}

# 本机探活用的地址：端口绑在所有网卡时走 127.0.0.1，否则走绑定的地址
probe_host() {
  local b; b="$(state_read bind '')"
  case "$b" in ""|0.0.0.0) printf '127.0.0.1' ;; *) printf '%s' "$b" ;; esac
}

# ── 构建并启动容器 ─────────────────────────────────────────────────────────
run_container() {
  docker_ok || die "Docker 没就绪。先装：$(self_cmd) 里选 1（会自动装），或 apt install docker.io"
  local port src bind
  port="$(state_read port 8188)"
  src="$(state_read src '')"
  # 端口绑在哪：空 = 所有网卡（IP:端口 直连）；$CADDY_GW = 公网只走 Caddy 域名（菜单 5 切换）
  bind="$(state_read bind '')"
  # 绑定地址不在本机就先停下，别构建完、删了旧容器再起不来
  bind_ok "$bind" || bind_die "$bind"


  # 源码目录（官方仓库，挂载进容器 —— 改代码直接改这里）
  if [ -z "$src" ] || [ ! -d "$src" ] || [ ! -f "$src/main.py" ]; then
    local defsrc="$APP_DIR/app"
    if [ -n "$src" ] && [ ! -d "$src" ]; then warn "源码目录不存在：${src}，改用 $defsrc"; fi
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
  for d in models custom_nodes output input user; do
    mkdir -p "$APP_DIR/data/$d"
  done

  # 构建镜像（CPU 版 torch；源码不进镜像，运行时装挂载）
  # ZFC_REBUILD=1（安装/更新时）强制重建：拿到新 requirements / Dockerfile 改动，没变的层走缓存
  local old_img=""
  old_img="$($DOCKER image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
  if [ "${ZFC_REBUILD:-0}" = "1" ] || [ -z "$old_img" ]; then
    # 构建失败时旧容器还没删，服务照旧；ZFC_ON_BUILD_FAIL（更新时）负责退回源码和插件
    build_image || { [ -z "${ZFC_ON_BUILD_FAIL:-}" ] || "$ZFC_ON_BUILD_FAIL"; die "构建失败。看上面报错"; }
  fi

  # 停旧容器
  $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true

  # ★ 数据目录都挂到 app/ 下：ComfyUI 按源码目录找 models/custom_nodes/output/input/user
  #   （user/ 里是网页保存的工作流、界面设置、Manager 配置、comfyui.db）
  # --stop-signal SIGINT：ComfyUI 不处理默认的 SIGTERM，docker stop/restart 要等 10 秒超时才 SIGKILL；
  #   SIGINT 让它走 KeyboardInterrupt 正常退出（打印 Stopped server），重启秒级完成
  $DOCKER run -d --name "$CONTAINER" --restart unless-stopped --stop-signal SIGINT \
    --log-driver json-file --log-opt max-size="$LOG_MAX_SIZE" --log-opt max-file="$LOG_MAX_FILE" \
    -p "${bind:+$bind:}$port:8188" \
    -v "$src:/opt/comfyui/app" \
    -v "$APP_DIR/data/models:/opt/comfyui/app/models" \
    -v "$APP_DIR/data/custom_nodes:/opt/comfyui/app/custom_nodes" \
    -v "$APP_DIR/data/output:/opt/comfyui/app/output" \
    -v "$APP_DIR/data/input:/opt/comfyui/app/input" \
    -v "$APP_DIR/data/user:/opt/comfyui/app/user" \
    -w /opt/comfyui/app \
    --entrypoint python \
    "$IMAGE" main.py --listen 0.0.0.0 --port 8188 --cpu --enable-manager \
    >/dev/null || die "容器起不来。看上面报错（端口被占？用菜单 4 换端口）"

  # 重建过就删掉被替换的旧镜像（每份 1GB+；只删本项目的，共享机上不用全局 prune）
  local new_img=""
  new_img="$($DOCKER image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
  if [ -n "$old_img" ] && [ "$old_img" != "$new_img" ]; then
    $DOCKER rmi "$old_img" >/dev/null 2>&1 || true
  fi

  if wait_ready; then json_out "install" "port=$port src=$src"; else json_out "install" "port=$port (slow-start)"; fi
  return 0
}

# 构建镜像；失败返回非零（不 die），由调用方收场
build_image() {
  say "构建镜像 ${IMAGE}（CPU 版 PyTorch，无 CUDA，首次约 2-3 分钟）…"
  [ -f "$APP_DIR/Dockerfile" ] || { warn "缺 Dockerfile：$APP_DIR/Dockerfile"; return 1; }
  $DOCKER build -t "$IMAGE" "$APP_DIR"
}

# 等服务就绪（首次启动较慢）
wait_ready() {
  local port tries=0
  port="$(state_read port 8188)"
  while [ $tries -lt 60 ]; do
    if curl -fsS -o /dev/null --max-time 3 "http://$(probe_host):$port/" 2>/dev/null; then
      ok "服务就绪：http://$(probe_host):$port/"
      return 0
    fi
    tries=$((tries + 5)); sleep 5
  done
  warn "60 秒内没等到 HTTP 200 —— 首次启动较慢，再等等；或看日志：$(self_cmd) 里选 l"
  return 1
}

# 重启：docker restart，不删容器（现场装的包还在）；容器不在或起不来再重建
restart_container() {
  if $DOCKER restart "$CONTAINER" >/dev/null 2>&1; then
    wait_ready || true
  else
    warn "docker restart 失败，改为重建容器"
    run_container
  fi
}

# ── 插件管理 ───────────────────────────────────────────────────────────────
# 预装插件 = 把仓库 clone 进 data/custom_nodes/。卸载 = 删目录。
# 按「API 客户端模式（无 GPU）」精选：不做本地推理，所以不推荐 ControlNet/
# Impact-Pack 那类依赖本地模型的插件；这些是纯界面/编排/API 增强。
# ★ 这里列的插件，导入需要的 pip 依赖都已在 Dockerfile 预装（容器每次重建，现场装的包会丢）；
#   例外：essentials 的抠图 / LUT / 像素化 / Seam Carving 节点要 rembg 等本地模型依赖，没装
# ★ Manager 不在这里：已改为 pip 包 + --enable-manager（见 Dockerfile）
PLUGIN_CORE="
ComfyUI-Global-Translation|https://github.com/a63976659/ComfyUI-Global-Translation.git
ComfyUI-APIimage|https://github.com/AyinMostima/ComfyUI-APIimage.git
ComfyUI-relayapi|https://github.com/flywhale-666/ComfyUI-relayapi.git
"
PLUGIN_TOOLS="
rgthree-comfy|https://github.com/rgthree/rgthree-comfy.git
"
PLUGIN_EXTRA="
ComfyUI-Custom-Scripts|https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git
ComfyUI_essentials|https://github.com/cubiq/ComfyUI_essentials.git
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

# 已装插件目录名，一行一个
plugin_list() {
  local d n
  for d in "$APP_DIR"/data/custom_nodes/*/; do
    [ -d "$d" ] || continue
    n="$(basename "$d")"
    [ "$n" = "__pycache__" ] || printf '%s\n' "$n"
  done
}

# 带序号列出；* = 本地改过被跟踪的文件（更新时会先暂存、拉完放回）
plugin_show() {
  local i=0 n mark
  while IFS= read -r n; do
    i=$((i + 1)); mark=" "
    if [ -d "$APP_DIR/data/custom_nodes/$n/.git" ] \
      && [ -n "$(git -C "$APP_DIR/data/custom_nodes/$n" status --porcelain --untracked-files=no 2>/dev/null || true)" ]; then
      mark="*"
    fi
    say "   $i) $mark $n"
  done < <(plugin_list)
  if [ "$i" = "0" ]; then say "  （空）"; else say "  （* = 本地改过文件）"; fi
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
        *) warn "未知组号 ${want}（用 1/2/3 或 all）" ;;
      esac
    done
    askyn "重启容器让插件生效？" "y" && restart_container
    json_out "plugins" "$ZFC_PLUGINS"
    return 0
  fi
  say ""
  say "  ${BLD}插件管理${RST}（装到 $APP_DIR/data/custom_nodes/，重启容器生效）"
  say ""
  say "  1) 必备三件套：汉化 + APIimage + relayapi（接反代生图）"
  say "  2) 进阶工具：rgthree（组静音 / 只跑选中输出 / 图片对比）"
  say "  3) 可选：Custom-Scripts + essentials + VideoHelperSuite"
  say "  4) 全部装（1+2+3）"
  say "  5) 自定义插件（填 git 地址）"
  say "  6) 列出已装插件"
  say "  7) 卸载插件（按序号）"
  say "  0) 返回"
  ask WHAT "选一个" "1"
  case "$WHAT" in
    1) plugin_install_set "$PLUGIN_CORE" ;;
    2) plugin_install_set "$PLUGIN_TOOLS" ;;
    3) plugin_install_set "$PLUGIN_EXTRA" ;;
    4) plugin_install_set "$PLUGIN_CORE"
       plugin_install_set "$PLUGIN_TOOLS"
       plugin_install_set "$PLUGIN_EXTRA" ;;
    5) ask PURL "Git 仓库地址" ""
       [ -n "$PURL" ] || die "要给 git 地址"
       PNAME="$(basename "${PURL%/}")"
       ask PNAME "插件目录名" "${PNAME%.git}"
       case "$PNAME" in ""|*/*|.*) die "插件目录名不能为空、带 / 或以 . 开头：$PNAME" ;; esac
       plugin_install "$PNAME" "$PURL" ;;
    6) say ""; plugin_show; return 0 ;;
    7) say ""; plugin_show
       local names=() pick
       mapfile -t names < <(plugin_list)
       [ "${#names[@]}" -gt 0 ] || return 0
       ask pick "卸载第几个" ""
       [[ "$pick" =~ ^[1-9][0-9]*$ ]] && [ "$pick" -le "${#names[@]}" ] || die "序号不对：$pick"
       PNAME2="${names[$((pick - 1))]}"
       askyn "确认删除 $APP_DIR/data/custom_nodes/$PNAME2 ？" "n" || { say "取消"; return 0; }
       rm -rf "$APP_DIR/data/custom_nodes/$PNAME2"
       ok "已删除插件 $PNAME2" ;;
    *) return 0 ;;
  esac
  askyn "重启容器让插件生效？" "y" && restart_container
  json_out "plugins" "done"
}

# ── 备份 / 恢复 ─────────────────────────────────────────────────────────────
# 备份什么：data/（插件+模型+输出+输入+工作流/设置）+ .install.conf（端口/源码路径）。
# 源码 app/ 是 git 仓库，不打包（重新 git clone 即可，体积大）。
do_backup() {
  docker_ok || true
  local dest="${1:-}"
  if [ -z "$dest" ]; then
    dest="$APP_DIR/backups/comfyui-$(date +%Y%m%d-%H%M%S).tar.gz"
  fi
  # 备份里有插件配置和工作流里的明文 key：目录和文件都只给自己读
  (umask 077; mkdir -p "$(dirname "$dest")")
  say "备份数据目录（data/ + 配置）到 $dest …"
  # 不停容器：打包中途有文件在写（出图、日志）时 GNU tar 返回 1，归档仍完整，只算警告
  local rc=0
  (umask 077; tar czf "$dest" -C "$APP_DIR" \
    --exclude='backups' --exclude='data/user/*.log' \
    data .install.conf 2>/dev/null) || rc=$?
  [ "$rc" -le 1 ] || die "备份失败（tar 退出码 ${rc}）"
  [ "$rc" = "0" ] || warn "打包时有文件正在写入（出图/日志），归档已生成；要严格一致可先停容器再备份"
  local size old
  size="$(/bin/ls -lh "$dest" | awk '{print $5}')"
  ok "备份完成（${size}）：$dest"
  # 只保留最近 $BACKUP_KEEP 份（只清本目录 backups/ 下脚本生成的 comfyui-*.tar.gz）
  ls -1t "$APP_DIR/backups/"comfyui-*.tar.gz 2>/dev/null | tail -n +$((BACKUP_KEEP + 1)) \
    | while IFS= read -r old; do rm -f "$old"; say "  清理旧备份：$(basename "$old")"; done || true
  say "  定时备份（可选）：crontab -e 加一行  0 4 * * * $(self_cmd) --backup >/dev/null 2>&1"
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
  # 先确认包是好的，再动容器
  tar tzf "$srcf" >/dev/null 2>&1 || die "备份文件坏了或不是 tar.gz：${srcf}（现有容器没动）"
  # 端口 / 绑定方式按本机当前的来（备份可能来自别的机器或旧版本）
  local had_state=0 cur_port="" cur_bind=""
  if [ -f "$STATE_FILE" ]; then
    had_state=1; cur_port="$(state_read port 8188)"; cur_bind="$(state_read bind '')"
  fi
  # 本机配置的绑定地址不在：容器和数据都不动
  [ "$had_state" = "0" ] || bind_ok "$cur_bind" || bind_die "$cur_bind"
  # 先停容器再恢复，避免写一半
  docker_ok && $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true
  say "恢复 $srcf …"
  tar xzf "$srcf" -C "$APP_DIR" 2>/dev/null || die "恢复失败（备份文件坏了？）"
  ok "数据已恢复"
  # 备份里的源码路径是旧目录的绝对路径，不在本目录就清掉，让 run_container 在这里重新 clone
  case "$(state_read src '')" in "$APP_DIR"/*) : ;; *) state_write src "" ;; esac
  if [ "$had_state" = "1" ]; then
    [ "$(state_read port 8188)" = "$cur_port" ] || say "  备份里的端口是 $(state_read port 8188)，保留本机当前的 $cur_port"
    [ "$(state_read bind '')" = "$cur_bind" ] || say "  备份里的绑定是「$(state_read bind '')」，保留本机当前的「${cur_bind}」"
    state_write port "$cur_port"
    state_write bind "$cur_bind"
  fi
  # 本目录原来没有配置时沿用备份里的绑定；那个地址不在本机就先不起容器
  if ! bind_ok "$(state_read bind '')"; then
    warn "数据已恢复，但备份里的绑定地址 $(state_read bind '') 不在本机，容器没起"
    say "  等网络起来后重跑 deploy.sh；或把 $STATE_FILE 里的 bind 改成 bind=\"\"（改回所有网卡，无鉴权）"
    return 0
  fi
  run_container
  json_out "restore" "$srcf"
}

# ── 更新 ───────────────────────────────────────────────────────────────────
# 源码 + 插件 git pull，然后重建镜像（装新依赖）。
#   · pull 失败要明确报出来，不能照样显示「更新完成」
#   · 插件用 --autostash：本地改过的文件（如汉化插件的开关写进 config.json）先暂存、拉完放回
#   · 构建失败：旧容器还在跑，把源码和插件退回更新前的 commit，避免下次重启变成「新源码 + 旧依赖」
UPD_DIRS=(); UPD_HEADS=()
update_rollback() {
  local i
  warn "构建失败，把源码和插件退回更新前的版本 …"
  for i in "${!UPD_DIRS[@]}"; do
    [ -n "${UPD_HEADS[$i]}" ] || continue
    git -C "${UPD_DIRS[$i]}" reset -q --keep "${UPD_HEADS[$i]}" 2>/dev/null \
      || warn "  $(basename "${UPD_DIRS[$i]}") 退回失败（本地改动冲突？进目录看 git status）"
  done
  say "  旧容器没动，服务仍是更新前的版本"
  return 0
}

do_update() {
  docker_ok || die "Docker 没就绪"
  local src d failed=0
  src="$(state_read src '')"
  [ -n "$src" ] && [ -d "$src" ] || die "没有源码目录，先全新安装"
  # 绑定地址不在本机时重建必然失败：先查，别拉完代码才停（那样会留下「新源码 + 旧依赖」）
  bind_ok "$(state_read bind '')" || bind_die "$(state_read bind '')"
  local out
  UPD_DIRS=("$src"); UPD_HEADS=("$(git -C "$src" rev-parse HEAD 2>/dev/null || true)")
  say "更新源码（git pull）…"
  if out="$(cd "$src" && git pull --ff-only 2>&1)"; then
    printf '%s\n' "$out" | tail -2
  else
    printf '%s\n' "$out" | sed 's/^/    /'
    warn "源码没更新成功（原因见上）"; failed=1
  fi
  for d in "$APP_DIR"/data/custom_nodes/*/; do
    [ -d "$d/.git" ] || continue
    UPD_DIRS+=("$d"); UPD_HEADS+=("$(git -C "$d" rev-parse HEAD 2>/dev/null || true)")
    say "更新插件 $(basename "$d") …"
    if ! out="$(cd "$d" && git pull --ff-only --autostash 2>&1)"; then
      printf '%s\n' "$out" | sed 's/^/    /'
      warn "  插件 $(basename "$d") 没更新成功（原因见上）"; failed=1
    elif [ -n "$(git -C "$d" ls-files -u)" ]; then
      # 放回本地改动时冲突：git 仍返回 0 但把冲突标记写进了文件。用上游版本，改动留在 stash 里
      git -C "$d" reset -q --hard HEAD
      warn "  插件 $(basename "$d") 本地改动和上游冲突，已用上游版本；你的改动在 stash 里（进目录 git stash show -p）"
      failed=1
    else
      printf '%s\n' "$out" | tail -1
    fi
  done
  ZFC_REBUILD=1 ZFC_ON_BUILD_FAIL=update_rollback run_container
  if [ "$failed" = "1" ]; then warn "更新完成，但上面有要处理的（看 ! 开头的提示）"; else ok "更新完成"; fi
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
  say "    data/user/            网页里保存的工作流、界面设置、Manager 配置"
  say ""
  say "  ${BLD}改代码怎么生效：${RST}"
  say "  直接编辑 app/ 下的文件（nano/vim），然后："
  say "    docker restart comfyui        # 重启容器即生效（= 菜单 r）"
  say ""
  say "  ${BLD}更新到官方最新：${RST}"
  say "    $(self_cmd) --update     # 源码 + 插件 git pull，重建镜像装新依赖"
  say ""
  say "  ${BLD}接反代 API：${RST}base_url 填 New API 根地址，${BLD}不要带 /v1${RST}（节点自己拼）"
  say "    gpt-image / grok-imagine-image → APIimage「OpenAI Image Generate」"
  say "      base_url: http://<宿主机IP>:13000   模型名填 custom_model"
  say "    gemini-image（走 chat）→ relayapi「Relay API Settings」"
  say "      custom_api_base: http://<宿主机IP>:13000   api_format: v1/chat/completions"
  say "    豆包 Seedream → APIimage「ModelArk Image Generate」（只有它要带 /v1）"
  say "    api_key: 给 ComfyUI 单独建的 New API 令牌（会写进工作流和图片元数据）"
  say "  容器里的 127.0.0.1 是容器自己，填宿主机内网 IP 或 172.17.0.1"
  say "  APIimage 的 Grok / Gemini 节点别用：Grok 直连 api.x.ai 不走 base_url；"
  say "  Gemini 每次先拿令牌请求 Google 官方，失败才走 base_url"
  say "  ${YLW}端口没有鉴权${RST}：能访问的人都能从 /api/history 读到 key、直接刷你的额度；"
  say "  别直接暴露公网（Docker -p 会绕过 ufw）：菜单 5 改成只走 Caddy 域名 + 密码"
}

# ── HTTPS + 访问密码（共享机组：80/443 被香水商城 Caddy 占，复用其 Caddyfile）──
# 只生成站点片段、不改别人的 Caddyfile；端口改绑 $CADDY_GW 后，公网 IP:端口 直连就不通了
do_https() {
  docker_ok || die "Docker 没就绪"
  local port domain user pw="" hash="" reach=0 code
  local ph="comfy.你的域名"
  port="$(state_read port 8188)"
  say ""
  say "  ${BLD}配 HTTPS + 访问密码${RST} —— 这台共享机上 80/443 已被香水商城 Caddy 容器占用，"
  say "  不要在这里装新 Caddy，在它的 Caddyfile 里追加一段站点。"
  say ""
  # 已改绑时先问要不要改回：香水商城网络没起时后面生成密码哈希会失败，别让人卡在那一步
  if [ "$(state_read bind '')" = "$CADDY_GW" ]; then
    say "  端口现在只绑 $CADDY_GW:${port}：公网不能直连（本机进程和本机容器仍可免密访问）。"
    if askyn "改回所有网卡开放（IP:$port 能直连，无鉴权）？选 n 继续配域名 / 密码" "n"; then
      state_write bind ""
      run_container
      return 0
    fi
  fi
  ask domain "ComfyUI 用的域名（DNS 已指向这台机器）" "$(state_read domain "$ph")"
  [ "$domain" = "$ph" ] || state_write domain "$domain"
  ask user "登录用户名" "admin"
  if [ "${ZFC_YES:-0}" != "1" ]; then
    local pw2=""
    printf '登录密码（不回显，留空 = 不加密码）: '
    IFS= read -rs pw </dev/tty || true
    printf '\n'
    if [ -n "$pw" ]; then
      printf '再输一遍: '
      IFS= read -rs pw2 </dev/tty || true
      printf '\n'
      [ "$pw" = "$pw2" ] || die "两次密码不一致，重跑菜单 5"
    fi
  fi
  if [ -n "$pw" ]; then
    # 密码走 stdin，不放命令行（ps 看得到）
    hash="$(printf '%s\n' "$pw" | $DOCKER exec -i "$CADDY_CONTAINER" caddy hash-password || true)"
    [ -n "$hash" ] || die "没生成出密码哈希（$CADDY_CONTAINER 没在跑？）—— 先解决再重跑菜单 5"
  fi
  say ""
  [ -n "$hash" ] || say "  ${RED}没设密码：贴进去后域名对公网无鉴权开放，任何人都能从 /api/history 读到 key${RST}"
  say "  在香水商城的 Caddyfile 里追加："
  say ""
  say "    $domain {"
  if [ -n "$hash" ]; then
    say "        basic_auth {"
    say "            $user $hash"
    say "        }"
  fi
  say "        reverse_proxy $CADDY_GW:$port"
  say "    }"
  say ""
  say "  然后热加载（注意是容器里的 Caddy，不是系统 Caddy）："
  say "    docker exec $CADDY_CONTAINER caddy reload --config /etc/caddy/Caddyfile"
  say "  （Caddy 2.8 以前的版本把 basic_auth 写成 basicauth；证书由 Caddy 自动申请续期）"
  say ""
  # ── 自检（只读）：网关地址对不对；Caddy 容器 → 网关 → ComfyUI 这条路通不通
  $DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$CADDY_CONTAINER" 2>/dev/null \
    | grep -qwF -- "$CADDY_GW" \
    || warn "$CADDY_CONTAINER 的网关里没有 ${CADDY_GW}（网段变了？）—— 上面片段里的地址可能不对"
  if $DOCKER exec "$CADDY_CONTAINER" wget -q -O /dev/null -T "$PROBE_TIMEOUT" "http://$CADDY_GW:$port/" >/dev/null 2>&1; then
    ok "Caddy 容器能访问 $CADDY_GW:$port"; reach=1
  else
    warn "Caddy 容器访问不到 $CADDY_GW:${port}（容器没跑 / 防火墙挡了容器网段 / 容器里没有 wget）"
    say "    这时改绑，域名也打不开 —— 先排查再改绑"
  fi
  if [ "$domain" != "$ph" ] && askyn "已把上面的站点贴进 Caddyfile 并 reload？现在检测域名" "n"; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$PROBE_TIMEOUT" \
      --resolve "$domain:443:127.0.0.1" "https://$domain/" 2>/dev/null || true)"
    case "$code" in
      401) ok "域名要密码（401）—— basic_auth 生效了" ;;
      200) if [ -n "$hash" ]; then
             say "  ${RED}设了密码但域名免密能访问（200）：没 reload，或没贴 basic_auth${RST}"
           else
             ok "域名能访问（200，没设密码）"
           fi ;;
      000|"") warn "域名连不上：Caddy 没 reload、站点没生效，或证书还没签好（稍等再测）" ;;
      *) warn "域名返回 HTTP ${code}（502 = Caddy 连不到 ComfyUI）" ;;
    esac
  fi
  say ""
  [ "$(state_read bind '')" != "$CADDY_GW" ] || return 0
  say "  端口现在对所有网卡开放：谁访问 IP:$port 都不用密码，Caddy 的密码形同虚设。"
  askyn "改绑 $CADDY_GW:${port}，公网只能走域名？" "$([ "$reach" = "1" ] && echo y || echo n)" || return 0
  state_write bind "$CADDY_GW"
  run_container
}

# ── 反代自测：从 ComfyUI 容器里请求 New API（和节点出图走同一条路）───────────
# 令牌只经 stdin 传给容器里的 curl（-H @-），不进命令行、不落盘
API_TEST_PY='import sys,json
raw=sys.stdin.read()
try:
    d=json.loads(raw)
except Exception:
    print("  返回的不是 JSON：" + raw[:200]); sys.exit(1)
ids=[m.get("id","") for m in d.get("data",[])] if isinstance(d,dict) else []
if not ids:
    print("  没拿到模型列表：" + raw[:200]); sys.exit(1)
img=[i for i in ids if any(k in i.lower() for k in ("image","banana","seedream","flux","dall"))]
print("  令牌能用 %d 个模型，名字像出图的：" % len(ids))
print("\n".join("    " + i for i in img) or "    （没有名字带 image/banana/seedream/flux/dall 的）")'
do_api_test() {
  docker_ok || die "Docker 没就绪"
  [ -n "$($DOCKER ps -q --filter "name=^${CONTAINER}\$" 2>/dev/null || true)" ] || die "容器没在跑 —— 先菜单 r 或 1"
  local gw base code key=""
  gw="$($DOCKER inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$CONTAINER" 2>/dev/null | awk '{print $1}')"
  say ""
  say "  从 ComfyUI 容器里测 New API（和节点出图走同一条路）"
  ask base "New API 根地址（不带 /v1）" "$(state_read api_base "http://${gw:-172.17.0.1}:13000")"
  base="${base%/}"
  state_write api_base "$base"
  code="$($DOCKER exec "$CONTAINER" curl -s -o /dev/null -w '%{http_code}' --max-time "$PROBE_TIMEOUT" "$base/v1/models" 2>/dev/null || true)"
  case "$code" in
    200|401|403) ok "连通（HTTP ${code}）：容器能访问 $base" ;;
    404) warn "404：路径不对 —— 地址是不是多带了 /v1？节点里的 base_url 也要去掉（豆包 Seedream 节点除外，它要带 /v1）"; return 0 ;;
    000|"") warn "连不上（${PROBE_TIMEOUT} 秒内没响应）："
            say "    · New API 是不是只监听了 127.0.0.1？要监听 0.0.0.0"
            say "    · 容器里的 127.0.0.1 是容器自己，要填宿主机内网 IP 或 ${gw:-172.17.0.1}"
            say "    · 宿主机防火墙有没有放行容器网段"
            return 0 ;;
    *) warn "返回 HTTP $code —— 看 New API 日志"; return 0 ;;
  esac
  # 可选：带令牌列出能用的出图模型，核对令牌有没有这些模型的权限
  if [ "${ZFC_YES:-0}" != "1" ]; then
    printf '  New API 令牌（不回显，留空 = 跳过）: '
    IFS= read -rs key </dev/tty || true
    printf '\n'
  fi
  [ -n "$key" ] || return 0
  printf 'Authorization: Bearer %s\n' "$key" \
    | $DOCKER exec -i "$CONTAINER" sh -c 'curl -s --max-time "$1" -H @- "$2/v1/models" | python -c "$3"' \
        _ "$PROBE_TIMEOUT" "$base" "$API_TEST_PY" \
    || warn "令牌可能无效，或没有模型权限（看上面的返回）"
}

# ── 日志 ───────────────────────────────────────────────────────────────────
do_logs() {
  docker_ok || die "Docker 没就绪"
  say ""
  say "  1) 最近 $LOG_TAIL 行"
  say "  2) 实时跟随（Ctrl+C 退出）"
  say "  3) 只看本次启动的问题（插件加载表 + 报错）"
  ask LW "选一个" "1"
  case "$LW" in
    2) $DOCKER logs -f --tail 50 "$CONTAINER" 2>&1 || true ;;
    3) local since logs
       since="$($DOCKER inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null || true)"
       logs="$($DOCKER logs ${since:+--since "$since"} "$CONTAINER" 2>&1 || true)"
       say ""
       say "  ${BLD}插件加载表${RST}（带 IMPORT FAILED 的就是没装上）："
       printf '%s\n' "$logs" | sed -n '/Import times for custom nodes/,/^[[:space:]]*$/p'
       say "  ${BLD}报错${RST}："
       printf '%s\n' "$logs" | grep -a -E 'IMPORT FAILED|Traceback|ModuleNotFoundError|ImportError|Cannot import' | tail -n 50 \
         || ok "本次启动没发现报错" ;;
    *) $DOCKER logs --tail "$LOG_TAIL" "$CONTAINER" 2>&1 || true ;;
  esac
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
    say "  插件：$(plugin_list | tr '\n' ' ')"
  fi
  say ""
  say "  访问地址："
  local ips=""
  ips="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -z "$ips" ] && ips="$(ip -4 addr show 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | grep -v '^127\.' | head -1)"
  if [ "$(probe_host)" = "127.0.0.1" ]; then
    say "    http://${ips:-<本机IP>}:$port/   ${YLW}（所有网卡开放，无鉴权；菜单 5 可改成只走 Caddy 域名 + 密码）${RST}"
  else
    say "    只对 $(probe_host):$port 开放 —— 走 Caddy 配的域名访问（菜单 5）"
  fi
  say ""
  if [ -n "$running" ]; then
    if curl -fsS -o /dev/null --max-time 5 "http://$(probe_host):$port/" 2>/dev/null; then
      ok "HTTP 200 —— 服务正常"
    else
      warn "容器在跑但页面没响应（首次启动装依赖较慢）—— 菜单选 l 看日志"
    fi
  else
    warn "容器没在跑 —— 菜单选 1 重新部署，或 2 更新"
  fi
  json_out "check" "port=$port running=$([ -n "$running" ] && echo yes || echo no)"
}

# ── 卸载 ───────────────────────────────────────────────────────────────────
do_uninstall() {
  docker_ok || { warn "Docker 都没就绪，没什么可卸的"; return 0; }
  local wipe="${1:-0}"
  say ""
  if [ "$wipe" = "1" ]; then
    say "  ${RED}卸载并删掉安装目录${RST}（${APP_DIR}，含源码/插件/数据，${RED}backups/ 里的备份也会删${RST}，要留先挪走）。"
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
    say "安装目录保留：${APP_DIR}（以后想再起，回这里跑 deploy.sh 选 1）"
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
      say "  --update            更新（源码 git pull + 插件更新 + 重建镜像 + 重建容器）"
      say "  --check             体检 + 看访问地址"
      say "  --uninstall         卸载（加 --purge 连目录一起删）"
      say "  --plugins           插件管理"
      say "  --plugins-set <n>   非交互装插件组（1,2 或 all）"
      say "  --backup            备份数据（data/ + 配置）"
      say "  --restore           恢复备份（加文件路径参数）"
      say ""
      say "安装选项："
      say "  --dir <目录>        安装目录（默认脚本所在目录；换目录会替换现有这套）"
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
# 套件文件（deploy.sh / Dockerfile / .dockerignore）所在目录：装到别的目录时要拷过去
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd -P || true)"
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
if [ "$ZFC_ACTION" = "update" ]; then do_update; exit 0; fi

# 已装过 → 菜单
INSTALLED=0
docker_ok && [ "$($DOCKER ps -aq --filter "name=$CONTAINER" 2>/dev/null | wc -l | tr -d ' ')" -ge 1 ] && INSTALLED=1

if [ "$INSTALLED" = "1" ] && [ "$ZFC_ACTION" != "install" ]; then
  say "这台机器上${BLD}已经装过${RST}了（容器 ${CONTAINER}，端口 $(state_read port 8188)）"
  say ""
  say "  1) ${BLD}全新安装${RST} / 重新部署（容器只有一个，换目录会替换现有这套）"
  say "  2) ${BLD}更新到最新版${RST}（源码 git pull + 插件更新 + 重建镜像 + 重建容器）"
  say "  3) 体检 + 看访问地址"
  say "  4) 改监听端口"
  say "  5) 配 HTTPS + 访问密码（复用香水商城 Caddy，公网只走域名）"
  say "  6) 插件管理（汉化 / APIimage / relayapi / 自定义）"
  say "  7) 数据说明（源码在哪、怎么改代码、怎么接反代）"
  say "  8) ${BLD}一键备份${RST}（插件+工作流+输出+配置 → backups/）"
  say "  9) 一键恢复${RST}（从备份文件还原数据并重启）"
  say "  10) ${RED}卸载${RST}"
  say "  11) 测试反代（容器 → New API 连通 + 令牌能用哪些出图模型）"
  say "  l) 看日志（最近 / 实时 / 只看问题）"
  say "  r) 重启容器（docker restart，不删容器，现场装的包还在）"
  say "  R) 重建容器（删了重新 run，现场装的包会丢）"
  say "  0) 退出"
  ask WHAT "选一个" "1"
  case "$WHAT" in
    1) : ;;
    2) do_update; exit 0 ;;
    3) do_check; exit 0 ;;
    4) ask NP "改成哪个端口" "$(state_read port 8188)"
       port_check "$NP"
       port_busy "$NP" && die "端口 $NP 被别的进程占着"
       state_write port "$NP"
       run_container
       ok "端口已改为 $NP"
       [ -z "$(state_read bind '')" ] || warn "记得把 Caddyfile 里的 reverse_proxy 改成 $CADDY_GW:$NP 并 reload，否则域名会 502"
       exit 0 ;;
    5) do_https; exit 0 ;;
    6) do_plugins; exit 0 ;;
    7) do_data_info; exit 0 ;;
    8) do_backup; exit 0 ;;
    9) do_restore; exit 0 ;;
    11) do_api_test; exit 0 ;;
    10) say ""
        say "  a) 卸载但${GRN}保留目录${RST}（容器停掉，目录留着）"
        say "  b) 卸载并${RED}删掉整个目录${RST}（源码/插件/数据/备份全没了）"
        ask UW "选一个" "a"
        do_uninstall "$([ "$UW" = "b" ] && echo 1 || echo 0)"; exit 0 ;;
    l|L) do_logs; exit 0 ;;
    r) restart_container; exit 0 ;;
    R) run_container; exit 0 ;;
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
# 自家容器占着同一个端口（重新部署这一套）不算冲突，run_container 会先删旧容器
if [ "$INSTALLED" = "0" ] || [ "$PORT" != "$(state_read port '')" ]; then
  port_busy "$PORT" && die "端口 $PORT 被别的进程占着（换一个）"
fi

ask_opt GAURL "源码仓库（留空 = 官方 comfyanonymous/ComfyUI）" ""
[ -n "$ZFC_GIT" ] && GAURL="$ZFC_GIT"

say ""
say "  确认一下："
say "    目录：$APP_DIR"
say "    端口：$PORT"
say "    源码：${GAURL:-官方 https://github.com/comfyanonymous/ComfyUI.git}"
if [ "$(probe_host)" = "127.0.0.1" ]; then say "    访问：所有网卡（IP:端口 直连）"; else say "    访问：公网只走 Caddy 域名（端口绑 $(probe_host)）"; fi
askyn "开始部署？" "y" || { say "取消"; exit 1; }

state_write port "$PORT"

# 装到别的目录：把套件文件拷过去（构建镜像要用 Dockerfile）
if [ ! -f "$APP_DIR/Dockerfile" ] && [ -n "$KIT_DIR" ]; then
  for f in deploy.sh Dockerfile .dockerignore; do
    [ -f "$KIT_DIR/$f" ] && cp "$KIT_DIR/$f" "$APP_DIR/"
  done
fi

# 安装也强制重建：卸载只删容器不删镜像，重装时不能复用旧镜像
ZFC_REBUILD=1 run_container

say ""
if [ "$(probe_host)" = "127.0.0.1" ]; then
  ok "完事。访问地址：http://<这台机器的IP>:$PORT/"
else
  ok "完事。端口绑在 $(probe_host):${PORT}，走 Caddy 域名访问（换过端口记得同步 Caddyfile）"
fi
say ""
say "${BLD}下一步建议${RST}：菜单选 6 装【汉化 + APIimage + relayapi】三件套，"
say "然后在节点里填你的反代 base_url + key 就能出图（怎么填：菜单选 7）。"
say ""
say "以后要更新：菜单选 2（一条命令：$(self_cmd) --update）"
json_out "install" "port=$PORT"