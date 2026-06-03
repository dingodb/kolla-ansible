#!/usr/bin/env python3
# roce-tc-watchdog-dpu.py
# 每 30 秒被 systemd timer 调用，扫描 RoCE OVS bridge 上所有有 VLAN tag 的 port，
# 缺少 OVS ACL 规则时从 sidecar 查 subnet 后补装。
#
# DPU 跑 OVS-DPDK，数据面走用户态 PMD，完全绕开 Linux 内核网络栈，
# 内核 TC flower 规则对其无效。改用 OVS OpenFlow 规则实现 ACL：
# OVS-DPDK 的控制面仍是 OpenFlow，PMD 会强制执行 ovs-ofctl 装入的流表。
#
# OVS-based 发现：不依赖易失的内存状态，sidecar 重启不影响正确性。

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

SIDECAR     = os.environ.get('ROCE_SIDECAR_URL', 'http://127.0.0.1:9967')
ROCE_BRIDGE = os.environ.get('ROCE_OVS_BRIDGE',  'br-ex')
SKIP_PORTS  = {'roce-dhcp'}   # OVS internal 口，跳过
LOG_FILE    = '/var/log/roce-dhcp/roce-sidecar.log'

# OVS flow 优先级（越高越先匹配，高于 br-ex 默认 normal=0）
PRIO_DHCP        = 210   # 放行 DHCP request
PRIO_SUBNET      = 205   # 放行同子网流量
PRIO_EXTRA_ROUTE = 204   # 放行 extra_routes
PRIO_DROP        = 200   # 拦截其他所有 IP 流量（ACL 存在标志）


def _log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [watchdog-dpu] {msg}\n'
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line)
    except Exception:
        pass
    print(line, end='', file=sys.stderr)


def _run(*args):
    return subprocess.run(list(args), capture_output=True, text=True)


def _ovs_list_ports(bridge):
    r = _run('ovs-vsctl', 'list-ports', bridge)
    return [p.strip() for p in r.stdout.splitlines() if p.strip()]


def _ovs_get_vlan(port):
    r = _run('ovs-vsctl', 'get', 'port', port, 'tag')
    val = r.stdout.strip()
    if val and val != '[]':
        try:
            return int(val)
        except ValueError:
            pass
    return None


def _has_acl(port):
    """检查该 port 是否已有 OVS drop 流（ACL 存在的标志）。"""
    r = _run('ovs-ofctl', 'dump-flows', ROCE_BRIDGE, f'in_port={port},ip')
    return any('actions=drop' in line for line in r.stdout.splitlines())


def _get_valid_openflow_ports(bridge):
    """返回 bridge 上当前有效的 OpenFlow 端口号集合（不含 LOCAL=65534）。"""
    r = _run('ovs-ofctl', 'show', bridge)
    ports = set()
    for line in r.stdout.splitlines():
        m = re.match(r'\s+(\d+)\(', line)
        if m:
            ports.add(int(m.group(1)))
    return ports


def _cleanup_stale_acl_flows(bridge, valid_ports):
    """删除 bridge 上 in_port 已不存在的孤儿 ACL 流规则。

    OVS-DOCA datapath 不会在端口删除时自动回收 OpenFlow 流，
    VM 销毁后 SF representor 端口消失但规则残留，需主动清理。
    """
    r = _run('ovs-ofctl', 'dump-flows', bridge)
    stale = set()
    for line in r.stdout.splitlines():
        m = re.search(r'priority=(?:200|204|205|210)[^,]*,.*?in_port=(\d+)', line)
        if m:
            port = int(m.group(1))
            if port not in valid_ports:
                stale.add(port)
    for port in stale:
        _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},ip')
        _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},udp')
        _log(f'removed stale acl for deleted port={port}')


def _install_ovs_acl(port, subnet, extra_routes):
    """为 RoCE SF representor 安装 OVS OpenFlow ACL 规则。"""
    bridge = ROCE_BRIDGE

    # 先清除该 port 已有的 IP/UDP ACL 流（幂等重装）
    _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},ip')
    _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},udp')

    # 放行 DHCP（UDP dst 67）
    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_DHCP},in_port={port},udp,tp_dst=67,actions=normal')

    # 放行同租户子网
    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_SUBNET},in_port={port},ip,nw_dst={subnet},actions=normal')

    # 放行 extra_routes（逗号分隔 CIDR）
    for cidr in (extra_routes or '').split(','):
        cidr = cidr.strip()
        if cidr:
            _run('ovs-ofctl', 'add-flow', bridge,
                 f'priority={PRIO_EXTRA_ROUTE},in_port={port},ip,'
                 f'nw_dst={cidr},actions=normal')

    # 拦截其他所有 IP（最低优先级，作为 ACL 存在的标志）
    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_DROP},in_port={port},ip,actions=drop')


def _sidecar_by_vlan(vlan_id):
    """GET /roce/by_vlan/<vlan> → {subnet, extra_routes} 或 None（非 RoCE VLAN）。"""
    try:
        resp = urllib.request.urlopen(
            f'{SIDECAR}/roce/by_vlan/{vlan_id}', timeout=3)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        _log(f'sidecar by_vlan/{vlan_id} HTTP {e.code}')
        return None
    except Exception as e:
        _log(f'sidecar 不可达: {e}')
        return None


def main():
    valid_ports = _get_valid_openflow_ports(ROCE_BRIDGE)

    # 先清理已删除端口的孤儿流规则（OVS-DOCA 不自动回收）
    _cleanup_stale_acl_flows(ROCE_BRIDGE, valid_ports)

    ports = _ovs_list_ports(ROCE_BRIDGE)
    for port in ports:
        if port in SKIP_PORTS:
            continue

        vlan = _ovs_get_vlan(port)
        if vlan is None:
            continue   # 没有 VLAN tag → 不是租户口

        if _has_acl(port):
            continue   # 规则已在，跳过

        info = _sidecar_by_vlan(vlan)
        if info is None:
            continue   # 非 RoCE VLAN

        subnet       = info.get('subnet', '')
        extra_routes = info.get('extra_routes', '')
        if not subnet:
            continue

        _install_ovs_acl(port, subnet, extra_routes)
        _log(f'installed ovs-acl rep={port} vlan={vlan} subnet={subnet}')


if __name__ == '__main__':
    main()
