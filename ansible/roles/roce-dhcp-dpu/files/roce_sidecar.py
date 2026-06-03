#!/usr/bin/env python3
"""
roce_sidecar.py  —  RoCE DHCP 注册服务（compute / dpu 双模式）

职责：
  1. HTTP API（:9967）：libvirt hook / nova-compute 调用，注册/注销 instance→(mac, ip, vlan_id)
  2. DHCP 服务（AF_PACKET on ROCE_PF）：响应 VM 发出的 DHCP Discover/Request
  3. DB 客户端：读写 nova.dpu_roce_* 表，查询 nova_api.flavor_extra_specs
  4. 租户隔离：每个租户独占一个 VLAN，首次创建时从池中认领，最后一个实例删除时归还

模式差异（NODE_TYPE）：
  compute：PF 收到 802.1Q tagged 帧，DHCP 回复带 VLAN tag
  dpu    ：监听 OVS internal port，收到无 tag 裸帧，DHCP 回复也无 tag

依赖：pip install flask pymysql
"""

import hashlib
import ipaddress
import logging
import os
import re
import socket
import struct
import subprocess
import threading
import time

import pymysql
from flask import Flask, jsonify, request

# ── 配置 ──────────────────────────────────────────────────────────────────────
DB_HOST          = os.getenv('NOVA_DB_HOST',      '10.220.68.247')
DB_PORT          = int(os.getenv('NOVA_DB_PORT',  '3306'))
NOVA_DB          = os.getenv('NOVA_DB_NAME',      'nova')
DB_USER          = os.getenv('NOVA_DB_USER',      'nova')
DB_PASS          = os.getenv('NOVA_DB_PASS',      '')
NOVA_API_DB      = os.getenv('NOVA_API_DB',       'nova_api')
NOVA_API_USER    = os.getenv('NOVA_API_DB_USER',  'nova_api')
NOVA_API_PASS    = os.getenv('NOVA_API_DB_PASS',  '')
NODE_HOST        = os.getenv('NODE_HOST',          '127.0.0.1')
NODE_IP          = os.getenv('NODE_IP',            NODE_HOST)
NODE_TYPE        = os.getenv('NODE_TYPE',          'compute')   # compute | dpu
ROCE_PF          = os.getenv('ROCE_PF',            'roce-dhcp')
ROCE_PF_LIST     = [pf.strip() for pf in ROCE_PF.split(',') if pf.strip()]
HTTP_PORT        = int(os.getenv('HTTP_PORT',      '9967'))
DEFAULT_VLAN     = int(os.getenv('DEFAULT_VLAN',   '4000'))
EXTRA_SPEC_KEY   = os.getenv('ROCE_EXTRA_SPEC',    'hw:roce_enabled')
ROCE_MTU         = int(os.getenv('ROCE_MTU',        '0'))    # 0 = 不设置
ROCE_EXTRA_ROUTES = os.getenv('ROCE_EXTRA_ROUTES',  '')      # 逗号分隔 CIDR，如 10.199.70.0/24

# 内存映射表：(vlan_id, mac_bytes) -> {ip, gw, mask, instance_uuid, project_id}
# dpu 模式：vlan_id 为 None（OVS 已剥 tag，收到的是裸帧）
_lock    = threading.Lock()
_mac_map: dict = {}

# DHCP 包计数器：{(pf, 'offer'|'ack'): int}，用于 /metrics 端点
_dhcp_counters: dict = {}

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── DB 工具 ───────────────────────────────────────────────────────────────────

def _db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, db=NOVA_DB,
        user=DB_USER, passwd=DB_PASS,
        charset='utf8mb4', autocommit=False, connect_timeout=5,
    )

def _db_api():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, db=NOVA_API_DB,
        user=NOVA_API_USER, passwd=NOVA_API_PASS,
        charset='utf8mb4', autocommit=False, connect_timeout=5,
    )


def gen_roce_mac(instance_uuid: str) -> str:
    h = hashlib.sha256(instance_uuid.encode()).digest()
    b = bytearray(6)
    b[0] = 0x02
    b[1:] = h[1:6]
    return ':'.join(f'{x:02x}' for x in b)


def _roce_enabled_for_flavor(flavor_name: str) -> bool:
    if not flavor_name:
        return False
    try:
        db = _db_api()
        with db.cursor() as c:
            c.execute("""
                SELECT fes.value FROM flavor_extra_specs fes
                JOIN flavors f ON fes.flavor_id = f.id
                WHERE f.name = %s AND fes.key = %s LIMIT 1
            """, (flavor_name, EXTRA_SPEC_KEY))
            row = c.fetchone()
        db.close()
        return row is not None and str(row[0]).lower() in ('true', '1', 'yes')
    except Exception:
        log.exception('查询 flavor extra_spec 失败，flavor=%s', flavor_name)
        return False


def _alloc_for_project(c, project_id: str, instance_uuid: str):
    """
    租户感知的 VLAN + IP 分配（在调用方事务内执行）：
      1. 若该租户已认领 VLAN → 直接在该 VLAN 子网内分配 IP
      2. 若尚未认领 → SELECT FOR UPDATE 抢一个空闲 VLAN，写入 project_id
      3. 在确定的 VLAN 内用 SHA256 偏移优先分配 IP，冲突时顺序扫描

    返回 (vlan_id, roce_ip, net_gw, net_mask)
    """
    # Step 1：查租户已有的 VLAN
    c.execute("""
        SELECT vlan_id, net_prefix, net_mask, net_gw, mtu, extra_routes
        FROM dpu_roce_vlan_pool WHERE project_id = %s LIMIT 1
    """, (project_id,))
    row = c.fetchone()

    if row is None:
        # Step 2：认领空闲 VLAN（FOR UPDATE 防止并发两个请求抢同一行）
        c.execute("""
            SELECT vlan_id, net_prefix, net_mask, net_gw, mtu, extra_routes
            FROM dpu_roce_vlan_pool WHERE project_id IS NULL LIMIT 1 FOR UPDATE
        """)
        row = c.fetchone()
        if row is None:
            raise RuntimeError('VLAN 池已耗尽，无空闲 VLAN 可分配给新租户')
        c.execute(
            "UPDATE dpu_roce_vlan_pool SET project_id = %s WHERE vlan_id = %s",
            (project_id, row[0])
        )
        log.info('租户 %s 认领 VLAN %d', project_id, row[0])

    vlan_id, net_prefix, mask, gw, mtu, extra_routes = row

    # Step 3：在该 VLAN 子网内分配 IP
    c.execute(
        "SELECT roce_ip FROM dpu_roce_instance_allocations WHERE vlan_id = %s",
        (vlan_id,)
    )
    used = {r[0] for r in c.fetchall()}

    ip = _pick_ip(net_prefix, mask, gw, used, instance_uuid)
    return vlan_id, ip, gw, mask, mtu or 0, extra_routes or '', net_prefix


def _alloc_ip_by_vlan(c, vlan_id: int, instance_uuid: str):
    """无 project_id 时的兼容分配（直接按 vlan_id 分配，不做租户管理）"""
    c.execute("""
        SELECT net_prefix, net_mask, net_gw, mtu, extra_routes
        FROM dpu_roce_vlan_pool WHERE vlan_id = %s FOR UPDATE
    """, (vlan_id,))
    pool = c.fetchone()
    if not pool:
        raise RuntimeError(f'VLAN {vlan_id} 不在池中')
    net_prefix, mask, gw, mtu, extra_routes = pool

    c.execute(
        "SELECT roce_ip FROM dpu_roce_instance_allocations WHERE vlan_id = %s",
        (vlan_id,)
    )
    used = {r[0] for r in c.fetchall()}

    ip = _pick_ip(net_prefix, mask, gw, used, instance_uuid)
    return ip, gw, mask, mtu or 0, extra_routes or '', net_prefix


def _add_to_mac_map(vlan_id, roce_mac: str, ip: str, gw: str, mask: str,
                    instance_uuid: str, project_id: str = '',
                    mtu: int = 0, extra_routes: str = ''):
    mac_bytes = bytes(int(x, 16) for x in roce_mac.split(':'))
    entry = {'ip': ip, 'gw': gw, 'mask': mask,
             'instance_uuid': instance_uuid, 'project_id': project_id,
             'mtu': mtu or ROCE_MTU,
             'extra_routes': extra_routes or ROCE_EXTRA_ROUTES}
    with _lock:
        _mac_map[(vlan_id, mac_bytes)] = entry
        # dpu 模式收到的帧无 VLAN tag（vlan_id=None），额外存一条 MAC-only 索引
        if NODE_TYPE == 'dpu':
            _mac_map[(None, mac_bytes)] = entry


def _load_state_from_db():
    """从 DB 恢复 _mac_map，失败时指数退避重试直到成功。在后台线程调用。"""
    delay = 5
    attempt = 0
    while True:
        try:
            db = _db()
            with db.cursor() as c:
                c.execute("""
                    SELECT a.instance_uuid, a.roce_mac, a.roce_ip, a.vlan_id,
                           p.net_gw, p.net_mask, a.project_id,
                           p.mtu, p.extra_routes
                    FROM dpu_roce_instance_allocations a
                    JOIN dpu_roce_vlan_pool p ON a.vlan_id = p.vlan_id
                    WHERE a.node_host = %s
                """, (NODE_HOST,))
                rows = c.fetchall()
            db.close()
            for uuid, mac, ip, vlan_id, gw, mask, proj, mtu, extra_routes in rows:
                _add_to_mac_map(vlan_id, mac, ip, gw, mask, uuid, proj or '',
                                mtu or 0, extra_routes or '')
            log.info('DB 状态恢复：%d 条映射', len(rows))
            return
        except Exception:
            attempt += 1
            log.warning('DB 状态恢复失败（第 %d 次），%ds 后重试', attempt, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)  # 5→10→20→40→60→60...

def _mask_to_prefix(mask_str: str) -> int:
    return bin(int.from_bytes(
        bytes(int(x) for x in mask_str.split('.')), 'big'
    )).count('1')


def _pick_ip(net_prefix: str, mask: str, gw: str, used: set, instance_uuid: str) -> str:
    """从子网中分配一个未使用的 IP，SHA256 偏移优先，冲突时顺序扫描。"""
    network = ipaddress.IPv4Network(f'{net_prefix}/{mask}', strict=False)
    gw_addr = ipaddress.IPv4Address(gw)
    candidates = [str(h) for h in network.hosts() if h != gw_addr]
    if not candidates:
        raise RuntimeError(f'子网 {net_prefix}/{mask} 无可用主机地址')
    h = hashlib.sha256(instance_uuid.encode()).digest()
    offset = int.from_bytes(h[4:8], 'big') % len(candidates)
    for i in range(len(candidates)):
        ip = candidates[(offset + i) % len(candidates)]
        if ip not in used:
            return ip
    raise RuntimeError(f'子网 {net_prefix}/{mask} IP 已耗尽')


# ── HTTP API ──────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.route('/roce/register', methods=['POST'])
def register():
    data          = request.get_json(silent=True) or {}
    instance_uuid = data.get('instance_uuid', '').strip()
    flavor_name   = data.get('flavor_name',   '').strip()
    project_id    = data.get('project_id',    '').strip()
    node_host     = data.get('node_host',     NODE_HOST)
    node_type     = data.get('node_type',     NODE_TYPE)
    # vlan_id 仅在无 project_id 时作为兜底（向后兼容）
    fallback_vlan = int(data.get('vlan_id', DEFAULT_VLAN))

    if not instance_uuid:
        return jsonify(error='instance_uuid required'), 400

    # DPU 模式下所有实例都分配 RoCE，无需 flavor extra_spec
    if flavor_name and NODE_TYPE != 'dpu' and not _roce_enabled_for_flavor(flavor_name):
        log.info('skip  uuid=%s  flavor=%s  (no %s)', instance_uuid,
                 flavor_name, EXTRA_SPEC_KEY)
        return jsonify(roce_enabled=False, msg='flavor 未启用 RoCE，跳过分配'), 200

    # 允许调用方传入实际 VF 硬件 MAC（eSwitch 模式：ip link set vf mac 不改 guest 可见 MAC）
    caller_mac = data.get('mac', '').strip().lower()
    roce_mac = caller_mac if caller_mac and len(caller_mac.split(':')) == 6 \
               else gen_roce_mac(instance_uuid)

    try:
        db = _db()
        try:
            with db.cursor() as c:
                # 幂等：已分配则直接返回，同时确保 _mac_map 有此条目
                # 冷迁移：若 node_host 变化，UPDATE DB 使目标节点成为新属主
                c.execute("""
                    SELECT a.roce_mac, a.roce_ip, a.vlan_id, p.net_gw, p.net_mask,
                           a.project_id, p.mtu, p.extra_routes, a.node_host, p.net_prefix
                    FROM dpu_roce_instance_allocations a
                    JOIN dpu_roce_vlan_pool p ON a.vlan_id = p.vlan_id
                    WHERE a.instance_uuid = %s
                """, (instance_uuid,))
                row = c.fetchone()
                if row:
                    ex_mac, ex_ip, ex_vlan, ex_gw, ex_mask, ex_proj, ex_mtu, ex_routes, ex_node_host, ex_prefix = row
                    if ex_node_host != node_host:
                        c.execute(
                            "UPDATE dpu_roce_instance_allocations "
                            "SET node_host=%s, node_type=%s WHERE instance_uuid=%s",
                            (node_host, node_type, instance_uuid)
                        )
                        db.commit()
                        log.info('migrate uuid=%s %s → %s', instance_uuid, ex_node_host, node_host)
                    _add_to_mac_map(ex_vlan, ex_mac, ex_ip, ex_gw, ex_mask,
                                    instance_uuid, ex_proj or '',
                                    ex_mtu or 0, ex_routes or '')
                    ex_pl = _mask_to_prefix(ex_mask)
                    ex_subnet = f'{ex_prefix}/{ex_pl}'
                    return jsonify(roce_enabled=True, mac=ex_mac, ip=ex_ip,
                                   vlan_id=ex_vlan, gw=ex_gw,
                                   subnet=ex_subnet, extra_routes=ex_routes or '')

                # 分配：有 project_id 走租户隔离逻辑，否则走旧逻辑
                if project_id:
                    vlan_id, roce_ip, gw, mask, mtu, extra_routes, net_prefix = _alloc_for_project(
                        c, project_id, instance_uuid)
                else:
                    log.warning('register 未提供 project_id，使用兼容模式 vlan=%d', fallback_vlan)
                    roce_ip, gw, mask, mtu, extra_routes, net_prefix = _alloc_ip_by_vlan(
                        c, fallback_vlan, instance_uuid)
                    vlan_id = fallback_vlan

                c.execute("""
                    INSERT INTO dpu_roce_instance_allocations
                        (instance_uuid, roce_mac, roce_ip, vlan_id,
                         node_host, node_type, project_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (instance_uuid, roce_mac, roce_ip, vlan_id,
                      node_host, node_type, project_id))
                db.commit()

            _pl = _mask_to_prefix(mask)
            subnet = f'{net_prefix}/{_pl}'
            _add_to_mac_map(vlan_id, roce_mac, roce_ip, gw, mask,
                            instance_uuid, project_id, mtu, extra_routes)
            log.info('register OK  uuid=%s  project=%s  mac=%s  ip=%s  vlan=%d',
                     instance_uuid, project_id or '-', roce_mac, roce_ip, vlan_id)
            return jsonify(roce_enabled=True, mac=roce_mac, ip=roce_ip,
                           vlan_id=vlan_id, gw=gw,
                           subnet=subnet, extra_routes=extra_routes)

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except RuntimeError as e:
        return jsonify(error=str(e)), 503
    except Exception as e:
        log.exception('register error')
        return jsonify(error=str(e)), 500


@app.route('/roce/instance/<instance_uuid>', methods=['DELETE'])
def deregister(instance_uuid):
    # 冷迁移：caller_host 为源节点 IP/hostname，若 DB 已属于目标节点则跳过删除
    caller_host = request.args.get('caller_host', NODE_HOST)
    try:
        db = _db()
        try:
            with db.cursor() as c:
                c.execute("""
                    SELECT roce_mac, vlan_id, project_id, node_host
                    FROM dpu_roce_instance_allocations
                    WHERE instance_uuid = %s
                """, (instance_uuid,))
                row = c.fetchone()
                if not row:
                    return jsonify(ok=True, note='not found')
                roce_mac, vlan_id, project_id, db_node_host = row
                if db_node_host != caller_host:
                    log.info('skip deregister uuid=%s: migrated %s → %s',
                             instance_uuid, caller_host, db_node_host)
                    return jsonify(ok=True, skipped=True)

                c.execute(
                    "DELETE FROM dpu_roce_instance_allocations WHERE instance_uuid = %s",
                    (instance_uuid,)
                )
                # 若该租户在此 VLAN 上已无其他实例，归还 VLAN
                if project_id:
                    c.execute("""
                        SELECT COUNT(*) FROM dpu_roce_instance_allocations
                        WHERE vlan_id = %s AND project_id = %s
                    """, (vlan_id, project_id))
                    remaining = c.fetchone()[0]
                    if remaining == 0:
                        c.execute(
                            "UPDATE dpu_roce_vlan_pool SET project_id = NULL WHERE vlan_id = %s",
                            (vlan_id,)
                        )
                        log.info('租户 %s VLAN %d 已归还', project_id, vlan_id)

                db.commit()

            mac_bytes = bytes(int(x, 16) for x in roce_mac.split(':'))
            with _lock:
                _mac_map.pop((vlan_id, mac_bytes), None)
                _mac_map.pop((None,    mac_bytes), None)

            log.info('deregister OK  uuid=%s  project=%s  mac=%s  vlan=%d',
                     instance_uuid, project_id or '-', roce_mac, vlan_id)
            return jsonify(ok=True)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as e:
        log.exception('deregister error')
        return jsonify(error=str(e)), 500


@app.route('/roce/instance/<instance_uuid>', methods=['GET'])
def lookup(instance_uuid):
    try:
        db = _db()
        with db.cursor() as c:
            c.execute("""
                SELECT a.roce_mac, a.roce_ip, a.vlan_id, a.node_host,
                       a.node_type, p.net_gw, a.project_id,
                       p.net_prefix, p.net_mask, p.extra_routes
                FROM dpu_roce_instance_allocations a
                JOIN dpu_roce_vlan_pool p ON a.vlan_id = p.vlan_id
                WHERE a.instance_uuid = %s
            """, (instance_uuid,))
            row = c.fetchone()
        db.close()
        if not row:
            return jsonify(error='not found'), 404
        mac, ip, vlan_id, node_host, node_type, gw, project_id, \
            net_prefix, net_mask, extra_routes = row
        prefix_len = _mask_to_prefix(net_mask) if net_mask else 24
        subnet = f'{net_prefix}/{prefix_len}' if net_prefix else ''
        return jsonify(mac=mac, ip=ip, vlan_id=vlan_id,
                       node_host=node_host, node_type=node_type,
                       gw=gw, project_id=project_id,
                       subnet=subnet,
                       extra_routes=extra_routes or '')
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route('/roce/status', methods=['GET'])
def status():
    try:
        db = _db()
        with db.cursor() as c:
            c.execute(
                "SELECT COUNT(*) FROM dpu_roce_instance_allocations WHERE node_host = %s",
                (NODE_HOST,)
            )
            local = c.fetchone()[0]
            c.execute("""
                SELECT p.vlan_id, p.net_prefix, p.net_mask, p.project_id,
                       (SELECT COUNT(*) FROM dpu_roce_instance_allocations
                        WHERE vlan_id = p.vlan_id) AS used
                FROM dpu_roce_vlan_pool p
            """)
            vlans = [
                {'vlan_id': r[0],
                 'subnet':  f'{r[1]}/{_mask_to_prefix(r[2]) if r[2] else 24}',
                 'project': r[3] or '',
                 'used':    r[4]}
                for r in c.fetchall()
            ]
        db.close()
        with _lock:
            cached = len(_mac_map)
        return jsonify(local_allocations=local, cached_entries=cached,
                       vlans=vlans, node=NODE_HOST, mode=NODE_TYPE, pf=ROCE_PF)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/metrics', methods=['GET'])
def prometheus_metrics():
    """Prometheus text-format metrics endpoint.

    暴露的指标：
      roce_sidecar_mac_map_entries   — 内存 MAC 映射表条目数（含 dpu 模式 None-key 副本）
      roce_sidecar_dhcp_packets_total{pf,type} — DHCP OFFER/ACK 发包计数（重启清零）
      roce_sidecar_allocations_total — 本节点在 DB 中的实例分配数
      roce_vlan_pool_total           — VLAN 池总条目数
      roce_vlan_pool_used            — 已被租户占用的 VLAN 数
      roce_vlan_ip_allocations{vlan_id,project_id} — 各活跃 VLAN 当前分配实例数
    """
    lines = []

    def _g(name, help_text, type_str='gauge'):
        lines.append(f'# HELP {name} {help_text}')
        lines.append(f'# TYPE {name} {type_str}')

    node_labels = f'node="{NODE_HOST}",node_type="{NODE_TYPE}"'

    # ── 内存缓存 ──────────────────────────────────────────────────────────────
    with _lock:
        # dpu 模式每条记录有 (vlan_id, mac) 和 (None, mac) 两个 key，计真实条目数
        if NODE_TYPE == 'dpu':
            real_entries = sum(1 for (v, _) in _mac_map if v is not None)
        else:
            real_entries = len(_mac_map)
        dhcp_snap = dict(_dhcp_counters)

    _g('roce_sidecar_mac_map_entries', 'In-memory MAC map entries (real RoCE allocations)')
    lines.append(f'roce_sidecar_mac_map_entries{{{node_labels}}} {real_entries}')

    # ── DHCP 计数器（重启清零，适合计算速率） ────────────────────────────────
    _g('roce_sidecar_dhcp_packets_total', 'DHCP packets sent since last restart', 'counter')
    for (pf, pkt_type), count in dhcp_snap.items():
        lines.append(
            f'roce_sidecar_dhcp_packets_total{{{node_labels},pf="{pf}",type="{pkt_type}"}} {count}'
        )

    # ── DB 查询 ───────────────────────────────────────────────────────────────
    try:
        db = _db()
        with db.cursor() as c:
            c.execute(
                "SELECT COUNT(*) FROM dpu_roce_instance_allocations WHERE node_host = %s",
                (NODE_HOST,)
            )
            local_allocs = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM dpu_roce_vlan_pool")
            pool_total = c.fetchone()[0]

            c.execute("SELECT COUNT(*) FROM dpu_roce_vlan_pool WHERE project_id IS NOT NULL")
            pool_used = c.fetchone()[0]

            c.execute("""
                SELECT p.vlan_id, COALESCE(p.project_id,''),
                       COUNT(a.instance_uuid) AS cnt
                FROM dpu_roce_vlan_pool p
                LEFT JOIN dpu_roce_instance_allocations a ON a.vlan_id = p.vlan_id
                WHERE p.project_id IS NOT NULL
                GROUP BY p.vlan_id, p.project_id
            """)
            vlan_rows = c.fetchall()
        db.close()

        _g('roce_sidecar_allocations_total', 'RoCE instance allocations on this node')
        lines.append(f'roce_sidecar_allocations_total{{{node_labels}}} {local_allocs}')

        _g('roce_vlan_pool_total', 'Total VLAN pool capacity (max concurrent tenants)')
        lines.append(f'roce_vlan_pool_total {pool_total}')

        _g('roce_vlan_pool_used', 'VLAN pool entries currently assigned to a tenant')
        lines.append(f'roce_vlan_pool_used {pool_used}')

        _g('roce_vlan_ip_allocations', 'Instance count per active tenant VLAN')
        for vlan_id, project_id, count in vlan_rows:
            lines.append(
                f'roce_vlan_ip_allocations{{vlan_id="{vlan_id}",project_id="{project_id}"}} {count}'
            )
    except Exception as e:
        lines.append(f'# DB query error: {e}')

    return '\n'.join(lines) + '\n', 200, {'Content-Type': 'text/plain; version=0.0.4; charset=utf-8'}


@app.route('/roce/by_vlan/<int:vlan_id>', methods=['GET'])
def by_vlan(vlan_id):
    """DPU watchdog 查询：按 VLAN ID 返回 subnet 和 extra_routes。

    watchdog 扫描 OVS bridge 上各 port 的 VLAN tag，用此接口确认是否为 RoCE 租户口
    并获取 TC ACL 所需参数。查 DB 而非内存，sidecar 重启不影响。
    """
    try:
        db = _db()
        with db.cursor() as c:
            c.execute("""
                SELECT p.net_prefix, p.net_mask, p.extra_routes
                FROM dpu_roce_vlan_pool p
                WHERE p.vlan_id = %s AND p.project_id IS NOT NULL
            """, (vlan_id,))
            row = c.fetchone()
        db.close()
        if not row:
            return jsonify(error='not found'), 404
        net_prefix, net_mask, extra_routes = row
        prefix_len = _mask_to_prefix(net_mask) if net_mask else 24
        subnet = f'{net_prefix}/{prefix_len}'
        return jsonify(subnet=subnet, extra_routes=extra_routes or '')
    except Exception as e:
        return jsonify(error=str(e)), 500


# ── DHCP 服务（AF_PACKET）────────────────────────────────────────────────────

ETH_P_8021Q      = 0x8100
ETH_P_IP         = 0x0800
ETH_P_ALL        = 0x0003
DHCP_SERVER_PORT = 67
DHCP_CLIENT_PORT = 68
DHCP_MAGIC       = b'\x63\x82\x53\x63'
OPT_SUBNET, OPT_ROUTER, OPT_LEASE = 1, 3, 51
OPT_MSG_TYPE, OPT_SERVER, OPT_END = 53, 54, 255
OPT_MTU, OPT_CLASSLESS_ROUTE = 26, 121
MSG_DISCOVER, MSG_OFFER, MSG_REQUEST, MSG_ACK = 1, 2, 3, 5


def _encode_classless_routes(extra_cidrs: str, gw: str) -> bytes:
    """RFC 3442 Classless Static Route option 编码。
    extra_cidrs: 逗号分隔的 CIDR，如 "10.199.70.0/24"。
    每条路由的 next-hop 均为 gw（本子网网关）。
    不追加默认路由，RoCE 网络不应覆盖 VM 的默认网关。
    """
    buf = bytearray()
    routes = []
    for cidr in extra_cidrs.split(','):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            net, prefix = cidr.split('/')
            routes.append((int(prefix), net))
        except ValueError:
            continue
    for prefix, net in routes:
        octets = (prefix + 7) // 8  # 需要编码的地址字节数
        net_bytes = socket.inet_aton(net)[:octets]
        buf.append(prefix)
        buf.extend(net_bytes)
        buf.extend(socket.inet_aton(gw))
    return bytes(buf)


def _parse_dhcp(data: bytes):
    if len(data) < 240 or data[0] != 1 or data[236:240] != DHCP_MAGIC:
        return None
    xid, chaddr, msg_type = data[4:8], data[28:34], None
    i = 240
    while i < len(data):
        t = data[i]
        if t == OPT_END:
            break
        if t == 0:
            i += 1; continue
        if i + 1 >= len(data):
            break
        l = data[i+1]
        if t == OPT_MSG_TYPE and l >= 1:
            msg_type = data[i+2]
        i += 2 + l
    return xid, chaddr, msg_type


def _build_dhcp_reply(msg_type, xid, chaddr, yiaddr, siaddr, gw, mask,
                      mtu: int = 0, extra_routes: str = ''):
    pkt = struct.pack('!BBBBIHH4s4s4s4s',
                      2, 1, 6, 0,
                      struct.unpack('!I', xid)[0], 0, 0,
                      b'\x00'*4, socket.inet_aton(yiaddr),
                      socket.inet_aton(siaddr), b'\x00'*4)
    pkt += chaddr + b'\x00'*10 + b'\x00'*192 + DHCP_MAGIC
    opts = bytearray()
    opts += bytes([OPT_MSG_TYPE, 1, msg_type])
    opts += bytes([OPT_SERVER,   4]) + socket.inet_aton(siaddr)
    opts += bytes([OPT_LEASE,    4]) + struct.pack('!I', 3600)
    opts += bytes([OPT_SUBNET,   4]) + socket.inet_aton(mask)
    if mtu > 0:
        opts += bytes([OPT_MTU, 2]) + struct.pack('!H', mtu)
    if extra_routes:
        route_data = _encode_classless_routes(extra_routes, gw)
        opts += bytes([OPT_CLASSLESS_ROUTE, len(route_data)]) + route_data
    opts += bytes([OPT_END])
    return pkt + bytes(opts)


def _cksum(data):
    if len(data) % 2:
        data += b'\x00'
    s = sum(struct.unpack('!%dH' % (len(data)//2), data))
    s = (s >> 16) + (s & 0xffff)
    s += s >> 16
    return ~s & 0xffff


def _build_eth_udp(src_mac, dst_mac, src_ip, dst_ip, sport, dport, payload, vlan_id):
    udp = struct.pack('!HHHH', sport, dport, 8+len(payload), 0) + payload
    ip  = struct.pack('!BBHHHBBH4s4s',
                      0x45, 0, 20+len(udp), 0, 0, 64, 17, 0,
                      socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip  = ip[:10] + struct.pack('!H', _cksum(ip)) + ip[12:]
    frame = dst_mac + src_mac
    # compute 模式回带 tag；dpu 模式发裸帧（OVS 内部路径，不需要 tag）
    if vlan_id is not None and NODE_TYPE == 'compute':
        frame += struct.pack('!HH', ETH_P_8021Q, vlan_id & 0x0FFF)
    frame += struct.pack('!H', ETH_P_IP) + ip + udp
    return frame


def _dhcp_listener(pf: str):
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                             socket.htons(ETH_P_ALL))
        sock.bind((pf, 0))
    except OSError as e:
        log.warning('AF_PACKET 绑定 %s 失败，DHCP 服务未启动: %s', pf, e)
        return

    import fcntl
    ifreq = struct.pack('16sH6s', pf.encode(), 0, b'\x00'*6)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        res = fcntl.ioctl(s.fileno(), 0x8927, ifreq)
    pf_mac = res[18:24]
    log.info('DHCP 监听启动: iface=%s  mac=%s  mode=%s', pf, pf_mac.hex(':'), NODE_TYPE)

    while True:
        try:
            raw, _ = sock.recvfrom(65535)
        except Exception:
            continue
        if len(raw) < 14:
            continue

        eth_type = struct.unpack('!H', raw[12:14])[0]
        vlan_id = ip_off = None

        if eth_type == ETH_P_8021Q and len(raw) >= 18:
            vlan_id  = struct.unpack('!H', raw[14:16])[0] & 0x0FFF
            eth_type = struct.unpack('!H', raw[16:18])[0]
            ip_off   = 18
        elif eth_type == ETH_P_IP:
            ip_off = 14
        else:
            continue

        if eth_type != ETH_P_IP or len(raw) < ip_off+20:
            continue
        ihl   = (raw[ip_off] & 0x0f) * 4
        proto = raw[ip_off+9]
        if proto != 17:
            continue
        udp_off = ip_off + ihl
        if len(raw) < udp_off+8:
            continue
        if struct.unpack('!H', raw[udp_off+2:udp_off+4])[0] != DHCP_SERVER_PORT:
            continue

        parsed = _parse_dhcp(raw[udp_off+8:])
        if not parsed:
            continue
        xid, chaddr, msg_type = parsed
        if msg_type not in (MSG_DISCOVER, MSG_REQUEST):
            continue

        mac_key = bytes(chaddr)
        with _lock:
            entry = _mac_map.get((vlan_id, mac_key))
            if entry is None:
                # Mellanox eSwitch 可能已剥 VLAN tag（compute），或 dpu 模式本就无 tag
                # 按 MAC-only 回退查找
                for (vid, mk), val in _mac_map.items():
                    if mk == mac_key:
                        entry = val
                        vlan_id = vid
                        break
        if not entry:
            continue

        reply_type = MSG_OFFER if msg_type == MSG_DISCOVER else MSG_ACK
        dhcp_pkt   = _build_dhcp_reply(reply_type, xid, chaddr,
                                        entry['ip'], NODE_IP,
                                        entry['gw'], entry['mask'],
                                        entry.get('mtu', 0),
                                        entry.get('extra_routes', ''))
        frame = _build_eth_udp(pf_mac, bytes([0xff]*6),
                               NODE_IP, '255.255.255.255',
                               DHCP_SERVER_PORT, DHCP_CLIENT_PORT,
                               dhcp_pkt, vlan_id)
        pkt_type_str = 'offer' if reply_type == MSG_OFFER else 'ack'
        try:
            sock.send(frame)
            log.info('DHCP %s  vlan=%s  mac=%s  ip=%s  project=%s',
                     pkt_type_str.upper(),
                     vlan_id, bytes(chaddr).hex(':'), entry['ip'],
                     entry.get('project_id', '-'))
            with _lock:
                _dhcp_counters[(pf, pkt_type_str)] = \
                    _dhcp_counters.get((pf, pkt_type_str), 0) + 1
        except Exception as e:
            log.warning('DHCP 发送失败: %s', e)

# ── 主入口 ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=_load_state_from_db, daemon=True, name='db-state-loader').start()
    for _pf in ROCE_PF_LIST:
        threading.Thread(target=_dhcp_listener, args=(_pf,), daemon=True).start()
    log.info('roce_sidecar 启动  node=%s  mode=%s  port=%d  pf=%s',
             NODE_HOST, NODE_TYPE, HTTP_PORT, ROCE_PF)
    app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True)
