#!/usr/bin/env python3
# roce-tc-watchdog.py
# 每 30 秒被 systemd timer 调用，扫描 RoCE OVS bridge 上所有有 VLAN tag 的 port，
# 缺少 OVS ACL 规则时从 sidecar 查 subnet 后补装。
#
# 计算节点 OVS 使用 kernel datapath，与 DPU 的 DOCA datapath 行为一致：
# 端口删除时均不会自动回收 OpenFlow 流，需主动清理孤儿规则。

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
LOG_FILE    = '/var/log/kolla/nova/roce-hook.log'

PRIO_DHCP        = 210
PRIO_SUBNET      = 205
PRIO_EXTRA_ROUTE = 204
PRIO_DROP        = 200


def _log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{ts} [watchdog] {msg}\n'
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
    r = _run('ovs-ofctl', 'dump-flows', ROCE_BRIDGE, f'in_port={port},ip')
    return any('actions=drop' in line for line in r.stdout.splitlines())


def _get_valid_openflow_ports(bridge):
    r = _run('ovs-ofctl', 'show', bridge)
    ports = set()
    for line in r.stdout.splitlines():
        m = re.match(r'\s+(\d+)\(', line)
        if m:
            ports.add(int(m.group(1)))
    return ports


def _cleanup_stale_acl_flows(bridge, valid_ports):
    """删除 bridge 上 in_port 已不存在的孤儿 ACL 流规则。"""
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
    bridge = ROCE_BRIDGE

    _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},ip')
    _run('ovs-ofctl', 'del-flows', bridge, f'in_port={port},udp')

    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_DHCP},in_port={port},udp,tp_dst=67,actions=normal')

    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_SUBNET},in_port={port},ip,nw_dst={subnet},actions=normal')

    for cidr in (extra_routes or '').split(','):
        cidr = cidr.strip()
        if cidr:
            _run('ovs-ofctl', 'add-flow', bridge,
                 f'priority={PRIO_EXTRA_ROUTE},in_port={port},ip,'
                 f'nw_dst={cidr},actions=normal')

    _run('ovs-ofctl', 'add-flow', bridge,
         f'priority={PRIO_DROP},in_port={port},ip,actions=drop')


def _sidecar_by_vlan(vlan_id):
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

    _cleanup_stale_acl_flows(ROCE_BRIDGE, valid_ports)

    ports = _ovs_list_ports(ROCE_BRIDGE)
    for port in ports:
        if port in SKIP_PORTS:
            continue

        vlan = _ovs_get_vlan(port)
        if vlan is None:
            continue

        if _has_acl(port):
            continue

        info = _sidecar_by_vlan(vlan)
        if info is None:
            continue

        subnet       = info.get('subnet', '')
        extra_routes = info.get('extra_routes', '')
        if not subnet:
            continue

        _install_ovs_acl(port, subnet, extra_routes)
        _log(f'installed ovs-acl rep={port} vlan={vlan} subnet={subnet}')


if __name__ == '__main__':
    main()
