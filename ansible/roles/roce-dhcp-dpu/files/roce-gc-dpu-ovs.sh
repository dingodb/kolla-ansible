#!/bin/bash
# roce-gc-dpu-ovs.sh — 清理 DPU br-ex 上指向已消失设备的残留 RoCE SF 端口
#
# 判据：port 的 interface 报 "No such device"，说明后端 SF netdev 已不存在
#       （裸金属实例掉电时 SF 在硬件层被回收，但 OVSDB 里的 port 条目残留）。
#       这种 port 不可能属于活实例——活的 SF representor 后端设备一定在。
#
# 纯 ovs-vsctl，无 DB / 无 mlxdevm / 无 pymysql，零外部依赖。
# 对应的孤儿 OpenFlow ACL 流由现有 roce-tc-watchdog-dpu 自动回收，此处不处理。
# roce-dhcp 内部监听口不匹配命名规则，永不触碰。
#
# 用法：roce-gc-dpu-ovs.sh            执行清理
#       roce-gc-dpu-ovs.sh --dry-run  只列出会删除的端口
set -euo pipefail

BRIDGE="${ROCE_OVS_BRIDGE:-br-ex}"
# RoCE SF representor 命名（不同 NIC 前缀可能不同，用 ROCE_REP_RE 覆盖）
REP_RE="${ROCE_REP_RE:-^en[0-9]f[0-9]c[0-9]pf[0-9]sf[0-9]+$}"
RECHECK_WAIT="${RECHECK_WAIT:-20}"   # 越过 SF 创建窗口(~15s)，排除创建竞态
# DPU 节点重启后 OVSDB 持久化保留 SF port 条目，但 SF netdev 尚未重建，
# 全部报 "No such device"。nova_compute 重启完成并重建 SF 通常需 60~90s，
# 必须等它稳定运行足够长时间后才允许清理，否则会误删活实例的 br-ex 口。
MIN_NOVA_UPTIME="${MIN_NOVA_UPTIME:-120}"
DRY="${1:-}"

log() { logger -t roce-gc-dpu "$*" 2>/dev/null || true; echo "$(date '+%F %T') $*"; }

# 检查 nova_compute 是否运行且已稳定（防 DPU 重启后批量误删）
# 只在真正删除前调用，dry-run 不受影响。
check_nova_ready() {
    local running started_at start_epoch now_epoch uptime
    running=$(docker inspect nova_compute --format '{{.State.Running}}' 2>/dev/null) || {
        log "skip: cannot inspect nova_compute"
        exit 0
    }
    if [[ "$running" != "true" ]]; then
        log "skip: nova_compute not running"
        exit 0
    fi
    started_at=$(docker inspect nova_compute --format '{{.State.StartedAt}}' 2>/dev/null)
    start_epoch=$(date -d "$started_at" +%s 2>/dev/null) || {
        log "skip: cannot parse nova_compute StartedAt='$started_at'"
        exit 0
    }
    now_epoch=$(date +%s)
    uptime=$(( now_epoch - start_epoch ))
    if [[ "$uptime" -lt "$MIN_NOVA_UPTIME" ]]; then
        log "skip: nova_compute started ${uptime}s ago (< ${MIN_NOVA_UPTIME}s), SF netdev may not be ready yet"
        exit 0
    fi
}

collect() {
    local port err out=()
    for port in $(ovs-vsctl list-ports "$BRIDGE" 2>/dev/null); do
        [[ "$port" =~ $REP_RE ]] || continue
        err=$(ovs-vsctl get interface "$port" error 2>/dev/null | tr -d '"')
        [[ "$err" == *"No such device"* ]] && out+=("$port")
    done
    [[ ${#out[@]} -gt 0 ]] && printf '%s\n' "${out[@]}"
}

mapfile -t first < <(collect)
[[ ${#first[@]} -eq 0 ]] && exit 0

if [[ "$DRY" == "--dry-run" ]]; then
    for p in "${first[@]}"; do log "DRY-RUN would del-port $p ($BRIDGE, No such device)"; done
    exit 0
fi

# 真正删除前：确认 nova_compute 已稳定运行，排除 DPU 重启后 SF 重建中的误判
check_nova_ready

# 二次确认：正常 SF 创建 ~15s 内 netdev 会出现，持续报错才是真残留
sleep "$RECHECK_WAIT"
mapfile -t second < <(collect)

for port in "${second[@]}"; do
    ovs-vsctl --if-exists del-port "$BRIDGE" "$port" \
        && log "removed stale RoCE SF port $port on $BRIDGE"
done
