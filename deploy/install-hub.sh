#!/usr/bin/env bash
set -euo pipefail
caller_directory=$PWD
cd "$(dirname "$0")/.."
root=$(pwd -P)
host='' port='' username='' password_file='' noninteractive=0
enable_panel_update=0
usage() {
  cat <<'HELP'
CodePier · 码头 — 面板安装与更新
  bash install.sh
  bash install.sh --host 192.0.2.20 --port 8765 --username admin --password-file /安全路径/admin-password --non-interactive
选项：--host 主机名/IP  --port 端口  --username 管理员  --password-file 私密密码文件
      --enable-panel-update 安装独立宿主机更新服务（Linux/systemd，需 root）
      --non-interactive 无人值守；首次安装必须提供 host 和密码文件或 CODEPIER_ADMIN_PASSWORD
需要 Docker Engine、Compose 插件和主机 Python 3.9+（只使用标准库）。
只支持当前工作空间架构；旧数据库、账号、Token、服务名与数据卷不迁移。
旧数据或不完整资料会明确拒绝，不会清空或重新初始化来冒充升级成功。
使用独立空数据卷建立新安装；当前安装自身的更新保留显式 COMPOSE_FILE 与证书卷。
HELP
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host|--port|--username|--password-file)
      [[ $# -ge 2 && -n "$2" ]] || { echo '选项缺少参数' >&2; exit 2; }
      case "$1" in --host) host=$2;; --port) port=$2;; --username) username=$2;; --password-file) password_file=$2;; esac
      shift 2;;
    --non-interactive) noninteractive=1; shift;;
    --enable-panel-update) enable_panel_update=1; shift;;
    --help|-h) usage; exit 0;;
    *) echo "未知选项：$1" >&2; usage >&2; exit 2;;
  esac
done
[[ ! -L .env ]] || { echo '.env 不能是符号链接；未修改任何配置。' >&2; exit 1; }
# Only the current explicit bootstrap credential is accepted.
export CODEPIER_ADMIN_PASSWORD="${CODEPIER_ADMIN_PASSWORD:-}"
if [[ -n "$password_file" ]]; then
  [[ "$password_file" == /* ]] || password_file="$caller_directory/$password_file"
  [[ -f "$password_file" && ! -L "$password_file" && -r "$password_file" ]] || { echo '密码文件不存在、不可读或是符号链接' >&2; exit 1; }
  CODEPIER_ADMIN_PASSWORD="$(cat -- "$password_file")"
fi
bootstrap_python=${CODEPIER_BOOTSTRAP_PYTHON:-python3}
command -v "$bootstrap_python" >/dev/null || { echo '请先安装主机 Python 3.9+，或设置 CODEPIER_BOOTSTRAP_PYTHON。' >&2; exit 1; }
"$bootstrap_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' || { echo '主机 Python 版本需要 3.9+' >&2; exit 1; }
command -v docker >/dev/null || { echo '请先安装 Docker Engine 与 Compose 插件。' >&2; exit 1; }
export COMPOSE_PROJECT_NAME=codepier
docker compose version >/dev/null
lock="$root/.codepier-install.lock"
mkdir "$lock" 2>/dev/null || { echo '另一个安装进程或未完成安装锁存在；请先检查 .codepier-install.lock。' >&2; exit 1; }
printf '%s\n' "$$" > "$lock/pid"
env_tmp=''
cleanup() {
  status=$?
  trap - EXIT
  [[ -z "$env_tmp" ]] || rm -f -- "$env_tmp"
  unset CODEPIER_ADMIN_PASSWORD
  if [[ -f "$lock/pid" && "$(cat "$lock/pid")" == "$$" ]]; then rm -f -- "$lock/pid"; rmdir "$lock" 2>/dev/null || true; fi
  exit "$status"
}
trap cleanup EXIT
if [[ ! -f .env ]]; then
  if [[ -z "$host" ]]; then
    ((noninteractive == 0)) || { echo '首次无人值守安装需要 --host' >&2; exit 1; }
    read -r -p '云端服务器公网 IP 或主机名（IPv6 请使用方括号）：' host
  fi
  if [[ -z "$port" && "$noninteractive" == 0 ]]; then read -r -p '面板端口 [8765]：' port; fi
  port=${port:-8765}
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || { echo '端口无效' >&2; exit 1; }
  port=$((10#$port))
  ((port >= 1 && port <= 65535)) || { echo '端口无效' >&2; exit 1; }
  [[ "$host" =~ ^[a-zA-Z0-9.-]+$ || "$host" =~ ^\[[a-fA-F0-9:]+\]$ ]] || { echo '主机名无效，请不要填写路径或空格' >&2; exit 1; }
  umask 077
  env_tmp=$(mktemp .env.tmp.XXXXXX)
  sed "s/^HUB_PORT=.*/HUB_PORT=$port/;s|^HUB_PUBLIC_URL=.*|HUB_PUBLIC_URL=http://$host:$port|" .env.example > "$env_tmp"
  mv "$env_tmp" .env
  env_tmp=''
fi
[[ ! -L "$root/.codepier-updater" && ! -L "$root/.codepier-updater/run" ]] || { echo "更新服务目录不能是符号链接" >&2; exit 1; }
mkdir -p "$root/.codepier-updater/run"
# The Hub must be able to check an absent marker even before the updater is enabled.
chmod 0755 "$root/.codepier-updater/run"
if ((enable_panel_update == 1)); then
  [[ "$(uname -s)" == Linux && "$EUID" == 0 && -d /run/systemd/system ]] || { echo "启用面板更新需 Linux/systemd 宿主机 root 权限" >&2; exit 1; }
  "$bootstrap_python" - "$root" <<'PYHOST'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from panel_updater import secure_host_path
secure_host_path(Path(sys.argv[1]))
secure_host_path(Path(sys.executable))
PYHOST
fi
docker compose config --quiet
# Build the selected current source, but do not stop or adopt another install.
docker compose build hub
# Resolve actual current volumes through the full Compose selection. The helper
# never searches old project names, copies databases, moves checkouts or rewrites
# proxy settings. Existing certificate/data volumes are not modified here.
hub_data_volume=$("$bootstrap_python" scripts/bootstrap_hub.py --root "$root")
needs_init=0
if docker compose run --rm --no-deps --volume "$hub_data_volume:/app/data:ro" hub python -m hub.installation; then
  echo '只读校验已确认当前版本数据库；保留该安装的账号与资料。'
else
  probe_status=$?
  if ((probe_status != 3)); then echo '无法检查管理员或数据卷；没有尝试重新初始化。' >&2; exit "$probe_status"; fi
  needs_init=1
  if [[ -z "$username" && "$noninteractive" == 0 ]]; then read -r -p '管理员用户名 [admin]：' username; fi
  if [[ "$noninteractive" == 1 && -z "${CODEPIER_ADMIN_PASSWORD:-}" ]]; then
    echo '首次无人值守初始化需要 --password-file 或 CODEPIER_ADMIN_PASSWORD；未创建账号。' >&2; exit 1
  fi
fi
# A successful read-only probe is the only path to new owner initialization.
# Never retry a failed probe as initialization, or silently reset an old store.
if ((needs_init == 1)); then
  if [[ -n "${CODEPIER_ADMIN_PASSWORD:-}" ]]; then
    docker compose run --rm --no-deps -e CODEPIER_ADMIN_PASSWORD hub python -m hub init --username "${username:-admin}"
  else
    docker compose run --rm --no-deps hub python -m hub init --username "${username:-admin}"
  fi
  unset CODEPIER_ADMIN_PASSWORD
fi
# Preserve the caller's full Compose selection, including a configured HTTPS proxy.
docker compose up -d --wait --wait-timeout 90
echo 'CodePier 已启动且健康检查通过。查看日志：docker compose logs -f hub'
if ((enable_panel_update == 1)); then
  "$bootstrap_python" "$root/scripts/panel_updater.py" install --root "$root"
fi
echo '请确认云端及系统防火墙放行所选端口；家里无需开放入站端口。'
echo 'HTTP 不保护浏览器密码与配对文件；首次初始化请使用可信网络或 SSH 转发。'
