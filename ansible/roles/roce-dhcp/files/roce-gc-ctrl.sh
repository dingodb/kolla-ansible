#!/bin/bash
# roce-gc-ctrl.sh — RoCE 孤儿 DB 记录清理（控制节点，GET_LOCK 单活主备）
#
# 三个控制节点各跑一个 5min timer；经 VIP 连到同一个 mariadb writer（HAProxy 单写），
# 用 GET_LOCK('roce_gc',0) 选主：只有抢到锁的节点实际清理，其余节点 @got<>1 自动 no-op。
# 持锁节点挂掉后，下个周期另一台自然接管（自动 failover）。
#
# 只删 nova.instances 中已 deleted 的分配记录；某 VLAN 已无任何分配时归还池。
# 全程只读 + 幂等原子 SQL，永不触碰活实例的分配。
#
# 依赖：/etc/kolla/roce-gc/gc.env（由 ansible 从 globals/passwords 渲染，含 VIP / DBPASS）
# 用法：roce-gc-ctrl.sh            执行清理
#       roce-gc-ctrl.sh --dry-run  只列出会清理的孤儿，不删除
set -euo pipefail

# shellcheck disable=SC1091
source /etc/kolla/roce-gc/gc.env    # 提供 VIP=... 与 DBPASS=...
MARIADB_CONTAINER="${MARIADB_CONTAINER:-mariadb}"
DRY="${1:-}"

if [[ "$DRY" == "--dry-run" ]]; then
    # -e 查询模式无需 -i（-i 会占用 stdin，作为 systemd/manual 单跑虽无害，仍去掉更干净）
    podman exec -e MYSQL_PWD="$DBPASS" "$MARIADB_CONTAINER" \
        mysql -h "$VIP" -unova nova -t -e "
        SELECT a.instance_uuid, a.vlan_id, a.project_id, a.node_host
        FROM dpu_roce_instance_allocations a
        WHERE NOT EXISTS
          (SELECT 1 FROM instances i
           WHERE i.uuid = a.instance_uuid AND i.deleted = 0);"
    exit 0
fi

# 单连接持锁跑完整个清理：GET_LOCK → 列出并删除孤儿 → 列出并归还空 VLAN → RELEASE_LOCK
# @got 是会话变量，全程有效；没抢到锁的节点每条语句都匹配 0 行。
# 删除前先 SELECT 出每条待删记录的明细（uuid/ip/vlan/project/node）逐行记日志，
# 与 DELETE 在同一持锁会话内，保证「列出的即删除的」。
# 用 ROW_COUNT() 汇总本轮删除/归还数；got_lock 可观测单活。
result=$(podman exec -e MYSQL_PWD="$DBPASS" -i "$MARIADB_CONTAINER" \
    mysql -h "$VIP" -unova nova -N <<'SQL'
SELECT GET_LOCK('roce_gc', 0) INTO @got;

SELECT CONCAT('deleted-alloc uuid=', a.instance_uuid,
              ' ip=',      a.roce_ip,
              ' vlan=',    a.vlan_id,
              ' project=', COALESCE(a.project_id, '-'),
              ' node=',    a.node_host)
FROM dpu_roce_instance_allocations a
WHERE @got = 1 AND NOT EXISTS
  (SELECT 1 FROM instances i WHERE i.uuid = a.instance_uuid AND i.deleted = 0);

DELETE FROM dpu_roce_instance_allocations
WHERE @got = 1 AND NOT EXISTS
  (SELECT 1 FROM instances i
   WHERE i.uuid = dpu_roce_instance_allocations.instance_uuid AND i.deleted = 0);
SELECT ROW_COUNT() INTO @del;

SELECT CONCAT('returned-vlan vlan=', p.vlan_id,
              ' project=', COALESCE(p.project_id, '-'))
FROM dpu_roce_vlan_pool p
WHERE @got = 1 AND p.project_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dpu_roce_instance_allocations a WHERE a.vlan_id = p.vlan_id);

UPDATE dpu_roce_vlan_pool p
SET p.project_id = NULL
WHERE @got = 1 AND p.project_id IS NOT NULL
  AND NOT EXISTS
    (SELECT 1 FROM dpu_roce_instance_allocations a WHERE a.vlan_id = p.vlan_id);
SELECT ROW_COUNT() INTO @vlan;

DO RELEASE_LOCK('roce_gc');

SELECT CONCAT('summary got_lock=', COALESCE(@got, 'NULL'),
              ' deleted=', @del, ' returned_vlans=', @vlan);
SQL
)

# 逐行记日志（每条删除明细/归还明细/汇总各占一条 journal 记录）
while IFS= read -r ln; do
    [[ -z "$ln" ]] && continue
    logger -t roce-gc-ctrl "$ln" 2>/dev/null || true
    echo "$(date '+%F %T') roce-gc-ctrl $ln"
done <<< "$result"
