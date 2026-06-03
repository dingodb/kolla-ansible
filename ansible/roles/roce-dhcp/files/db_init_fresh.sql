-- RoCE DB 全新初始化（直接建 v3 正确 schema，跳过增量迁移）
-- 适用场景：全新环境首次部署（数据库中尚无 dpu_roce_* 表）
-- 执行方式（在部署节点）：
--   PASS=$(grep nova_database_password /etc/kolla/passwords.yml | awk '{print $2}')
--   mysql -h 10.199.71.250 -u nova -p"$PASS" nova < /path/to/db_init_fresh.sql

USE nova;

CREATE TABLE IF NOT EXISTS dpu_roce_vlan_pool (
    vlan_id       INT               NOT NULL COMMENT 'VLAN ID',
    net_prefix    VARCHAR(39)       NOT NULL COMMENT '网络地址，如 11.11.1.0',
    net_mask      VARCHAR(18)       NOT NULL DEFAULT '255.255.255.0',
    net_gw        VARCHAR(39)       NOT NULL COMMENT 'DHCP GW',
    project_id    VARCHAR(36)       DEFAULT NULL COMMENT 'NULL=空闲，非 NULL=已被该租户独占',
    mtu           SMALLINT UNSIGNED DEFAULT NULL COMMENT 'DHCP option 26，NULL=不下发',
    extra_routes  VARCHAR(512)      DEFAULT NULL COMMENT 'DHCP option 121，逗号分隔 CIDR，NULL=不下发',
    updated_at    DATETIME          DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (vlan_id),
    INDEX idx_project (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='RoCE DHCP VLAN/IP 池';

CREATE TABLE IF NOT EXISTS dpu_roce_instance_allocations (
    instance_uuid VARCHAR(36)  NOT NULL,
    roce_mac      VARCHAR(17)  NOT NULL COMMENT 'SHA256(uuid) 生成的 MAC，前缀 02:',
    roce_ip       VARCHAR(39)  NOT NULL,
    vlan_id       INT          NOT NULL,
    node_host     VARCHAR(255) NOT NULL COMMENT 'sidecar 重启时按此字段过滤加载 _mac_map',
    node_type     ENUM('compute','dpu') NOT NULL DEFAULT 'compute',
    project_id    VARCHAR(36)  NOT NULL DEFAULT '',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (instance_uuid),
    INDEX idx_mac (roce_mac),
    INDEX idx_vlan (vlan_id),
    INDEX idx_project (project_id),
    CONSTRAINT fk_roce_vlan FOREIGN KEY (vlan_id)
        REFERENCES dpu_roce_vlan_pool(vlan_id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='RoCE DHCP 实例分配记录';

-- ── 71 环境 VLAN 池数据（来自租户rocevlan.xlsx）────────────────────────────────
-- VLAN 2001-2020 / subnet 11.11.{n}.0/24 / GW .254 / MTU 9000 / extra_routes 10.199.70.0/24
INSERT IGNORE INTO dpu_roce_vlan_pool
    (vlan_id, net_prefix, net_mask, net_gw, mtu, extra_routes)
VALUES
(2001, '11.11.1.0',  '255.255.255.0', '11.11.1.254',  9000, '10.199.70.0/24'),
(2002, '11.11.2.0',  '255.255.255.0', '11.11.2.254',  9000, '10.199.70.0/24'),
(2003, '11.11.3.0',  '255.255.255.0', '11.11.3.254',  9000, '10.199.70.0/24'),
(2004, '11.11.4.0',  '255.255.255.0', '11.11.4.254',  9000, '10.199.70.0/24'),
(2005, '11.11.5.0',  '255.255.255.0', '11.11.5.254',  9000, '10.199.70.0/24'),
(2006, '11.11.6.0',  '255.255.255.0', '11.11.6.254',  9000, '10.199.70.0/24'),
(2007, '11.11.7.0',  '255.255.255.0', '11.11.7.254',  9000, '10.199.70.0/24'),
(2008, '11.11.8.0',  '255.255.255.0', '11.11.8.254',  9000, '10.199.70.0/24'),
(2009, '11.11.9.0',  '255.255.255.0', '11.11.9.254',  9000, '10.199.70.0/24'),
(2010, '11.11.10.0', '255.255.255.0', '11.11.10.254', 9000, '10.199.70.0/24'),
(2011, '11.11.11.0', '255.255.255.0', '11.11.11.254', 9000, '10.199.70.0/24'),
(2012, '11.11.12.0', '255.255.255.0', '11.11.12.254', 9000, '10.199.70.0/24'),
(2013, '11.11.13.0', '255.255.255.0', '11.11.13.254', 9000, '10.199.70.0/24'),
(2014, '11.11.14.0', '255.255.255.0', '11.11.14.254', 9000, '10.199.70.0/24'),
(2015, '11.11.15.0', '255.255.255.0', '11.11.15.254', 9000, '10.199.70.0/24'),
(2016, '11.11.16.0', '255.255.255.0', '11.11.16.254', 9000, '10.199.70.0/24'),
(2017, '11.11.17.0', '255.255.255.0', '11.11.17.254', 9000, '10.199.70.0/24'),
(2018, '11.11.18.0', '255.255.255.0', '11.11.18.254', 9000, '10.199.70.0/24'),
(2019, '11.11.19.0', '255.255.255.0', '11.11.19.254', 9000, '10.199.70.0/24'),
(2020, '11.11.20.0', '255.255.255.0', '11.11.20.254', 9000, '10.199.70.0/24');

-- 验证
SELECT vlan_id, net_prefix, net_mask, net_gw, mtu, extra_routes, project_id
FROM dpu_roce_vlan_pool ORDER BY vlan_id;
