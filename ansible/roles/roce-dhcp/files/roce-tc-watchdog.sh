#!/bin/bash
# roce-tc-watchdog.sh
# 每 30 秒被 systemd timer 调用，扫描所有运行中 VM 的 eSwitch representor，
# 缺少 TC ACL 规则时从 sidecar 查信息后补装。与 OVS 无时序冲突。

SIDECAR="http://127.0.0.1:9967"
LOG="/var/log/kolla/nova/roce-hook.log"

_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" >> "$LOG"; }

_install_tc_acl() {
    local rep=$1 subnet=$2 extra_routes=$3
    # 不删整个 qdisc，避免破坏 OVN 在同一 qdisc 上安装的 LLDP 规则（pref 5）
    # OVN 检测到自身规则消失后会重建 qdisc，会把我们的规则一起覆盖掉
    tc qdisc add dev "$rep" handle ffff: ingress 2>/dev/null || true
    # 每条规则先 del 再 add，实现幂等更新而不影响其他 pref
    tc filter del dev "$rep" parent ffff: pref 10  2>/dev/null || true
    tc filter add dev "$rep" parent ffff: protocol ip pref 10 \
        flower ip_proto udp dst_port 67 action pass 2>/dev/null || true
    tc filter del dev "$rep" parent ffff: pref 50  2>/dev/null || true
    tc filter add dev "$rep" parent ffff: protocol ip pref 50 \
        flower dst_ip "$subnet" action pass 2>/dev/null || true
    tc filter del dev "$rep" parent ffff: pref 60  2>/dev/null || true
    local _cidr
    IFS=',' read -ra _ROUTES <<< "$extra_routes"
    for _cidr in "${_ROUTES[@]}"; do
        _cidr="${_cidr//[[:space:]]/}"
        [[ -n "$_cidr" ]] && tc filter add dev "$rep" parent ffff: protocol ip pref 60 \
            flower dst_ip "$_cidr" action pass 2>/dev/null || true
    done
    tc filter del dev "$rep" parent ffff: pref 200 2>/dev/null || true
    tc filter add dev "$rep" parent ffff: protocol ip pref 200 \
        flower action drop 2>/dev/null || true
}

_find_representor() {
    local vf_addr=$1
    local pf_sysfs pf_net switch_id pf_port_name pf_idx vf_num link target port_name
    _REP_NETDEV=""

    pf_sysfs=$(readlink -f "/sys/bus/pci/devices/$vf_addr/physfn" 2>/dev/null)
    [[ -z "$pf_sysfs" ]] && return 1

    pf_net=$(ls "${pf_sysfs}/net/" 2>/dev/null | head -1)
    [[ -z "$pf_net" ]] && return 1

    switch_id=$(cat "/sys/class/net/${pf_net}/phys_switch_id" 2>/dev/null)
    [[ -z "$switch_id" ]] && return 1

    pf_port_name=$(cat "/sys/class/net/${pf_net}/phys_port_name" 2>/dev/null)
    pf_idx=$(echo "$pf_port_name" | grep -oP '\d+' | head -1)
    pf_idx=${pf_idx:-0}

    vf_num=""
    for link in "${pf_sysfs}"/virtfn*; do
        target=$(readlink -f "$link" 2>/dev/null)
        if [[ "$target" == *"$vf_addr"* ]]; then
            vf_num=${link##*virtfn}
            break
        fi
    done
    [[ -z "$vf_num" ]] && return 1

    port_name="pf${pf_idx}vf${vf_num}"
    local netdev_path netdev dev_switch dev_port
    for netdev_path in /sys/class/net/*; do
        netdev=$(basename "$netdev_path")
        dev_switch=$(cat "${netdev_path}/phys_switch_id" 2>/dev/null)
        dev_port=$(cat "${netdev_path}/phys_port_name" 2>/dev/null)
        if [[ "$dev_switch" == "$switch_id" && "$dev_port" == "$port_name" ]]; then
            _REP_NETDEV=$netdev
            return 0
        fi
    done
    return 1
}

# 扫描 nova_libvirt 容器内的运行中域 XML（bind-mount 到宿主机同路径）
shopt -s nullglob
dom_xml_files=(/var/run/libvirt/qemu/instance-*.xml)
[[ ${#dom_xml_files[@]} -eq 0 ]] && exit 0

for dom_xml_file in "${dom_xml_files[@]}"; do
    [[ -f "$dom_xml_file" ]] || continue
    dom_xml=$(<"$dom_xml_file")

    uuid=$(echo "$dom_xml" | grep -oP '(?<=<uuid>)[0-9a-f-]+' | head -1)
    [[ -z "$uuid" ]] && continue

    bus=$(echo "$dom_xml"  | grep -A5 "hostdev.*type='pci'" | grep -oP "bus='0x\K[0-9a-f]+"      | head -1)
    [[ -z "$bus" ]] && continue
    slot=$(echo "$dom_xml" | grep -A5 "hostdev.*type='pci'" | grep -oP "slot='0x\K[0-9a-f]+"     | head -1)
    func=$(echo "$dom_xml" | grep -A5 "hostdev.*type='pci'" | grep -oP "function='0x\K[0-9a-f]+" | head -1)
    vf_addr="0000:${bus}:${slot}.${func}"

    _find_representor "$vf_addr" || continue
    rep=$_REP_NETDEV

    # 有规则则跳过（只认我们的 pref 10；pref 5 是 OVN LLDP 规则，不算已装）
    tc filter show dev "$rep" parent ffff: 2>/dev/null | grep -q "pref 10" && continue

    # 查 sidecar
    resp=$(HOME=/nonexistent curl -s --max-time 3 "$SIDECAR/roce/instance/$uuid")
    subnet=$(echo "$resp" | grep -oP '"subnet":"\K[^"]+')
    [[ -z "$subnet" ]] && continue
    extra=$(echo "$resp" | grep -oP '"extra_routes":"\K[^"]+')

    _install_tc_acl "$rep" "$subnet" "$extra"
    _log "installed rep=$rep subnet=$subnet uuid=$uuid"
done
