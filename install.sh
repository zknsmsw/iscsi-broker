#!/usr/bin/env bash
# ============================================================
#  iSCSI Broker 服务端一键安装（Linux）
#
#  在 clone 出来的仓库根目录执行：
#      sudo bash install.sh
#
#  它会做四件事：
#      1) 按发行版安装依赖（python3 / tgt / qemu-utils / iproute2 / arping /
#         dnsmasq / ipxe / iptables / kmod 等）；
#      2) 把程序文件装到 /opt/iscsi-broker（源码目录不动，方便以后 git pull）；
#      3) 准备数据根目录（母盘 images/、网盘 cloud/ 等）并写 /etc/iscsi-broker/*.env；
#      4) 注册并启动 systemd 服务 iscsi-broker.service。
#
#  常用参数：
#      --base-dir DIR     数据根目录（母盘/网盘/配置放这里），默认 /home/prts/server
#      --install-dir DIR  程序安装目录，默认 /opt/iscsi-broker
#      --no-deps          跳过依赖安装
#      --no-tftp          不往 TFTP 目录复制 iPXE 引导文件
#      --no-start         只注册服务，不立即启动
#      --uninstall        停止并卸载服务（数据目录保留）
#      --purge            配合 --uninstall：连程序目录一起删（数据目录仍保留）
#      -h | --help        帮助
# ============================================================
set -euo pipefail

APP="iscsi-broker"
INSTALL_DIR="/opt/iscsi-broker"
BASE_DIR="/home/prts/server"
ENV_DIR="/etc/iscsi-broker"
ENV_FILE="$ENV_DIR/$APP.env"
UNIT_FILE="/etc/systemd/system/$APP.service"
UNIT_NAME="$APP.service"
TFTP_DIR="/srv/tftp"
DO_DEPS=1
DO_TFTP=1
DO_START=1
DO_UNINSTALL=0
DO_PURGE=0
ASSUME_YES=0
IN_PLACE=0           # --in-place：不拷贝，直接给当前目录注册 systemd 服务
BASE_DIR_GIVEN=0
PY_BIN=""
PROG_DIR=""          # 实际运行代码的目录（原地模式=当前目录，否则=/opt/iscsi-broker）
LEGACY_BASE=""       # 从旧部署里探测到的数据目录
LEGACY_SRC=""        # 旧部署的程序目录/源码位置

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROG_FILES=(iscsi_broker.py users_auth.py cloud_store.py netctrl.py agent_hub.py webdav.py wsbridge.py)

c_ok=$'\033[32m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_info=$'\033[36m'; c_end=$'\033[0m'
[[ -t 1 ]] || { c_ok=""; c_warn=""; c_err=""; c_info=""; c_end=""; }

msg()  { echo "${c_info}[$APP]${c_end} $*"; }
ok()   { echo "${c_ok}[ OK ]${c_end} $*"; }
warn() { echo "${c_warn}[WARN]${c_end} $*"; }
die()  { echo "${c_err}[FAIL]${c_end} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：sudo bash install.sh [参数]
  首次安装和升级都用这一条：已经装过就自动按“升级”处理（沿用原数据目录、备份旧程序、重启服务）。

  两种装法，选一个：
    （默认）拷贝到 /opt/iscsi-broker 再注册服务：
        好处是“部署”和“源码”分离——systemd 执行的文件 root 独占、clone 可以随便移动或删除、
        git 操作不会影响正在跑的代码；代价是每次 git pull 后要重跑本脚本。
    --in-place  不拷贝，直接给当前目录注册服务（推荐给“仓库就放服务器上、自己维护”的场景）：
        一次 git pull + systemctl restart 就生效，没有两份代码；前提是这个目录只有 root 能写
        （脚本会检查并提示 chown/chmod），否则普通用户改一下 iscsi_broker.py 就能拿到 root。

  --base-dir DIR     数据根目录（母盘/网盘/配置放这里）。不写的话：先读 /etc/iscsi-broker/*.env，
                     再探测旧部署源码/正在运行的实例，最后才用默认 /home/prts/server
  --install-dir DIR  程序安装目录（仅默认装法用），默认 /opt/iscsi-broker
  --in-place         不拷贝程序，直接用当前目录（配合 git pull 升级最方便）
  --no-deps          跳过依赖安装（升级时常用：sudo bash install.sh --no-deps）
  --no-tftp          不往 TFTP 目录复制 iPXE 引导文件
  --no-start         只注册服务，不立即启动
  -y | --yes         所有询问都自动回答“是”（无人值守）
  --uninstall        停止并卸载服务（数据目录保留；原地模式不会删源码目录）
  --purge            卸载并删除程序目录（--in-place 时不生效）
  -h | --help        显示本帮助

升级/迁移：
  默认装法：cd 仓库 && git pull && sudo bash install.sh --no-deps
  原地装法：cd 仓库 && git pull && sudo systemctl restart iscsi-broker
  接管旧部署（源码里手改过 BASE_DIR、进程是手跑的）：在旧目录跑一次本脚本，它会抓出旧数据
      目录、停掉旧进程、然后交给 systemd 统一管理；跑完建议 git checkout -- iscsi_broker.py
      （数据目录已写进 env 文件，源码不用再改），以后 git pull 就不会冲突。
  回滚：默认装法用 <install-dir>.bak；原地装法用 git（git checkout <旧提交> 后 restart）。
EOF
  exit 0
}

# ---------------- 参数 ----------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir)    BASE_DIR="${2:-}"; BASE_DIR_GIVEN=1; shift 2;;
    --install-dir) INSTALL_DIR="${2:-}"; shift 2;;
    --no-deps)     DO_DEPS=0; shift;;
    --no-tftp)     DO_TFTP=0; shift;;
    --no-start)    DO_START=0; shift;;
    --in-place)    IN_PLACE=1; shift;;
    -y|--yes)      ASSUME_YES=1; shift;;
    --uninstall)   DO_UNINSTALL=1; shift;;
    --purge)       DO_UNINSTALL=1; DO_PURGE=1; shift;;
    -h|--help)     usage;;
    *) die "未知参数：$1（用 --help 看用法）";;
  esac
done

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash install.sh"
[[ -n "$BASE_DIR" && "$BASE_DIR" = /* ]] || die "--base-dir 必须是绝对路径"
[[ -n "$INSTALL_DIR" && "$INSTALL_DIR" = /* ]] || die "--install-dir 必须是绝对路径"

# ---------------- 发行版 / 包管理器 ----------------
DISTRO_ID=""; DISTRO_LIKE=""; PM=""
detect_distro() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_ID="${ID:-}"; DISTRO_LIKE="${ID_LIKE:-}"
  fi
  case " $DISTRO_ID $DISTRO_LIKE " in
    *" debian "*|*" ubuntu "*|*" raspbian "*|*" linuxmint "*) PM="apt";;
    *" fedora "*|*" rhel "*|*" centos "*|*" rocky "*|*" almalinux "*) PM="dnf";;
    *" opensuse "*|*" suse "*|*" sles "*) PM="zypper";;
    *" arch "*|*" manjaro "*) PM="pacman";;
    *) PM="";;
  esac
  if [[ -z "$PM" ]]; then
    for c in apt-get dnf yum zypper pacman; do
      if command -v "$c" >/dev/null 2>&1; then
        case "$c" in apt-get) PM="apt";; dnf|yum) PM="dnf";; zypper) PM="zypper";; pacman) PM="pacman";; esac
        break
      fi
    done
  fi
  [[ -n "$PM" ]] || warn "认不出发行版（ID=${DISTRO_ID:-?}），依赖请自己装，见 README 第四节"
}

set_pkg_lists() {
  case "$PM" in
    apt)    PKGS=(python3 tgt qemu-utils iproute2 iputils-arping util-linux dnsmasq iptables kmod); OPT_PKGS=(ipxe);;
    dnf)    PKGS=(python3 tgt qemu-img iproute iputils util-linux dnsmasq iptables kmod);           OPT_PKGS=(ipxe-bootimgs);;
    zypper) PKGS=(python3 tgt qemu-tools iproute2 iputils util-linux dnsmasq iptables kmod);        OPT_PKGS=(ipxe-bootimgs);;
    pacman) PKGS=(python3 tgt qemu-img iproute2 iputils util-linux dnsmasq iptables kmod);          OPT_PKGS=(ipxe);;
    *)      PKGS=(); OPT_PKGS=();;
  esac
}

install_deps() {
  [[ $DO_DEPS -eq 1 ]] || { warn "按参数要求跳过依赖安装"; return 0; }
  [[ -n "$PM" ]] || { warn "不知道用什么包管理器，跳过依赖安装（见 README 第四节）"; return 0; }
  set_pkg_lists
  msg "安装依赖（$PM）：${PKGS[*]}"
  case "$PM" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      if ! apt-get update -qq; then warn "apt-get update 失败（离线？），继续尝试安装"; fi
      apt-get install -y --no-install-recommends "${PKGS[@]}" || warn "部分依赖安装失败，请稍后手动补：apt install ${PKGS[*]}"
      apt-get install -y --no-install-recommends "${OPT_PKGS[@]}" 2>/dev/null || warn "可选包 ${OPT_PKGS[*]} 装不上（iPXE 引导文件可自己编译/拷贝）"
      ;;
    dnf)
      (command -v dnf >/dev/null 2>&1 && dnf install -y "${PKGS[@]}") || \
      (command -v yum >/dev/null 2>&1 && yum install -y "${PKGS[@]}") || \
        warn "依赖安装失败，请手动补：dnf install ${PKGS[*]}"
      (command -v dnf >/dev/null 2>&1 && dnf install -y "${OPT_PKGS[@]}") 2>/dev/null || \
        warn "可选包 ${OPT_PKGS[*]} 装不上（iPXE 引导文件可自己编译/拷贝）"
      ;;
    zypper)
      zypper --non-interactive install "${PKGS[@]}" || warn "依赖安装失败，请手动补：zypper install ${PKGS[*]}"
      zypper --non-interactive install "${OPT_PKGS[@]}" 2>/dev/null || warn "可选包 ${OPT_PKGS[*]} 装不上"
      ;;
    pacman)
      pacman -Sy --noconfirm --needed "${PKGS[@]}" || warn "依赖安装失败，请手动补：pacman -S ${PKGS[*]}"
      pacman -S --noconfirm --needed "${OPT_PKGS[@]}" 2>/dev/null || warn "可选包 ${OPT_PKGS[*]} 装不上"
      ;;
  esac
  ok "依赖处理完成"
}

check_python() {
  PY_BIN="$(command -v python3 || true)"
  [[ -n "$PY_BIN" ]] || die "没找到 python3，请先安装 python3（程序需要 3.10+）"
  local ver
  ver="$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [[ -z "$ver" ]]; then
    die "$PY_BIN 跑不起来（无输出）：请确认 '$PY_BIN -V' 能正常输出版本"
  fi
  "$PY_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "python3 版本太低：$ver，需要 3.10+"
  ok "Python：$PY_BIN（$ver）"
}

# ---------------- 已装/旧部署探测（升级与迁移用） ----------------
confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  [[ -t 0 ]] || return 1
  local ans=""
  read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

base_dir_from_file() {
  # 老部署是把 BASE_DIR 直接写在源码里的（形如 BASE_DIR = "/xxx"），这里抓出来；
  # 新源码写的是 BASE_DIR = _resolve_base_dir()，没有引号字面量，所以不会误抓。
  local f="$1"
  [[ -f "$f" ]] || return 0
  sed -n 's/^[[:space:]]*BASE_DIR[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$f" | head -1
}

env_file_base_dir() {
  [[ -r "$ENV_FILE" ]] || return 0
  sed -n 's/^[[:space:]]*ISCSI_BROKER_BASE_DIR[[:space:]]*=[[:space:]]*\(.*\)$/\1/p' "$ENV_FILE" \
    | head -1 | tr -d '"' | tr -d "'"
}

proc_script() {
  # 进程命令行里的 iscsi_broker.py 路径
  local pid="$1"
  tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep 'iscsi_broker\.py$' | head -1
}

running_brokers() {
  # 输出 "pid<TAB>脚本路径"（脚本路径可能为空）；永远返回 0，避免 set -e 把安装中断
  local pid script
  if command -v pgrep >/dev/null 2>&1; then
    for pid in $(pgrep -f 'iscsi_broker\.py' 2>/dev/null || true); do
      [[ "$pid" == "$$" ]] && continue
      script="$(proc_script "$pid")"
      printf '%s\t%s\n' "$pid" "$script"
    done
  else
    ps -eo pid=,args= 2>/dev/null | grep '[i]scsi_broker\.py' | while read -r pid rest; do
      printf '%s\t%s\n' "$pid" "$(echo "$rest" | tr ' ' '\n' | grep 'iscsi_broker\.py$' | head -1)"
    done
  fi
  return 0
}

foreign_units() {
  # 引用了 iscsi_broker.py 的 systemd 单元（排除我们自己的）
  local f
  for f in /etc/systemd/system/*.service /etc/systemd/system/*/*.service; do
    [[ -f "$f" ]] || continue
    [[ "$(basename "$f")" == "$UNIT_NAME" ]] && continue
    if grep -qs 'iscsi_broker\.py' "$f"; then basename "$f"; fi
  done
}

detect_legacy() {
  local e pid script base
  e="$(env_file_base_dir)"
  if [[ -n "$e" ]]; then
    LEGACY_BASE="$e"; LEGACY_SRC="$INSTALL_DIR"
    msg "检测到已安装（存在 $ENV_FILE）：数据目录 $e —— 按“升级”处理，数据不动"
    return 0
  fi
  # 从正在跑的旧实例上抓它的数据目录
  while IFS=$'\t' read -r pid script; do
    [[ -n "${pid:-}" ]] || continue
    if [[ -z "${script:-}" && -d "/proc/$pid" ]]; then
      script="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)/iscsi_broker.py"
    fi
    base="$(base_dir_from_file "$script")"
    if [[ -n "$base" && -z "$LEGACY_BASE" ]]; then
      LEGACY_BASE="$base"; LEGACY_SRC="$(dirname "$script")"
    fi
  done < <(running_brokers)
  # 再扫常见位置里“老源码”写死的 BASE_DIR（含你现在这个 clone）
  if [[ -z "$LEGACY_BASE" ]]; then
    for script in "$SRC_DIR/iscsi_broker.py" "$INSTALL_DIR/iscsi_broker.py" \
                  /root/iscsi-broker/iscsi_broker.py /opt/iscsi-broker/iscsi_broker.py; do
      base="$(base_dir_from_file "$script")"
      if [[ -n "$base" ]]; then
        LEGACY_BASE="$base"; LEGACY_SRC="$(dirname "$script")"
        break
      fi
    done
  fi
  if [[ -n "$LEGACY_BASE" ]]; then
    msg "检测到旧部署（$LEGACY_SRC）：数据目录 $LEGACY_BASE —— 会接管并沿用它"
  fi
}

resolve_base_dir() {
  if [[ $BASE_DIR_GIVEN -eq 1 ]]; then
    msg "数据目录（--base-dir 指定）：$BASE_DIR"
  elif [[ -n "$LEGACY_BASE" ]]; then
    BASE_DIR="$LEGACY_BASE"
    msg "沿用已有数据目录：$BASE_DIR（升级/接管不会动里面的数据）"
  else
    msg "没探测到已有安装，使用默认数据目录：$BASE_DIR"
    if [[ ! -d "$BASE_DIR" ]]; then
      warn "该目录不存在，会新建。若你的母盘/网盘在别处，请用 --base-dir 指定，例如："
      warn "    sudo bash install.sh --base-dir /你的/实际/数据目录"
    fi
  fi
}

warn_dirty_source() {
  command -v git >/dev/null 2>&1 || return 0
  [[ -d "$SRC_DIR/.git" ]] || return 0
  if ! git -C "$SRC_DIR" diff --quiet -- iscsi_broker.py 2>/dev/null; then
    if git -C "$SRC_DIR" diff -- iscsi_broker.py 2>/dev/null | grep -q 'BASE_DIR'; then
      warn "本目录的 iscsi_broker.py 把 BASE_DIR 改在源码里了（老部署常见做法）。"
      warn "数据目录已经写进 $ENV_FILE，装完后建议执行下面这条，以后 git pull 才不会冲突："
      warn "    git -C $SRC_DIR checkout -- iscsi_broker.py"
    else
      warn "本目录源码有未提交改动（升级前建议先 git stash 或提交，免得 git pull 冲突）"
    fi
  fi
}

stop_old_instances() {
  local units unit rows pid
  units="$(foreign_units)"
  if [[ -n "$units" ]]; then
    warn "发现别的 systemd 单元也在跑本程序（会和新服务抢 5000/8080 端口）："
    echo "$units" | sed 's/^/         /'
    if confirm "停用并删除这些单元，统一交给 $UNIT_NAME 管理？"; then
      for unit in $units; do
        systemctl stop "$unit" 2>/dev/null || true
        systemctl disable "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
        ok "已停用并删除 $unit"
      done
      command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
    else
      warn "保留这些单元：请自己确认它们不会和新服务同时跑"
    fi
  fi
  rows="$(running_brokers || true)"
  [[ -n "$rows" ]] || return 0
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$UNIT_NAME" 2>/dev/null; then
    msg "旧实例由 $UNIT_NAME 管理，稍后 restart 会替换掉它"
    return 0
  fi
  warn "发现正在运行的 iscsi_broker.py 进程（不是 systemd 管理的）："
  echo "$rows" | sed 's/^/         /'
  if confirm "现在停掉它们（否则新服务起不来）？"; then
    while IFS=$'\t' read -r pid _rest; do
      [[ -n "${pid:-}" ]] && kill "$pid" 2>/dev/null || true
    done <<< "$rows"
    sleep 2
    while IFS=$'\t' read -r pid _rest; do
      [[ -n "${pid:-}" ]] && kill -9 "$pid" 2>/dev/null || true
    done <<< "$rows"
    ok "已停止旧实例"
  else
    warn "没停旧实例：新服务可能因端口被占用而起不来"
  fi
}

backup_prev() {
  if [[ $IN_PLACE -eq 1 ]]; then
    msg "原地模式不备份程序目录（版本回滚用 git）"
    return 0
  fi
  [[ -f "$INSTALL_DIR/iscsi_broker.py" ]] || return 0
  local bak="$INSTALL_DIR.bak"
  rm -rf "$bak"
  if cp -a "$INSTALL_DIR" "$bak"; then
    ok "已备份旧程序目录到 $bak（回滚：rm -rf $INSTALL_DIR && mv $bak $INSTALL_DIR && systemctl restart $UNIT_NAME）"
  else
    warn "备份旧程序目录失败，继续安装"
  fi
}

# ---------------- 安装/卸载 ----------------
install_files() {
  local f
  for f in "${PROG_FILES[@]}"; do
    [[ -f "$SRC_DIR/$f" ]] || die "缺少 $f —— 请在仓库根目录运行本脚本"
  done
  if [[ $IN_PLACE -eq 1 ]]; then
    PROG_DIR="$SRC_DIR"
    msg "原地安装：代码就留在 $PROG_DIR（不拷贝，升级靠 git pull）"
    return 0
  fi
  msg "安装程序到 $INSTALL_DIR"
  mkdir -p "$INSTALL_DIR"
  for f in "${PROG_FILES[@]}"; do
    install -m 0644 "$SRC_DIR/$f" "$INSTALL_DIR/$f"
  done
  if [[ -d "$SRC_DIR/web" ]]; then
    rm -rf "$INSTALL_DIR/web"
    cp -a "$SRC_DIR/web" "$INSTALL_DIR/web"
  else
    warn "源码里没有 web/ 目录，VNC 控制页（noVNC）会不可用"
  fi
  if [[ -f "$SRC_DIR/README.md" ]]; then
    install -m 0644 "$SRC_DIR/README.md" "$INSTALL_DIR/README.md"
  fi
  PROG_DIR="$INSTALL_DIR"
  ok "程序文件就绪"
}

# 原地模式的权限检查：systemd 以 root 跑这里的代码，所以只能 root 能写
check_in_place_safety() {
  [[ $IN_PLACE -eq 1 ]] || return 0
  [[ -f "$SRC_DIR/iscsi_broker.py" ]] || die "--in-place 需要在有 iscsi_broker.py 的目录里运行"
  local owner unsafe
  owner="$(stat -c '%U' "$SRC_DIR" 2>/dev/null || echo '?')"
  unsafe="$(find "$SRC_DIR" -maxdepth 1 \( -name '*.py' -o -name 'web' -o -name '.' \) -perm /022 -print 2>/dev/null | head -3 || true)"
  if [[ "$owner" != "root" ]]; then
    warn "原地模式：$SRC_DIR 属主是 $owner（不是 root）。systemd 会以 root 执行这里的代码，"
    warn "任何能写这个目录的用户都等于能拿到 root。"
    if confirm "现在 chown -R root:root $SRC_DIR（之后 git pull 也要用 root 执行）？"; then
      chown -R root:root "$SRC_DIR" && ok "已改为 root 所有"
    else
      warn "保持原样 —— 请自行确保该目录只有 root 可写"
    fi
  fi
  if [[ -n "$unsafe" ]]; then
    warn "以下路径对同组/其他用户可写："
    echo "$unsafe" | sed 's/^/         /'
    if confirm "去掉组/其他的写权限（chmod -R go-w $SRC_DIR）？"; then
      chmod -R go-w "$SRC_DIR" && ok "已收紧写权限"
    fi
  fi
}

prepare_data_dir() {
  msg "准备数据目录 $BASE_DIR"
  mkdir -p "$BASE_DIR/images" "$BASE_DIR/cloud"
  # 数据目录里有 admin.conf/users.conf/网盘内容，收紧到只有 root 能进（tgtd 本身也是 root 跑）
  chmod 0700 "$BASE_DIR" 2>/dev/null || true
  if ! ls -1 "$BASE_DIR"/images/*.raw >/dev/null 2>&1; then
    warn "images/ 里还没有母盘：把母盘 .raw 放进去后（文件名如 win11.raw）iPXE 菜单才会出现它"
  fi
  ok "数据目录就绪（母盘放 $BASE_DIR/images/）"
}

write_env_file() {
  mkdir -p "$ENV_DIR"
  cat > "$ENV_FILE" <<EOF
# iSCSI Broker 运行参数（由 install.sh 生成，systemd 通过 EnvironmentFile 注入）
# 数据根目录：母盘 images/、网盘 cloud/、叠加盘与各配置文件都在这里
ISCSI_BROKER_BASE_DIR=$BASE_DIR
# 日志直接进 journald，不缓冲
PYTHONUNBUFFERED=1
EOF
  chmod 0644 "$ENV_FILE"
  ok "写配置文件 $ENV_FILE"
}

write_unit() {
  mkdir -p "$(dirname "$UNIT_FILE")"
  cat > "$UNIT_FILE" <<EOF
[Unit]
Description=iSCSI Broker (iPXE 无盘启动 + iSCSI 供给 + Web 管理后台 + 个人网盘/客户机控制)
Documentation=file://$PROG_DIR/README.md
After=network-online.target tgt.service
Wants=network-online.target
# 说明：tgt.service 由 tgt 包提供；若该单元不存在，systemd 只会记一条告警，不影响启动

[Service]
Type=simple
User=root
# tgtadm / qemu-nbd / modprobe / iptables 都需要 root
WorkingDirectory=$PROG_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=$PY_BIN $PROG_DIR/iscsi_broker.py
Restart=on-failure
RestartSec=3
TimeoutStopSec=15
LimitNOFILE=65536
# 停止服务不会自动删掉已导出的 iSCSI target（tgt 自己还活着）；下次启动会做一次清理

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$UNIT_FILE"
  ok "写 systemd 单元 $UNIT_FILE（ExecStart=$PROG_DIR/iscsi_broker.py）"
}

setup_tftp() {
  [[ $DO_TFTP -eq 1 ]] || return 0
  local src=""
  for candidate in /usr/lib/ipxe /usr/share/ipxe /usr/share/syslinux /usr/lib/syslinux; do
    [[ -d "$candidate" ]] && src="$candidate" && break
  done
  if [[ -z "$src" ]]; then
    warn "没找到 iPXE 引导文件（undionly.kpxe / ipxe.efi），dnsmasq 那步需要它，见 README 第五节 4"
    return 0
  fi
  if [[ ! -d "$TFTP_DIR" ]]; then
    warn "$TFTP_DIR 不存在，跳过 iPXE 引导文件复制（dnsmasq 的 tftp-root 建好后把 $src 里的文件拷进去）"
    return 0
  fi
  local copied=0
  for f in undionly.kpxe ipxe.efi; do
    if [[ -f "$src/$f" && ! -f "$TFTP_DIR/$f" ]]; then
      cp -f "$src/$f" "$TFTP_DIR/$f" && copied=$((copied+1))
    fi
  done
  ok "TFTP 目录 $TFTP_DIR：复制了 $copied 个 iPXE 引导文件（原有的不覆盖）"
}

enable_service() {
  command -v systemctl >/dev/null 2>&1 || { warn "没有 systemd（systemctl），请手动运行：$PY_BIN $INSTALL_DIR/iscsi_broker.py"; return 0; }
  if systemctl list-unit-files 2>/dev/null | grep -q '^tgt\.service'; then
    msg "启动 tgt 服务（iSCSI target 守护进程）"
    systemctl enable --now tgt 2>/dev/null || warn "tgt 服务启动失败，请检查：systemctl status tgt"
  else
    warn "系统里没有 tgt.service（tgt 包可能没装成功），请确认 tgtadm 可用"
  fi
  systemctl daemon-reload
  systemctl enable "$UNIT_NAME" >/dev/null 2>&1 || warn "systemctl enable 失败（容器里没 systemd？）"
  ok "已注册开机自启：$UNIT_NAME"
  if [[ $DO_START -eq 1 ]]; then
    systemctl restart "$UNIT_NAME" || warn "systemctl restart 返回失败，继续检查状态"
    sleep 2
    if systemctl is-active --quiet "$UNIT_NAME"; then
      ok "服务已启动：systemctl status $UNIT_NAME"
    else
      warn "服务没起来，看日志：journalctl -u $UNIT_NAME -n 50 --no-pager"
      return 0
    fi
  else
    msg "按参数要求没有立即启动，可手动：systemctl start $UNIT_NAME"
  fi
}

do_uninstall() {
  msg "卸载 $APP"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "$UNIT_NAME" 2>/dev/null || true
    systemctl disable "$UNIT_NAME" 2>/dev/null || true
  fi
  rm -f "$UNIT_FILE"
  command -v systemctl >/dev/null 2>&1 && systemctl daemon-reload || true
  rm -f "$ENV_FILE"; rmdir "$ENV_DIR" 2>/dev/null || true
  if [[ $DO_PURGE -eq 1 ]]; then
    if [[ $IN_PLACE -eq 1 ]]; then
      warn "--purge 在 --in-place 模式下不删除源码目录（$SRC_DIR），要删请自己处理"
    else
      rm -rf "$INSTALL_DIR"
      ok "已删除程序目录 $INSTALL_DIR"
    fi
  else
    msg "程序目录保留：$PROG_DIR"
  fi
  ok "卸载完成；数据目录 $BASE_DIR 未改动"
  exit 0
}

print_summary() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1 || true)"
  [[ -n "$ip" ]] || ip="<服务器IP>"
  echo
  echo "${c_ok}========================================${c_end}"
  echo "${c_ok} iSCSI Broker 安装完成${c_end}"
  echo "${c_ok}========================================${c_end}"
  echo " 程序目录   : $PROG_DIR$( [[ $IN_PLACE -eq 1 ]] && echo "（原地模式，升级：git pull && systemctl restart $UNIT_NAME）" )"
  echo " 数据目录   : $BASE_DIR  （母盘放 $BASE_DIR/images/）"
  [[ -d "$INSTALL_DIR.bak" ]] && echo " 旧版本备份 : $INSTALL_DIR.bak（回滚方法见 install.sh --help）"
  echo " 服务名     : $UNIT_NAME（开机自启）"
  echo " 常用命令   : systemctl status|restart|stop $UNIT_NAME"
  echo "              journalctl -u $UNIT_NAME -f"
  echo
  echo " Web 后台   : http://$ip:8080/     （管理员 admin / 默认密码 admin123，务必马上改）"
  echo " iPXE 供给  : http://$ip:5000/boot.ipxe"
  echo " iSCSI      : $ip:3260"
  echo
  echo " 还需要手动做的："
  echo "   1) 把母盘 .raw 放进 $BASE_DIR/images/（文件名如 win11.raw）"
  echo "   2) 配 dnsmasq 的 DHCP/TFTP，把 iPXE 引导器推给客户机（README 第五节 4）"
  echo "   3) 客户机母盘里装客户端 agent，实现远程关机/VNC/网盘挂载（README 第五节 7、client/README.md）"
  echo
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$UNIT_NAME"; then
    echo "${c_ok}服务正在运行${c_end}"
  else
    echo "${c_warn}服务当前没在运行：journalctl -u $UNIT_NAME -n 50 --no-pager${c_end}"
  fi
}

# ---------------- 主流程 ----------------
detect_distro
if [[ $DO_UNINSTALL -eq 1 ]]; then
  do_uninstall
fi
echo "${c_info}== iSCSI Broker 安装/升级 ==${c_end}  源码目录：$SRC_DIR"
PROG_DIR="$INSTALL_DIR"
install_deps
check_python
detect_legacy
resolve_base_dir
warn_dirty_source
check_in_place_safety
stop_old_instances
backup_prev
install_files
prepare_data_dir
write_env_file
write_unit
setup_tftp
enable_service
print_summary
