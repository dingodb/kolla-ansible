# Copyright 2024 NVIDIA Corporation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_config import cfg

dpu_group = cfg.OptGroup(
    'dpu',
    title='DPU Options',
    help="""
Configuration options for DPU driver.
Each nova-compute service manages its own DPU nodes as defined in the
configuration.
""")

dpu_options = [
    cfg.MultiStrOpt(
        'nodes',
        default=[],
        help="""
List of DPU nodes managed by this nova-compute service.

Each line should be a JSON string containing node information:
- uuid: Unique identifier for the node
- cpus: Number of CPUs
- memory_mb: Memory in MB
- local_gb: Local disk size in GB
- cpu_arch: CPU architecture (e.g., x86_64, aarch64)
- resource_class: Resource class identifier (optional)

Example:
  nodes = {"uuid": "node-uuid-1", "cpus": 8, "memory_mb": 16384, "local_gb": 100, "cpu_arch": "x86_64"}
  nodes = {"uuid": "node-uuid-2", "cpus": 16, "memory_mb": 32768, "local_gb": 200, "cpu_arch": "x86_64"}

Note: Each 'nodes =' line defines one node. Multiple nodes require multiple lines.
"""),
    cfg.IntOpt(
        'default_vcpus',
        default=8,
        min=1,
        help="""
Default VCPU inventory for a DPU node when `cpus` is not provided in
`[dpu]/nodes`.
"""
    ),
    cfg.IntOpt(
        'default_memory_mb',
        default=16384,
        min=1,
        help="""
Default MEMORY_MB inventory for a DPU node when `memory_mb` is not provided in
`[dpu]/nodes`.
"""
    ),
    cfg.IntOpt(
        'default_local_gb',
        default=100,
        min=1,
        help="""
Default DISK_GB inventory for a DPU node when `local_gb` is not provided in
`[dpu]/nodes`.
"""
    ),
    cfg.StrOpt(
        'spdk_endpoint',
        default=None,
        help="""
SPDK service endpoint URL (reserved for future use).

This will be used to connect to SPDK service for volume management.
"""),
    cfg.StrOpt(
        'spdk_volume_bdev_type',
        default='bdev_aio',
        help="""
Which SPDK block device backend to use for Cinder RBD volumes and ephemeral
RBD root disks when exposing them to the DPU NVMe stack.

* ``bdev_aio`` — Create ``bdev_aio`` on top of a kernel block device path.
  Requires a mapped device (``rbd map``) so ``device_path`` exists before
  SPDK attaches the bdev (legacy behavior).

* ``bdev_rbd`` — Create ``bdev_rbd`` directly from Ceph pool/image via SPDK.
  Kernel ``rbd map`` is skipped for SPDK attach; SPDK talks to librbd.

Config drive ISO attachment always uses ``bdev_aio`` regardless of this
option (see ConfigDriveManager).

Valid values: bdev_aio, bdev_rbd
"""
    ),
    cfg.StrOpt(
        'sf_endpoint',
        default=None,
        help="""
SF (Smart Fabric) service endpoint URL (reserved for future use).

This will be used to connect to SF service for network interface management.
"""),
    cfg.IntOpt(
        'api_retry_interval',
        default=2,
        min=0,
        help="""
The number of seconds to wait before retrying operations.

Related options:
* api_max_retries
"""),
    cfg.IntOpt(
        'api_max_retries',
        default=60,
        min=0,
        help="""
The number of times to retry when an operation fails.
If set to 0, only try once, no retries.

Related options:
* api_retry_interval
"""),
    cfg.StrOpt(
        'ceph_cluster_name',
        default='ceph',
        help="""
Name of the Ceph cluster that stores Glance images and ephemeral root disks.
Used when the DPU driver clones Glance images into Ceph RBD volumes.
"""
    ),
    cfg.StrOpt(
        'images_rbd_pool',
        default=None,
        help="""
The RADOS pool in which RBD volumes are stored for DPU instances.

This pool is used when cloning Glance images into RBD volumes for ephemeral
root disks. If not specified, defaults to the libvirt images_rbd_pool value.

Related options:
* ceph_cluster_name
* [libvirt]/images_rbd_pool
"""
    ),
    cfg.StrOpt(
        'sf_pci_address',
        default='pci/0000:03:00.0',
        help="""
PCI address used for creating SF (SmartNIC Functions).

This specifies the physical NIC device on which SF instances will be created.
Format: pci/domain:bus:device.function

Example: pci/0000:03:00.0

Related options:
* sf_pfnum
"""
    ),
    cfg.IntOpt(
        'sf_num_min',
        default=10,
        min=1,
        max=65535,
        help="""
Minimum SF number to use when creating SF instances.

SF numbers must be in the range [sf_num_min, sf_num_max]. The driver will
automatically find available SF numbers within this range when creating
new network interfaces.

Related options:
* sf_num_max
"""
    ),
    cfg.IntOpt(
        'sf_num_max',
        default=1000,
        min=1,
        max=65535,
        help="""
Maximum SF number to use when creating SF instances.

SF numbers must be in the range [sf_num_min, sf_num_max]. The driver will
automatically find available SF numbers within this range when creating
new network interfaces.

Related options:
* sf_num_min
"""
    ),
    cfg.IntOpt(
        'sf_pfnum',
        default=0,
        min=0,
        help="""
Physical Function (PF) number for creating SF instances.

This specifies which PF on the NIC to use for creating SF instances.
Different NICs or configurations may use different PF numbers. Typically
this is 0 for the first PF.

Related options:
* sf_pci_address
"""
    ),
    cfg.StrOpt(
        'mlxdevm_path',
        default='/opt/mellanox/iproute2/sbin/mlxdevm',
        help="""
Path to the mlxdevm command for managing Mellanox devices.

This command is used to create, configure, and delete SF instances.
"""
    ),
    cfg.StrOpt(
        'ovs_bridge',
        default='br-int',
        help="""
Name of the OVS bridge to which SF representor ports will be added.

The SF representor ports are the host-side network interfaces that
correspond to SF instances on the DPU.
"""
    ),
    cfg.StrOpt(
        'sf_representor_prefix',
        default='en3f0c1pf0sf',
        help="""
Prefix for SF representor interface names.

The full representor name is constructed as: prefix + sfnum.
For example, with prefix 'en3f0c1pf0sf' and sfnum 4, the representor
name would be 'en3f0c1pf0sf4'.

The prefix depends on the NIC hardware and configuration. To find the
correct prefix, create a test SF and check the representor name using:
  ip link show

Related options:
* sf_num_min
* sf_num_max
"""
    ),
    cfg.StrOpt(
        'sf_ovs_offload_mode',
        default='system',
        choices=['system', 'doca'],
        help="""
OVS offload mode used when adding SF representor ports to bridge.

* ``system`` - Default mode. Add representor as OVS system interface type:
  ``ovs-vsctl add-port <bridge> <representor> -- set Interface <representor> type=system``.
* ``doca`` - Add representor as DOCA interface type:
  ``ovs-vsctl add-port <bridge> <representor> -- set Interface <representor> type=doca``.
"""
    ),
    cfg.StrOpt(
        'ipmi_host',
        default=None,
        help="""
IPMI BMC host/IP used for out-of-band reboot before finalizing spawn.
"""),
    cfg.StrOpt(
        'ipmi_username',
        default=None,
        help="""
IPMI username.
"""),
    cfg.StrOpt(
        'ipmi_password',
        default=None,
        secret=True,
        help="""
IPMI password.
"""),
    cfg.StrOpt(
        'dpu_nqn',
        default='nqn.2022-10.io.nvda.nvme:0',
        help="""
NQN used by the SNAP NVMe controller for DPU volumes.

This is passed to the underlying SNAP layer (when supported) so that
the exposed NVMe namespaces use a stable, configurable NQN.
"""),
    cfg.StrOpt(
        'dpu_nvme_ctrl_name',
        default=None,
        help="""
Name of the SNAP NVMe controller used to attach namespaces for DPU volumes.

If not set, the driver will choose a sensible default based on the detected
DPU platform:
* BF2: NvmeEmu0pf0
* BF3: NVMeCtrl1
"""),
    cfg.StrOpt(
        'force_bf_mode',
        default='auto',
        help="""
Force DPU hardware mode detection.

Valid values:
* 'auto' - auto-detect via lspci (default)
* 'bf2'  - force treat the platform as BlueField-2
* 'bf3'  - force treat the platform as BlueField-3
"""),
    cfg.StrOpt(
        'vnc_listen_address',
        default=None,
        help="""
IP address or hostname where the DPU VNC proxy (e.g. websockify BMC proxy)
listens. nova-novncproxy will connect here.

If not set, the driver uses [vnc] server_proxyclient_address (typically
the compute node IP). Each instance gets a distinct port in the configured
range to avoid multi-tenant sharing the same BMC console.

Related options:
* vnc_listen_port (fixed port, e.g. for SOL-VNC agent on DPU)
* vnc_listen_port_base
* vnc_listen_port_range
* [vnc] server_proxyclient_address
"""),
    cfg.IntOpt(
        'vnc_listen_port',
        default=0,
        min=0,
        max=65535,
        help="""
Fixed TCP port for DPU VNC (e.g. SOL-VNC agent on DPU). When set (e.g. 5900),
get_vnc_console returns (vnc_listen_address, vnc_listen_port). Use with
sol-vnc-agent fixed_port or dpu-vnc-proxy fixed_port. When 0, port is
computed per instance: vnc_listen_port_base + (hash(instance.uuid) %% range).
"""),
    cfg.PortOpt(
        'vnc_listen_port_base',
        default=6090,
        help="""
Base TCP port for the DPU VNC proxy. Instance port is computed as:
base + (hash(instance.uuid) % port_range). The proxy must use the same
mapping for port -> BMC.

Related options:
* vnc_listen_address
* vnc_listen_port_range
"""),
    cfg.IntOpt(
        'vnc_listen_port_range',
        default=1000,
        min=1,
        help="""
Port range size for instance-level VNC ports. Ports will be in
[vnc_listen_port_base, vnc_listen_port_base + range - 1]. Prevents
multiple instances from sharing the same BMC console.

Related options:
* vnc_listen_port_base
"""),
    cfg.StrOpt(
        'roce_sidecar_url',
        default='http://127.0.0.1:9967',
        help="""
URL of the local RoCE DHCP sidecar service.
"""),
    cfg.IntOpt(
        'roce_sf_num_min',
        default=800,
        min=1,
        max=65535,
        help="""
Minimum SF number for RoCE-dedicated scalable functions.
Must not overlap with sf_num_min/sf_num_max range.
"""),
    cfg.IntOpt(
        'roce_sf_num_max',
        default=999,
        min=1,
        max=65535,
        help="""
Maximum SF number for RoCE-dedicated scalable functions.
"""),
    cfg.StrOpt(
        'roce_ovs_bridge',
        default='br-ex',
        help="""
OVS bridge for RoCE SF representor ports.
Separate from br-int so OVN does not manage RoCE traffic.
"""),
    cfg.IntOpt(
        'roce_mtu',
        default=9000,
        min=0,
        help="""
MTU to set on RoCE SF representor interfaces after creation.
Set to 0 to skip MTU configuration.
"""),
]


def register_opts(conf):
    """Register DPU configuration options."""
    conf.register_group(dpu_group)
    conf.register_opts(dpu_options, group=dpu_group)


def list_opts():
    """Return DPU configuration options."""
    return {dpu_group: dpu_options}

