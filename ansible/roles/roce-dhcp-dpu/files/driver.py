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

"""
DPU driver for Nova Compute.

This driver manages DPU nodes directly without using Ironic API.
It uses SPDK for volume management and SF for network interface management.
"""

import collections
import copy
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import tempfile
import time
import uuid
from copy import deepcopy

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_concurrency import processutils
from oslo_utils import excutils
from oslo_utils import units

from nova import block_device
from nova.console import type as console_type
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
import nova.conf
from nova import exception
from nova import context as nova_context
from nova.i18n import _
from nova import objects
from nova.objects import fields as obj_fields
from nova import utils
from nova.image import glance
from nova.virt import configdrive
from nova.virt import driver as virt_driver
from nova.virt import hardware
from nova.virt import images
from nova.virt.dpu import configdrive_util
from nova.virt.dpu import ipmi_util
from nova.virt.dpu import conf as dpu_conf
from nova.virt.dpu import dpu_states
from nova.virt.dpu import sf_client
from nova.virt.dpu import spdk_client
from nova.virt.dpu import spdk_rpc
from nova.virt.dpu import spdk_volume_policy as vol_policy
from nova.virt.dpu import snap_rpc
from nova.virt.dpu import roce_client as dpu_roce_client
from nova.storage import rbd_utils

LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF

# Register DPU configuration options
dpu_conf.register_opts(CONF)


class InjectionInfo(collections.namedtuple(
        'InjectionInfo', ['network_info', 'files', 'admin_pass'])):
    __slots__ = ()

    def __repr__(self):
        return ('InjectionInfo(network_info=%r, files=%r, '
                'admin_pass=<SANITIZED>)') % (self.network_info, self.files)

_POWER_STATE_MAP = {
    dpu_states.POWER_ON: power_state.RUNNING,
    dpu_states.NOSTATE: power_state.NOSTATE,
    dpu_states.POWER_OFF: power_state.SHUTDOWN,
}


def map_power_state(state):
    """Map DPU power state to Nova power state."""
    try:
        return _POWER_STATE_MAP.get(state, power_state.NOSTATE)
    except KeyError:
        LOG.warning("Power state %s not found.", state)
        return power_state.NOSTATE


def _get_nodes_supported_instances(cpu_arch=None):
    """Return supported instances for a node."""
    if not cpu_arch:
        return []
    return [(cpu_arch,
             obj_fields.HVType.BAREMETAL,
             obj_fields.VMMode.HVM)]


class DPUNode(object):
    """Represents a DPU node."""

    def __init__(self, node_config):
        """Initialize DPU node from configuration.

        :param node_config: Dictionary or JSON string containing node info
        :raises: ValueError if node_config cannot be parsed
        """
        # Parse JSON string if needed
        if isinstance(node_config, str):
            # Remove leading/trailing whitespace
            config_str = node_config.strip()
            # Remove surrounding quotes if present (e.g., from config file)
            if (config_str.startswith("'") and config_str.endswith("'")) or \
               (config_str.startswith('"') and config_str.endswith('"')):
                config_str = config_str[1:-1]
            
            try:
                node_config = json.loads(config_str)
            except json.JSONDecodeError as e:
                raise ValueError(
                    "Failed to parse node configuration as JSON: %s. "
                    "Error: %s" % (config_str[:100], str(e)))
        elif not isinstance(node_config, dict):
            raise ValueError(
                "Node configuration must be a dictionary or JSON string, "
                "got %s" % type(node_config).__name__)
        
        # Friendly name used as ComputeNode.hypervisor_hostname.
        # Default to local nova-compute host since per-user requirement is
        # "each DPU host runs one machine".
        self.hostname = (node_config.get('hostname') or
                         node_config.get('name') or
                         CONF.host or
                         '')

        # Extract node UUID. If not provided, derive a stable UUID from hostname.
        # This keeps the node identity stable across restarts without requiring
        # users to configure per-node UUIDs.
        self.uuid = node_config.get('uuid')
        if not self.uuid:
            if not self.hostname:
                raise ValueError(
                    "Node configuration missing both 'uuid' and hostname/name; "
                    "cannot derive stable UUID")
            # Use a fixed namespace to avoid collisions with other generators.
            self.uuid = str(uuid.uuid5(uuid.NAMESPACE_OID, f"nova-dpu:{self.hostname}"))
        self.uuid = str(self.uuid)
        
        # Keep these optional; DPU scheduling is typically driven by placement
        # inventory using resource_class (total=1).
        # Fall back to configurable defaults so standard inventories
        # (VCPU/MEMORY_MB/DISK_GB) are always present even if omitted per-node.
        self.cpus = int(node_config.get('cpus') or CONF.dpu.default_vcpus)
        self.memory_mb = int(node_config.get('memory_mb') or
                             CONF.dpu.default_memory_mb)
        self.local_gb = int(node_config.get('local_gb') or
                            CONF.dpu.default_local_gb)
        self.cpu_arch = node_config.get('cpu_arch', 'x86_64')
        # Unified custom resource class for all DPU nodes.
        #
        # NOTE: nova.utils.normalize_rc_name() 会自动加 `oslo_resource_classes`
        # 的 CUSTOM_NAMESPACE 前缀（通常是 `CUSTOM_`），所以这里不要再带
        # CUSTOM_ 前缀，否则会变成 CUSTOM_CUSTOM_DPU_X。
        self.resource_class = 'DPU_X'
        
        # Instance management
        self.instance_id = None
        self.provision_state = dpu_states.AVAILABLE
        self.power_state = dpu_states.POWER_OFF
        self.maintenance = False
        
        # Properties for compatibility
        self.properties = {
            'cpus': self.cpus,
            'memory_mb': self.memory_mb,
            'local_gb': self.local_gb,
            'cpu_arch': self.cpu_arch,
        }
        
        # Unified custom trait prefix: CUSTOM_TRAIT_{HOSTNAME}
        # OpenStack traits must be uppercase and use underscores.
        hn = str(self.hostname).upper().replace('-', '_')
        hn = re.sub(r'[^A-Z0-9_]', '_', hn)
        self.traits = [f'CUSTOM_TRAIT_{hn}']

    @property
    def id(self):
        """Return node UUID for compatibility."""
        return self.uuid

    @property
    def is_maintenance(self):
        """Return maintenance status."""
        return self.maintenance


class DPUDriver(virt_driver.ComputeDriver):
    """Hypervisor driver for DPU."""

    capabilities = {
        "has_imagecache": False,
        "supports_evacuate": False,
        "supports_migrate_to_same_host": False,
        "supports_attach_interface": True,
        "supports_multiattach": False,
        "supports_trusted_certs": False,
        "supports_pcpus": False,
        "supports_accelerators": False,
        "supports_remote_managed_ports": False,
        "supports_address_space_passthrough": False,
        "supports_address_space_emulated": False,
        "supports_stateless_firmware": False,
        "supports_virtio_fs": False,
        "supports_mem_backing_file": False,
        "supports_extend_volume": True,

        # Image type support flags
        "supports_image_type_aki": False,
        "supports_image_type_ami": True,
        "supports_image_type_ari": False,
        "supports_image_type_iso": False,
        "supports_image_type_qcow2": True,
        "supports_image_type_raw": True,
        "supports_image_type_vdi": False,
        "supports_image_type_vhd": False,
        "supports_image_type_vhdx": False,
        "supports_image_type_vmdk": False,
        "supports_image_type_ploop": False,
    }

    rebalances_nodes = False

    def __init__(self, virtapi, read_only=False):
        """Initialize DPU driver.

        :param virtapi: VirtAPI instance
        :param read_only: Whether driver is in read-only mode
        """
        super().__init__(virtapi)

        self.node_cache = {}
        # Map UUID -> nodename (hypervisor_hostname). Used for backward compat
        # when instances / callers still refer to node UUID.
        self._uuid_to_nodename = {}
        self.node_cache_time = 0
        
        # Initialize SPDK and SF clients
        self.spdk_client = spdk_client.SPDKClient(
            endpoint=CONF.dpu.spdk_endpoint
        )
        self.sf_client = sf_client.SFClient(
            endpoint=CONF.dpu.sf_endpoint
        )
        self.roce_client = dpu_roce_client.RoCEClient()
        self.roce_sf_client = sf_client.RoCESFClient()
        self.image_api = glance.get_default_image_service()
        self._ephemeral_root_volumes = {}
        
        # Initialize config drive manager
        self.configdrive_manager = configdrive_util.ConfigDriveManager(self.spdk_client)
        
        # Load nodes from configuration
        self._load_nodes_from_config()

    def get_host_uptime(self):
        """Returns the result of calling "uptime"."""
        out, err = processutils.execute('env', 'LANG=C', 'uptime')
        return out

    def get_host_ip_addr(self):
        """Return a stable host IP for migration bookkeeping.

        Nova resource tracker uses this value when claiming an existing
        migration record (e.g. resize/cold-migrate). Returning NotImplemented
        here breaks scheduling with NoValidHost.
        """
        # Prefer explicit host identity from nova.conf.
        if getattr(CONF, 'my_ip', None):
            return CONF.my_ip
        # Fallback to DPU management address if configured.
        if getattr(CONF.dpu, 'vnc_listen_address', None):
            return CONF.dpu.vnc_listen_address
        # Last resort: resolve local hostname.
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'

    def _load_nodes_from_config(self):
        """Load DPU nodes from configuration."""
        node_configs = CONF.dpu.nodes
        self.node_cache = {}
        self._uuid_to_nodename = {}
        
        if not node_configs:
            # Default to a single local node when not configured.
            # Node UUID is derived from CONF.host to remain stable across restarts.
            LOG.info("No DPU nodes configured in [dpu] nodes option; "
                     "defaulting to a single node for host %s", CONF.host)
            node_configs = [{}]
        
        for node_config in node_configs:
            try:
                node = DPUNode(node_config)
                if not node.uuid:
                    LOG.warning("Skipping node with missing UUID: %s",
                               node_config)
                    continue
                
                nodename = str(node.hostname)
                # Avoid collisions if multiple nodes are configured on one host.
                # This keeps "no hostname config required" behavior for single-node
                # deployments, while still working if multiple nodes exist.
                if nodename in self.node_cache:
                    nodename = f"{nodename}_{str(node.uuid)[:8]}"
                    node.hostname = nodename
                    hn = str(node.hostname).upper().replace('-', '_')
                    hn = re.sub(r'[^A-Z0-9_]', '_', hn)
                    node.traits = [f'CUSTOM_TRAIT_{hn}']

                # Check for duplicate nodenames (hypervisor_hostname)
                if nodename in self.node_cache:
                    LOG.warning("Duplicate node hostname/nodename %s found, skipping",
                               nodename)
                    continue

                self.node_cache[nodename] = node
                self._uuid_to_nodename[str(node.uuid)] = nodename
                LOG.info("Loaded DPU node: %s (cpus=%d, memory_mb=%d, "
                        "local_gb=%d, cpu_arch=%s)",
                        node.uuid, node.cpus, node.memory_mb,
                        node.local_gb, node.cpu_arch)
            except ValueError as e:
                LOG.error("Invalid node configuration format: %s. "
                         "Error: %s", node_config[:100], e)
            except Exception as e:
                LOG.error("Failed to load node configuration %s: %s",
                         node_config[:100] if isinstance(node_config, str)
                         else node_config, e, exc_info=True)
        
        self.node_cache_time = time.time()
        LOG.info("Loaded %d DPU nodes from configuration",
                len(self.node_cache))

    def _get_node(self, node_id):
        """Get a node by its UUID.

        :param node_id: Node UUID
        :returns: DPUNode object
        :raises: exception.InstanceNotFound if node not found
        """
        # node_id is ComputeNode.hypervisor_hostname (nodename).
        # For backward compatibility we also accept UUID.
        if node_id not in self.node_cache:
            mapped = self._uuid_to_nodename.get(str(node_id))
            if mapped and mapped in self.node_cache:
                return self.node_cache[mapped]

            LOG.warning("Node %s not found in cache", node_id)
            # Refresh cache and try again
            self._load_nodes_from_config()

            if node_id in self.node_cache:
                return self.node_cache[node_id]
            mapped = self._uuid_to_nodename.get(str(node_id))
            if mapped and mapped in self.node_cache:
                return self.node_cache[mapped]

            raise exception.InstanceNotFound(
                instance_id="Node %s not found" % node_id)
        return self.node_cache[node_id]

    def _node_resource(self, node):
        """Create resource dict from node stats.

        :param node: DPUNode object
        :returns: Dictionary with resource information
        """
        cpu_arch = node.cpu_arch
        if cpu_arch:
            try:
                cpu_arch = obj_fields.Architecture.canonicalize(cpu_arch)
            except exception.InvalidArchitectureName:
                cpu_arch = None

        vcpus = node.cpus
        memory_mb = node.memory_mb
        local_gb = node.local_gb

        vcpus_used = 0
        memory_mb_used = 0
        local_gb_used = 0

        if node.instance_id:
            # Node is in use
            vcpus_used = vcpus
            memory_mb_used = memory_mb
            local_gb_used = local_gb

        nodes_extra_specs = {}
        if cpu_arch:
            nodes_extra_specs['cpu_arch'] = node.cpu_arch

        dic = {
            'uuid': str(node.uuid),
            'hypervisor_hostname': str(node.hostname),
            'hypervisor_type': self._get_hypervisor_type(),
            'hypervisor_version': 1,
            'resource_class': node.resource_class or 'DPU',
            'cpu_info': None,
            'vcpus': vcpus,
            'vcpus_used': vcpus_used,
            'local_gb': local_gb,
            'local_gb_used': local_gb_used,
            'disk_available_least': local_gb - local_gb_used,
            'memory_mb': memory_mb,
            'memory_mb_used': memory_mb_used,
            'supported_instances': _get_nodes_supported_instances(cpu_arch),
            'stats': nodes_extra_specs,
            'numa_topology': None,
        }
        return dic

    def _node_resources_used(self, node):
        """Check if node resources are currently used.

        :param node: DPUNode object
        :returns: True if node is in use
        """
        return node.instance_id is not None

    def _node_resources_unavailable(self, node):
        """Check if node resources are unavailable.

        :param node: DPUNode object
        :returns: True if node is unavailable
        """
        bad_power_states = [dpu_states.ERROR, dpu_states.NOSTATE]
        good_provision_states = [dpu_states.AVAILABLE, dpu_states.NOSTATE]
        return (node.is_maintenance or
                node.power_state in bad_power_states or
                node.provision_state not in good_provision_states)

    def init_host(self, host):
        """Initialize the driver for the given host.

        :param host: Hostname
        """
        LOG.info("Initializing DPU driver for host %s", host)
        # Refresh node cache
        self._load_nodes_from_config()
        
        # Recover storage and network for existing instances
        # This is called when compute node restarts after a crash
        try:
            self._recover_existing_instances()
        except Exception as exc:
            LOG.warning("Failed to recover existing instances during init_host: %s", exc)

    def _get_hypervisor_type(self):
        """Get hypervisor type."""
        return 'dpu'

    def _get_hypervisor_version(self):
        """Get hypervisor version."""
        return 1

    def get_available_nodes(self, refresh=False):
        """Return list of available node UUIDs.

        :param refresh: Whether to refresh cache (ignored for now)
        :returns: List of node UUIDs
        """
        if refresh or not self.node_cache:
            self._load_nodes_from_config()
        # Keys are ComputeNode.hypervisor_hostname (nodename).
        return list(self.node_cache.keys())

    def get_available_resource(self, nodename):
        """Retrieve resource information for a node.

        :param nodename: ComputeNode.hypervisor_hostname (nodename)
        :returns: Dictionary describing resources
        """
        if not self.node_cache:
            self._load_nodes_from_config()

        node = self._get_node(nodename)
        return self._node_resource(node)

    def update_provider_tree(self, provider_tree, nodename, allocations=None):
        """Update ProviderTree with current resource information.

        :param provider_tree: ProviderTree object
        :param nodename: ComputeNode.hypervisor_hostname (nodename)
        :param allocations: Allocation information
        """
        node = self._get_node(nodename)

        # NOTE: reserved=1 会让 total=1 的资源类无法再进行正常调度/resize。
        # 仅在维护模式下将资源保留，其余场景保持 reserved=0。
        reserved = bool(node.is_maintenance)

        info = self._node_resource(node)
        inv = provider_tree.data(nodename).inventory
        ratios = self._get_allocation_ratios(inv)
        result = {
            'VCPU': {
                'total': info['vcpus'],
                'reserved': CONF.reserved_host_cpus,
                'min_unit': 1,
                'max_unit': info['vcpus'],
                'step_size': 1,
                'allocation_ratio': ratios['VCPU'],
            },
            'MEMORY_MB': {
                'total': info['memory_mb'],
                'reserved': CONF.reserved_host_memory_mb,
                'min_unit': 1,
                'max_unit': info['memory_mb'],
                'step_size': 1,
                'allocation_ratio': ratios['MEMORY_MB'],
            },
            'DISK_GB': {
                'total': info['local_gb'],
                'reserved': self._get_reserved_host_disk_gb_from_config(),
                'min_unit': 1,
                'max_unit': info['local_gb'],
                'step_size': 1,
                'allocation_ratio': ratios['DISK_GB'],
            },
        }

        rc_name = info.get('resource_class', 'DPU')
        norm_name = utils.normalize_rc_name(rc_name)
        if norm_name is not None:
            result[norm_name] = {
                'total': 1,
                'reserved': int(reserved),
                'min_unit': 1,
                'max_unit': 1,
                'step_size': 1,
                'allocation_ratio': 1.0,
            }

        provider_tree.update_inventory(nodename, result)
        if node.traits:
            provider_tree.update_traits(nodename, node.traits)

    def get_info(self, instance, use_cache=True):
        """Get the current status of an instance.

        :param instance: Instance object
        :param use_cache: Whether to use cache (ignored for now)
        :returns: InstanceInfo object
        """
        try:
            node = self._get_node(instance.node)
            if node.instance_id == instance.uuid:
                return hardware.InstanceInfo(
                    state=map_power_state(node.power_state))
            else:
                # Instance not found on this node
                return hardware.InstanceInfo(state=power_state.NOSTATE)
        except exception.InstanceNotFound:
            return hardware.InstanceInfo(state=power_state.NOSTATE)

    def instance_exists(self, instance):
        """Check if instance exists.

        :param instance: Instance object
        :returns: True if instance exists
        """
        try:
            node = self._get_node(instance.node)
            return node.instance_id == instance.uuid
        except exception.InstanceNotFound:
            return False

    def list_instance_uuids(self):
        """Return list of instance UUIDs.

        :returns: List of instance UUIDs
        """
        uuids = []
        for node in self.node_cache.values():
            if node.instance_id:
                uuids.append(node.instance_id)
        return uuids

    def list_instances(self):
        """Return the names of all the instances provisioned.

        :returns: a list of instance names.
        """
        if not self.node_cache:
            self._load_nodes_from_config()

        context = nova_context.get_admin_context()
        instance_names = []
        for node in self.node_cache.values():
            if node.instance_id:
                try:
                    instance = objects.Instance.get_by_uuid(
                        context, node.instance_id)
                    instance_names.append(instance.name)
                except exception.InstanceNotFound:
                    LOG.warning("Instance %s not found for node %s",
                               node.instance_id, node.uuid)
        return instance_names

    def get_nodenames_by_uuid(self, refresh=False):
        """Get nodename mapping by UUID.

        :param refresh: Whether to refresh cache
        :returns: Dictionary mapping UUIDs to nodenames
        """
        if refresh or not self.node_cache:
            self._load_nodes_from_config()
        # uuid -> nodename (hypervisor_hostname)
        return dict(self._uuid_to_nodename)

    def node_is_available(self, nodename):
        """Check if node is available.

        :param nodename: Node UUID
        :returns: True if node exists
        """
        try:
            self._get_node(nodename)
            return True
        except exception.InstanceNotFound:
            return False

    def prepare_for_spawn(self, instance):
        """Prepare to spawn instance.

        :param instance: Instance object
        """
        LOG.debug('Preparing to spawn instance %s.', instance.uuid)
        node_id = instance.get('node')
        if not node_id:
            msg = _(
                "DPU node uuid not supplied to "
                "driver for instance %s."
            ) % instance.id
            raise exception.NovaException(msg)
        
        node = self._get_node(node_id)
        
        # Check if node is available
        if (self._node_resources_used(node) or
            self._node_resources_unavailable(node)):
            msg = "Chosen DPU node %s is not available" % node_id
            LOG.info(msg, instance=instance)
            raise exception.ComputeResourcesUnavailable(reason=msg)
        
        # Reserve the node for this instance
        node.instance_id = instance.uuid
        node.provision_state = dpu_states.DEPLOYING

    def failed_spawn_cleanup(self, instance):
        """Cleanup from failed spawn.

        :param instance: Instance object
        """
        LOG.debug('Failed spawn cleanup called for instance',
                  instance=instance)
        self._cleanup_root_volume(instance)
        try:
            node = self._get_node(instance.node)
            if node.instance_id == instance.uuid:
                self._cleanup_deploy(node, instance)
        except exception.InstanceNotFound:
            LOG.warning('Attempt to clean-up from failed spawn of '
                       'instance %s failed due to node not found.',
                       instance.uuid)

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, allocations, network_info=None,
              block_device_info=None, power_on=True, accel_info=None):
        """Deploy an instance.

        :param context: The security context.
        :param instance: The instance object.
        :param image_meta: Image dict returned by nova.image.glance
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in instance.
        :param allocations: Information about resources allocated to the
                            instance via placement.
        :param network_info: Instance network information.
        :param block_device_info: Instance block device information.
        :param power_on: True if the instance should be powered on.
        :param accel_info: Accelerator requests for this instance.
        """
        LOG.debug('Spawn called for instance', instance=instance)

        node_id = instance.get('node')
        if not node_id:
            raise exception.NovaException(
                _("DPU node uuid not supplied to "
                  "driver for instance %s.") % instance.uuid
            )

        node = self._get_node(node_id)
        flavor = instance.flavor

        try:
            if not self._has_boot_volume(block_device_info):
                self._attach_root_volume_from_image(
                    context, instance, image_meta, block_device_info)

            # Add volumes using SPDK
            LOG.error(f"--------------------{block_device_info}")
            if block_device_info:
                self._add_volumes(context, instance, block_device_info)

            # Add network interfaces using SF
            if network_info:
                self._plug_vifs(context, node, instance, network_info)

            # Register RoCE allocation and create RoCE SF (non-fatal)
            self._setup_roce(instance, instance.flavor)

            # Create and attach config drive if required
            iso_path = None
            try:
                injection_info = InjectionInfo(
                    network_info=network_info,
                    files=injected_files,
                    admin_pass=admin_password)
                iso_path = self.configdrive_manager.create_configdrive(
                    context, instance, injection_info)
                if iso_path:
                    connection_info = self.configdrive_manager.build_connection_info(
                        iso_path, instance)
                    self.configdrive_manager.attach_configdrive(connection_info, instance)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    LOG.error("Failed to create/attach config drive: %s",
                              e, instance=instance)
                    # Cleanup ISO file if creation succeeded but attach failed
                    if iso_path and os.path.exists(iso_path):
                        try:
                            os.unlink(iso_path)
                        except Exception:
                            pass

            # Out-of-band reboot via IPMI after volumes/VIFs are ready
            ipmi_util.reboot_and_wait()

            # Update node state
            node.instance_id = instance.uuid
            node.provision_state = dpu_states.ACTIVE
            node.power_state = dpu_states.POWER_ON if power_on else dpu_states.POWER_OFF

            LOG.info('Successfully spawned instance %s on DPU node %s',
                     instance.uuid, node_id, instance=instance)

        except Exception as e:
            with excutils.save_and_reraise_exception():
                LOG.error("Error spawning instance %(instance)s on "
                         "DPU node %(node)s: %(reason)s",
                         {'instance': instance.uuid,
                          'node': node_id,
                          'reason': str(e)},
                         instance=instance)
                try:
                    self.configdrive_manager.cleanup_configdrive(instance)
                except Exception:
                    pass
                self._cleanup_root_volume(instance)
                self._cleanup_deploy(node, instance, network_info)

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, allocations, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                evacuate=False, block_device_info=None,
                preserve_ephemeral=False, accel_uuids=None,
                reimage_boot_volume=False):
        """Rebuild an instance on DPU.

        Align with Nova default rebuild sequence:
        detach block devices -> (destroy local resources) -> attach block
        devices -> spawn.
        """
        if preserve_ephemeral:
            raise exception.PreserveEphemeralNotSupported()

        LOG.info('Rebuild called for instance %(inst)s (evacuate=%(evacuate)s)',
                 {'inst': instance.uuid, 'evacuate': evacuate},
                 instance=instance)

        detach_root_bdm = not reimage_boot_volume
        if evacuate:
            detach_block_devices(context, bdms, detach_root_bdm=detach_root_bdm)
        else:
            detach_block_devices(context, bdms, detach_root_bdm=detach_root_bdm)
            if reimage_boot_volume:
                block_device_info_copy = copy.deepcopy(block_device_info)
                root_bdm = compute_utils.get_root_bdm(context, instance, bdms)
                if block_device_info_copy and root_bdm:
                    mapping = block_device_info_copy.get('block_device_mapping') or []
                    block_device_info_copy['block_device_mapping'] = [
                        bdm for bdm in mapping
                        if bdm.get('volume_id') != root_bdm.volume_id
                    ]
                self.destroy(context, instance,
                             network_info=network_info,
                             block_device_info=block_device_info_copy)
            else:
                self.destroy(context, instance,
                             network_info=network_info,
                             block_device_info=block_device_info)

            if reimage_boot_volume:
                is_volume_backed = compute_utils.is_volume_backed_instance(
                    context, instance, bdms)
                if is_volume_backed:
                    self._rebuild_volume_backed_instance(
                        context, instance, bdms, image_meta.id)

        instance.task_state = task_states.REBUILD_BLOCK_DEVICE_MAPPING
        instance.save(expected_task_state=[task_states.REBUILDING])

        new_block_device_info = attach_block_devices(context, instance, bdms)

        instance.task_state = task_states.REBUILD_SPAWNING
        instance.save(
            expected_task_state=[task_states.REBUILD_BLOCK_DEVICE_MAPPING])

        self.spawn(context, instance, image_meta, injected_files,
                   admin_password, allocations,
                   network_info=network_info,
                   block_device_info=new_block_device_info,
                   accel_info=accel_uuids)

    def _reimage_failed_callback(self, event_name, instance):
        msg = ('Cinder reported failure during reimaging '
               'with %(event)s for instance %(uuid)s')
        msg_args = {'event': event_name, 'uuid': instance.uuid}
        LOG.error(msg, msg_args, instance=instance)
        raise exception.ReimageException(msg % msg_args)

    def _rebuild_volume_backed_instance(self, context, instance, bdms, image_id):
        """Reimage root Cinder volume from image during rebuild."""
        root_bdm = compute_utils.get_root_bdm(context, instance, bdms)
        if not root_bdm:
            msg = _('Failed to rebuild volume backed instance.')
            raise exception.BuildAbortException(
                instance_uuid=instance.uuid, reason=msg)

        events = [('volume-reimaged', root_bdm.volume_id)]
        try:
            image = self.image_api.get(context, image_id)
        except exception.ImageNotFound:
            msg = _('Image %s not found.') % image_id
            LOG.error(msg, instance=instance)
            raise exception.BuildAbortException(
                instance_uuid=instance.uuid, reason=msg)

        image_size_gb = int(math.ceil(float(image.get('size')) / units.Gi))
        deadline = CONF.reimage_timeout_per_gb * image_size_gb
        error_cb = self._reimage_failed_callback
        try:
            from nova.volume import cinder
            with self.virtapi.wait_for_instance_event(
                    instance, events, deadline=deadline, error_callback=error_cb):
                cinder.API().reimage_volume(
                    context, root_bdm.volume_id, image_id,
                    reimage_reserved=True)
        except Exception as ex:
            LOG.error('Failed to rebuild volume backed instance: %s',
                      str(ex), instance=instance)
            msg = _('Failed to rebuild volume backed instance.')
            raise exception.BuildAbortException(
                instance_uuid=instance.uuid, reason=msg)

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, destroy_secrets=True):
        """Destroy the specified instance.

        :param context: Security context
        :param instance: Instance object
        :param network_info: Instance network information
        :param block_device_info: Information about block devices
        :param destroy_disks: Indicates if disks should be destroyed
        :param destroy_secrets: Indicates if secrets should be destroyed
        """
        LOG.debug('Destroy called for instance', instance=instance)

        node_id = instance.get('node')
        if not node_id:
            LOG.warning('No node ID found for instance %s', instance.uuid)
            return

        try:
            node = self._get_node(node_id)
        except exception.InstanceNotFound:
            LOG.warning('Node %s not found for instance %s',
                       node_id, instance.uuid)
            return

        try:
            # Cleanup volumes using SPDK
            # This will handle all volumes including root volume if it's a volume
            if block_device_info:
                self._remove_volumes(context, instance, block_device_info)

            # Only cleanup root volume if it's not a Cinder volume
            # (i.e., it was created from an image and stored in _ephemeral_root_volumes)
            if not self._has_boot_volume(block_device_info):
                self._cleanup_root_volume(instance, delete_volume=destroy_disks)

            # Cleanup network interfaces using SF
            if network_info:
                self._unplug_vifs(node, instance, network_info)

            # Release RoCE allocation and remove RoCE SF
            self._teardown_roce(instance)

            # Cleanup config drive
            self.configdrive_manager.cleanup_configdrive(instance)

            # Cleanup instance directory
            if destroy_disks:
                self._delete_instance_files(instance)

            # Cleanup any remaining RBD mapped devices
            # This ensures all RBD devices are unmapped, even if they weren't
            # properly tracked in connection_info
            try:
                self._cleanup_all_rbd_mappings(instance)
            except Exception as e:
                LOG.warning("Failed to cleanup RBD mappings: %s", e, instance=instance)

            # Update node state
            node.instance_id = None
            node.provision_state = dpu_states.AVAILABLE
            node.power_state = dpu_states.POWER_OFF

            LOG.info('Successfully destroyed instance %s on DPU node %s',
                     instance.uuid, node_id, instance=instance)

            # Trigger IPMI reset to reset the DPU node after instance destruction
            try:
                LOG.info('Triggering IPMI reset for DPU node %s after instance destruction',
                         node_id, instance=instance)
                ipmi_util.reboot_and_wait()
                LOG.info('Successfully reset DPU node %s via IPMI after instance destruction',
                         node_id, instance=instance)
            except Exception as e:
                LOG.warning('Failed to reset DPU node %s via IPMI after instance destruction: %s',
                           node_id, e, instance=instance)
                # Don't raise exception here, as instance destruction is already complete

        except Exception as e:
            LOG.error("Error destroying instance %(instance)s on "
                     "DPU node %(node)s: %(reason)s",
                     {'instance': instance.uuid,
                      'node': node_id,
                      'reason': str(e)},
                     instance=instance)

    def _wait_for_all_drivers_inactive(self, instance, max_wait_time=60, check_interval=2):
        """Wait for all NVMe controllers to have driver_active=False.
        
        :param instance: Instance object
        :param max_wait_time: Maximum time to wait in seconds
        :param check_interval: Interval between checks in seconds
        :returns: True if all drivers are inactive, False if timeout
        """
        LOG.info("Waiting for all NVMe controllers to have driver_active=False",
                 instance=instance)
        
        start_time = time.time()
        while time.time() - start_time < max_wait_time:
            try:
                controllers = snap_rpc.nvme_controller_list(
                    socket_path=self.spdk_client.endpoint)
                
                if not controllers:
                    LOG.info("No NVMe controllers found, assuming all drivers inactive",
                            instance=instance)
                    return True
                
                # Check if all controllers have driver_active=False
                all_inactive = True
                for ctrl in controllers:
                    driver_active = ctrl.get('driver_active', True)
                    ctrl_id = ctrl.get('ctrl_id', 'unknown')
                    if driver_active:
                        LOG.debug("Controller %s still has driver_active=True, waiting...",
                                 ctrl_id, instance=instance)
                        all_inactive = False
                        break
                
                if all_inactive:
                    LOG.info("All NVMe controllers have driver_active=False",
                            instance=instance)
                    return True
                
                time.sleep(check_interval)
            except Exception as e:
                LOG.warning("Error checking driver_active status: %s, retrying...",
                           e, instance=instance)
                time.sleep(check_interval)
        
        LOG.warning("Timeout waiting for all drivers to become inactive after %d seconds",
                   max_wait_time, instance=instance)
        return False

    def cleanup_local_resources(self, context, instance, network_info, 
                                block_device_info=None):
        """Cleanup local DPU resources (SPDK/SNAP and SF) without calling 
        volume API or neutron API.
        
        This method only cleans up local resources on the DPU:
        - First triggers IPMI reset
        - Waits for all NVMe controllers to have driver_active=False
        - Removes SPDK/SNAP namespaces (but keeps RBD volumes)
        - Removes SF interfaces (but keeps Neutron ports)
        - Cleans up config drive
        - Does NOT delete volumes via volume API
        - Does NOT delete ports via neutron API
        - Does NOT delete RBD volumes (even ephemeral root volumes)
        
        :param context: Security context
        :param instance: Instance object
        :param network_info: Instance network information
        :param block_device_info: Information about block devices
        """
        LOG.info('Cleaning up local DPU resources for instance %s',
                 instance.uuid, instance=instance)
        
        node_id = instance.get('node')
        if not node_id:
            LOG.warning('No node ID found for instance %s', instance.uuid)
            return
        
        try:
            node = self._get_node(node_id)
        except exception.InstanceNotFound:
            LOG.warning('Node %s not found for instance %s',
                       node_id, instance.uuid)
            return
        
        try:
            # Step 1: Trigger IPMI reset first
            try:
                LOG.info('Triggering IPMI reset for DPU node %s before cleanup',
                         node_id, instance=instance)
                ipmi_util.reboot_and_wait()
                LOG.info('Successfully reset DPU node %s via IPMI',
                         node_id, instance=instance)
            except Exception as e:
                LOG.warning('Failed to reset DPU node %s via IPMI: %s',
                           node_id, e, instance=instance)
                # Continue with cleanup even if IPMI reset fails
            
            # Step 2: Wait for all drivers to become inactive
            if not self._wait_for_all_drivers_inactive(instance):
                LOG.warning('Not all drivers became inactive, but proceeding with cleanup',
                           instance=instance)
            
            # Step 3: Cleanup SPDK/SNAP namespaces for all volumes (including root volume)
            # This only removes the SPDK namespace, not the RBD volume itself
            if block_device_info:
                bdms = virt_driver.block_device_info_get_mapping(block_device_info)
                for bdm in bdms:
                    if not bdm.is_volume:
                        continue
                    
                    try:
                        connection_info = jsonutils.loads(bdm._bdm_obj.connection_info)
                        mountpoint = bdm._bdm_obj.device_name
                        # Only remove SPDK namespace, not the volume
                        self.spdk_client.remove_volume(connection_info, instance, mountpoint)
                        LOG.debug("Removed SPDK namespace for volume %s",
                                 bdm._bdm_obj.volume_id, instance=instance)
                    except Exception as e:
                        LOG.warning("Failed to remove SPDK namespace for volume %(volume)s: %(reason)s",
                                   {'volume': bdm._bdm_obj.volume_id,
                                    'reason': str(e)},
                                   instance=instance)
            
            # Cleanup root volume SPDK namespace if it's an ephemeral root volume
            # This only removes the SPDK namespace, not the RBD volume
            if not self._has_boot_volume(block_device_info):
                root_info = self._ephemeral_root_volumes.get(instance.uuid)
                if root_info:
                    connection_info = root_info.get('connection_info')
                    mountpoint = root_info.get('mountpoint') or '/dev/vda'
                    if connection_info:
                        try:
                            # Only remove SPDK namespace, not the RBD volume
                            self.spdk_client.remove_volume(connection_info,
                                                           instance, mountpoint)
                            LOG.debug("Removed SPDK namespace for ephemeral root volume",
                                     instance=instance)
                        except Exception as e:
                            LOG.warning("Failed to remove SPDK namespace for root volume: %s",
                                       e, instance=instance)
            
            # Cleanup SF interfaces (but keep Neutron ports)
            if network_info:
                for vif in network_info:
                    try:
                        self.sf_client.remove_interface(vif, instance)
                        LOG.debug("Removed SF interface for VIF %s",
                                 vif.get('id', 'unknown'), instance=instance)
                    except Exception as e:
                        LOG.warning("Failed to remove SF interface for VIF %(vif)s: %(reason)s",
                                   {'vif': vif.get('id', 'unknown'),
                                    'reason': str(e)},
                                   instance=instance)
            
            # Cleanup RoCE SF（只删本地 SF，不调 deregister，reboot 期间 DB 分配记录保留）
            try:
                self.roce_sf_client.remove_roce_interface(instance.uuid)
                LOG.debug("Removed RoCE SF for instance %s", instance.uuid,
                          instance=instance)
            except Exception as e:
                LOG.warning("Failed to remove RoCE SF for instance %s: %s",
                            instance.uuid, e, instance=instance)

            # Cleanup config drive (detach from SPDK only; keep ISO for power_on)
            try:
                self.configdrive_manager.cleanup_configdrive(instance, delete_iso=False)
            except Exception as e:
                LOG.warning("Failed to cleanup config drive: %s", e, instance=instance)

            # Cleanup any remaining RBD mapped devices
            # This ensures all RBD devices are unmapped, even if they weren't
            # properly tracked in connection_info
            try:
                self._cleanup_all_rbd_mappings(instance)
            except Exception as e:
                LOG.warning("Failed to cleanup RBD mappings: %s", e, instance=instance)
            
            # Update node state
            node.instance_id = None
            node.provision_state = dpu_states.AVAILABLE
            node.power_state = dpu_states.POWER_OFF
            
            LOG.info('Successfully cleaned up local DPU resources for instance %s on node %s',
                     instance.uuid, node_id, instance=instance)
        
        except Exception as e:
            LOG.error("Error cleaning up local resources for instance %(instance)s on "
                     "DPU node %(node)s: %(reason)s",
                     {'instance': instance.uuid,
                      'node': node_id,
                      'reason': str(e)},
                     instance=instance)
            raise

    def restore_local_resources(self, context, instance, network_info,
                                block_device_info=None):
        """Restore local DPU resources (SPDK/SNAP and SF) without calling 
        volume API or neutron API.
        
        This method restores local resources on the DPU (inverse of cleanup_local_resources):
        - Restores SPDK/SNAP namespaces for all volumes (including root volume)
        - Restores SF interfaces
        - Does NOT create volumes via volume API (assumes volumes already exist)
        - Does NOT create ports via neutron API (assumes ports already exist)
        - Does NOT create RBD volumes (assumes they already exist)
        - Triggers IPMI reset at the end
        
        :param context: Security context
        :param instance: Instance object
        :param network_info: Instance network information
        :param block_device_info: Information about block devices
        """
        LOG.info('Restoring local DPU resources for instance %s',
                 instance.uuid, instance=instance)
        
        node_id = instance.get('node')
        if not node_id:
            LOG.warning('No node ID found for instance %s', instance.uuid)
            return
        
        try:
            node = self._get_node(node_id)
        except exception.InstanceNotFound:
            LOG.warning('Node %s not found for instance %s',
                       node_id, instance.uuid)
            return
        
        try:
            # Restore SPDK/SNAP namespaces for all volumes (including root volume)
            # This assumes volumes already exist and are mapped
            # Always query BDMs from database to ensure we don't miss any volumes
            try:
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                    context, instance.uuid)
            except Exception as e:
                LOG.warning("Failed to query BDMs from database: %s", e, instance=instance)
                bdms = []
            
            # Track processed volume IDs to avoid duplicates
            processed_volume_ids = set()
            
            # Process volumes from block_device_info first (if provided)
            if block_device_info:
                bdms_from_info = virt_driver.block_device_info_get_mapping(block_device_info)
                for bdm in bdms_from_info:
                    if not bdm.is_volume:
                        continue
                    
                    try:
                        # Get connection_info from BDM
                        connection_info = None
                        if bdm._bdm_obj.connection_info:
                            try:
                                connection_info = jsonutils.loads(bdm._bdm_obj.connection_info)
                            except Exception as e:
                                LOG.warning("Failed to parse connection_info for volume %s: %s",
                                           bdm._bdm_obj.volume_id, e, instance=instance)
                        
                        # If connection_info is missing, try to get it from Cinder
                        if not connection_info:
                            LOG.info("Connection info missing for volume %s, fetching from Cinder",
                                    bdm._bdm_obj.volume_id, instance=instance)
                            try:
                                connection_info = self._get_volume_connection_info(
                                    context, bdm._bdm_obj.volume_id)
                                # Save connection_info back to BDM
                                bdm._bdm_obj.connection_info = jsonutils.dumps(connection_info)
                                bdm._bdm_obj.save()
                            except Exception as e:
                                LOG.warning("Failed to get connection_info from Cinder for volume %s: %s",
                                           bdm._bdm_obj.volume_id, e, instance=instance)
                                continue
                        
                        mountpoint = bdm._bdm_obj.device_name
                        
                        # Ensure volume is connected (rbd map if needed)
                        connection_info = self._ensure_volume_connected(
                            connection_info, instance)
                        bdm._bdm_obj.connection_info = jsonutils.dumps(connection_info)
                        bdm._bdm_obj.save()
                        
                        # Always call add_volume to ensure SPDK namespace exists
                        # add_volume will check if bdev/namespace already exists and reuse it
                        self.spdk_client.add_volume(connection_info, instance, mountpoint)
                        LOG.info("Restored SPDK namespace for volume %s at %s",
                                bdm._bdm_obj.volume_id, mountpoint, instance=instance)
                        
                        processed_volume_ids.add(bdm._bdm_obj.volume_id)
                    except Exception as e:
                        LOG.warning("Failed to restore SPDK namespace for volume %(volume)s: %(reason)s",
                                   {'volume': bdm._bdm_obj.volume_id,
                                    'reason': str(e)},
                                   instance=instance)
                        # Continue with other volumes even if one fails
            
            # Also restore volumes that might not be in block_device_info
            # (e.g., if connection_info was missing and they were filtered out)
            # Process remaining volumes from database
            for bdm in bdms:
                if not bdm.is_volume:
                    continue
                
                # Skip if already processed above
                if bdm.volume_id in processed_volume_ids:
                    continue
                
                # This volume was not in block_device_info, try to restore it
                try:
                    connection_info = None
                    if bdm.connection_info:
                        try:
                            connection_info = jsonutils.loads(bdm.connection_info)
                        except Exception:
                            pass
                    
                    # If connection_info is missing, get it from Cinder
                    if not connection_info:
                        LOG.info("Connection info missing for volume %s (not in block_device_info), "
                                "fetching from Cinder", bdm.volume_id, instance=instance)
                        try:
                            connection_info = self._get_volume_connection_info(
                                context, bdm.volume_id)
                            bdm.connection_info = jsonutils.dumps(connection_info)
                            bdm.save()
                        except Exception as e:
                            LOG.warning("Failed to get connection_info from Cinder for volume %s: %s",
                                       bdm.volume_id, e, instance=instance)
                            continue
                    
                    mountpoint = bdm.device_name or '/dev/vdb'
                    
                    # Ensure volume is connected (rbd map if needed)
                    connection_info = self._ensure_volume_connected(
                        connection_info, instance)
                    bdm.connection_info = jsonutils.dumps(connection_info)
                    bdm.save()
                    
                    # Always call add_volume to ensure SPDK namespace exists
                    # add_volume will check if bdev/namespace already exists and reuse it
                    self.spdk_client.add_volume(connection_info, instance, mountpoint)
                    LOG.info("Restored SPDK namespace for volume %s at %s (recovered from DB)",
                            bdm.volume_id, mountpoint, instance=instance)
                except Exception as e:
                    LOG.warning("Failed to restore SPDK namespace for volume %(volume)s "
                               "(recovered from DB): %(reason)s",
                               {'volume': bdm.volume_id, 'reason': str(e)},
                               instance=instance)
            
            # Restore root volume SPDK namespace if it's an ephemeral root volume
            # This assumes the RBD volume already exists
            if not self._has_boot_volume(block_device_info):
                root_info = self._ephemeral_root_volumes.get(instance.uuid)
                if root_info:
                    connection_info = root_info.get('connection_info')
                    mountpoint = root_info.get('mountpoint') or '/dev/vda'
                    if connection_info:
                        try:
                            # Ensure root volume is connected (rbd map if needed)
                            connection_info = self._ensure_volume_connected(
                                connection_info, instance)
                            root_info['connection_info'] = connection_info
                            
                            # Add SPDK namespace
                            self.spdk_client.add_volume(connection_info, instance, mountpoint)
                            LOG.debug("Restored SPDK namespace for ephemeral root volume",
                                     instance=instance)
                        except Exception as e:
                            LOG.warning("Failed to restore SPDK namespace for root volume: %s",
                                       e, instance=instance)
                else:
                    # Cache may be missing after service restart.
                    # Do NOT require an existing SPDK bdev here because cleanup
                    # path removes root SPDK bdev/namespace on power-off.
                    # Rebuild root connection_info from deterministic root name
                    # and let add_volume recreate bdev/namespace.
                    LOG.info("Ephemeral root volume cache missing for instance %s, "
                            "rebuilding root connection_info", instance.uuid)
                    volume_name = self._build_root_volume_name(instance)
                    pool = CONF.dpu.images_rbd_pool or CONF.libvirt.images_rbd_pool
                    if pool:
                        try:
                            if not self._root_rbd_image_exists(pool, volume_name, instance):
                                LOG.warning(
                                    "Ephemeral root image %(pool)s/%(vol)s does not exist; "
                                    "skip root attach during restore",
                                    {'pool': pool, 'vol': volume_name},
                                    instance=instance)
                            else:
                                root_data = {
                                    'volume_id': volume_name,
                                    'name': f"{pool}/{volume_name}",
                                    'cluster_name': self._get_ceph_cluster_name(),
                                    'auth_enabled': bool(CONF.libvirt.rbd_user),
                                    'auth_username': CONF.libvirt.rbd_user,
                                    'secret_type': 'ceph',
                                    'serial': volume_name,
                                }
                                keyring = self._find_ceph_keyring(root_data)
                                if keyring:
                                    root_data['keyring'] = keyring
                                root_info = {
                                    'volume': volume_name,
                                    'pool': pool,
                                    'mountpoint': self._get_root_mountpoint(instance, block_device_info),
                                    'connection_info': {
                                        'driver_volume_type': 'rbd',
                                        'data': root_data,
                                    },
                                }
                                # Restore to cache
                                self._ephemeral_root_volumes[instance.uuid] = root_info

                                # Ensure volume is connected and add SPDK namespace
                                connection_info = self._ensure_volume_connected(
                                    root_info['connection_info'], instance)
                                root_info['connection_info'] = connection_info
                                self.spdk_client.add_volume(connection_info, instance,
                                                           root_info['mountpoint'])
                                LOG.info("Rebuilt and restored ephemeral root volume for instance %s",
                                         instance.uuid, instance=instance)
                        except Exception as exc:
                            LOG.warning("Failed to rebuild root volume for instance %s: %s",
                                       instance.uuid,
                                       exc, instance=instance)
            
            # Restore SF interfaces (assumes Neutron ports already exist)
            if network_info:
                for vif in network_info:
                    try:
                        self.sf_client.add_interface(context, vif, instance)
                        LOG.debug("Restored SF interface for VIF %s",
                                 vif.get('id', 'unknown'), instance=instance)
                    except Exception as e:
                        LOG.warning("Failed to restore SF interface for VIF %(vif)s: %(reason)s",
                                   {'vif': vif.get('id', 'unknown'),
                                    'reason': str(e)},
                                   instance=instance)
                        # Continue with other interfaces even if one fails
            
            # Restore RoCE SF（register 幂等，返回已有分配；重建 SF 时应用新的 max_io_eqs）
            try:
                self._setup_roce(instance, instance.flavor)
            except Exception as e:
                LOG.warning("Failed to restore RoCE SF for instance %s: %s",
                            instance.uuid, e, instance=instance)

            # Restore configdrive if it exists
            try:
                if configdrive.required_by(instance):
                    from nova.virt.libvirt import utils as libvirt_utils
                    instance_dir = libvirt_utils.get_instance_path(instance)
                    iso_path = os.path.join(instance_dir, 'disk.config')
                    
                    if os.path.exists(iso_path):
                        LOG.info('Restoring config drive for instance %s from %s',
                                instance.uuid, iso_path, instance=instance)
                        connection_info = self.configdrive_manager.build_connection_info(
                            iso_path, instance)
                        self.configdrive_manager.attach_configdrive(connection_info, instance)
                        LOG.info('Successfully restored config drive for instance %s',
                                instance.uuid, instance=instance)
                    else:
                        LOG.debug('Config drive ISO not found at %s for instance %s, skipping',
                                 iso_path, instance.uuid, instance=instance)
            except Exception as e:
                LOG.warning('Failed to restore config drive for instance %s: %s',
                           instance.uuid, e, instance=instance)
                # Don't fail the entire restore process if configdrive restore fails
            
            # Update node state
            node.instance_id = instance.uuid
            node.provision_state = dpu_states.ACTIVE
            node.power_state = dpu_states.POWER_ON
            
            LOG.info('Successfully restored local DPU resources for instance %s on node %s',
                     instance.uuid, node_id, instance=instance)
            
            # Trigger IPMI reset to reset the DPU node after restoration
            try:
                LOG.info('Triggering IPMI reset for DPU node %s after local resource restoration',
                         node_id, instance=instance)
                ipmi_util.reboot_and_wait()
                LOG.info('Successfully reset DPU node %s via IPMI after local resource restoration',
                         node_id, instance=instance)
            except Exception as e:
                LOG.warning('Failed to reset DPU node %s via IPMI after local resource restoration: %s',
                           node_id, e, instance=instance)
                # Don't raise exception here, as restoration is already complete
        
        except Exception as e:
            LOG.error("Error restoring local resources for instance %(instance)s on "
                     "DPU node %(node)s: %(reason)s",
                     {'instance': instance.uuid,
                      'node': node_id,
                      'reason': str(e)},
                     instance=instance)
            raise

    def _delete_instance_files(self, instance):
        """Delete instance directory and all files within it.
        
        :param instance: Instance object
        :returns: True if successful, False otherwise
        """
        from nova.virt.libvirt import utils as libvirt_utils
        
        instance_dir = libvirt_utils.get_instance_path(instance)
        
        if not os.path.exists(instance_dir):
            LOG.debug("Instance directory %(dir)s does not exist, skipping deletion",
                     {'dir': instance_dir}, instance=instance)
            return True
        
        try:
            LOG.info('Deleting instance directory %(dir)s',
                    {'dir': instance_dir}, instance=instance)
            shutil.rmtree(instance_dir)
            LOG.info('Successfully deleted instance directory %(dir)s',
                    {'dir': instance_dir}, instance=instance)
            return True
        except OSError as e:
            LOG.error('Failed to delete instance directory %(dir)s: %(err)s',
                     {'dir': instance_dir, 'err': e}, instance=instance)
            return False
        except Exception as e:
            LOG.error('Unexpected error deleting instance directory %(dir)s: %(err)s',
                     {'dir': instance_dir, 'err': e}, instance=instance)
            return False

    def _setup_roce(self, instance, flavor):
        """Register RoCE allocation and create RoCE SF. Non-fatal."""
        try:
            result = self.roce_client.register(
                instance_uuid=instance.uuid,
                flavor_name=flavor.name if flavor else "",
                project_id=instance.project_id or "",
                node_host=CONF.host,
                node_type="dpu",
            )
        except Exception as exc:
            LOG.warning("RoCE register failed  instance=%s: %s",
                        instance.uuid, exc, instance=instance)
            return

        if not result.get("roce_enabled"):
            LOG.debug("RoCE not enabled for instance %s",
                      instance.uuid, instance=instance)
            return

        mac = result["mac"]
        vlan_id = result.get("vlan_id", 0)
        subnet = result.get("subnet", "")
        extra_routes = result.get("extra_routes", "")
        try:
            representor = self.roce_sf_client.add_roce_interface(
                instance_uuid=instance.uuid,
                mac_address=mac,
                vlan_id=vlan_id,
                mtu=CONF.dpu.roce_mtu,
            )
            LOG.info("RoCE ready  instance=%s  mac=%s  vlan=%s  repr=%s",
                     instance.uuid, mac, vlan_id, representor, instance=instance)
        except Exception as exc:
            LOG.error("RoCE SF creation failed  instance=%s: %s",
                      instance.uuid, exc, instance=instance)
            self.roce_client.deregister(instance.uuid)

    def _teardown_roce(self, instance, caller_host=None):
        """Remove RoCE SF and release DB allocation. Best-effort.

        caller_host: 显式指定调用方节点，冷迁移时传入本节点标识，
        sidecar 发现 DB 已属于目标节点则跳过 DELETE，避免误删迁移后记录。
        SF 清理是本地操作，无论是否迁移都必须执行。
        """
        try:
            self.roce_sf_client.remove_roce_interface(instance.uuid)
        except Exception as exc:
            LOG.warning("RoCE SF removal failed  instance=%s: %s",
                        instance.uuid, exc, instance=instance)
        try:
            self.roce_client.deregister(instance.uuid,
                                        caller_host=caller_host or CONF.host)
        except Exception as exc:
            LOG.warning("RoCE deregister failed  instance=%s: %s",
                        instance.uuid, exc, instance=instance)

    def _cleanup_deploy(self, node, instance, network_info=None):
        """Cleanup deployment resources.

        :param node: DPUNode object
        :param instance: Instance object
        :param network_info: Network information
        """
        try:
            if network_info:
                self._unplug_vifs(node, instance, network_info)
        except Exception as e:
            LOG.warning("Error cleaning up network interfaces: %s", e)

        # Cleanup RoCE SF and DB allocation
        try:
            self._teardown_roce(instance)
        except Exception as e:
            LOG.warning("Error tearing down RoCE: %s", e)

        self._cleanup_root_volume(instance)
        node.instance_id = None
        node.provision_state = dpu_states.AVAILABLE
        node.power_state = dpu_states.POWER_OFF

    def _add_volumes(self, context, instance, block_device_info):
        """Add volumes to instance using SPDK.

        :param context: Security context
        :param instance: Instance object
        :param block_device_info: Block device information
        """
        bdms = virt_driver.block_device_info_get_mapping(block_device_info)
        
        for bdm in bdms:
            if not bdm.is_volume:
                continue

            connection_info = jsonutils.loads(bdm._bdm_obj.connection_info)
            mountpoint = bdm._bdm_obj.device_name
            
            try:
                connection_info = self._ensure_volume_connected(
                    connection_info, instance)
                bdm._bdm_obj.connection_info = jsonutils.dumps(connection_info)
                self.spdk_client.add_volume(connection_info, instance, mountpoint)
            except Exception as e:
                LOG.error("Failed to add volume %(volume)s to instance "
                         "%(instance)s: %(reason)s",
                         {'volume': bdm._bdm_obj.volume_id,
                          'instance': instance.uuid,
                          'reason': str(e)},
                         instance=instance)
                raise

    def _remove_volumes(self, context, instance, block_device_info):
        """Remove volumes from instance using SPDK.

        :param context: Security context
        :param instance: Instance object
        :param block_device_info: Block device information
        """
        bdms = virt_driver.block_device_info_get_mapping(block_device_info)
        
        for bdm in bdms:
            if not bdm.is_volume:
                continue

            connection_info = jsonutils.loads(bdm._bdm_obj.connection_info)
            mountpoint = bdm._bdm_obj.device_name
            
            try:
                self.spdk_client.remove_volume(connection_info, instance, mountpoint)
            except Exception as e:
                LOG.warning("Failed to remove volume %(volume)s from instance "
                           "%(instance)s: %(reason)s",
                           {'volume': bdm._bdm_obj.volume_id,
                            'instance': instance.uuid,
                            'reason': str(e)},
                           instance=instance)
            finally:
                try:
                    self._disconnect_volume(connection_info, instance)
                except Exception as e:
                    LOG.warning("Failed to disconnect volume %(volume)s after "
                                "SPDK removal for instance %(instance)s: %(reason)s",
                                {'volume': bdm._bdm_obj.volume_id,
                                 'instance': instance.uuid,
                                 'reason': str(e)},
                                instance=instance)
                else:
                    bdm._bdm_obj.connection_info = jsonutils.dumps(connection_info)

    def _has_boot_volume(self, block_device_info):
        """Return True if the instance is booting from a Cinder volume."""
        if not block_device_info:
            return False

        root_device = virt_driver.block_device_info_get_root_device(
            block_device_info)
        if root_device and block_device.volume_in_mapping(
                root_device, block_device_info):
            return True

        bdms = virt_driver.block_device_info_get_mapping(block_device_info)
        for bdm in bdms:
            if getattr(bdm, 'is_volume', False) and bdm.is_volume:
                boot_index = bdm.get('boot_index')
                if boot_index in (0, '0'):
                    return True
        return False

    def _get_root_mountpoint(self, instance, block_device_info):
        root_device = (
            virt_driver.block_device_info_get_root_device(block_device_info) or
            instance.root_device_name or '/dev/vda')
        if not root_device.startswith('/dev'):
            root_device = '/dev/%s' % root_device
        return root_device

    def _build_root_volume_name(self, instance):
        return f"{instance.uuid}_disk"

    def _root_rbd_image_exists(self, pool, volume_name, instance):
        """Check whether ephemeral root RBD image exists in Ceph."""
        try:
            driver = rbd_utils.RBDDriver(pool=pool)
            return bool(driver.exists(volume_name))
        except Exception as exc:
            LOG.warning("Failed to check root RBD image %(pool)s/%(vol)s: %(err)s",
                        {'pool': pool, 'vol': volume_name, 'err': exc},
                        instance=instance)
            return False

    def _build_rbd_connection_info(self, driver, volume_name):
        data = {
            'volume_id': volume_name,
            'name': f"{driver.pool}/{volume_name}",
            'cluster_name': self._get_ceph_cluster_name(),
            'hosts': [],
            'ports': [],
            'auth_enabled': bool(driver.rbd_user),
            'auth_username': driver.rbd_user,
            'secret_type': 'ceph',
            'serial': volume_name,
        }

        try:
            hosts, ports = driver.get_mon_addrs()
            data['hosts'] = hosts or []
            data['ports'] = ports or []
        except Exception as exc:
            LOG.warning("Failed to retrieve Ceph monitor addresses: %s", exc)

        secret_uuid = getattr(CONF.libvirt, 'rbd_secret_uuid', None)
        if secret_uuid:
            data['secret_uuid'] = secret_uuid

        keyring = self._find_ceph_keyring(data)
        if keyring:
            data['keyring'] = keyring

        return {
            'driver_volume_type': 'rbd',
            'data': data,
            'serial': volume_name,
        }

    def _attach_root_volume_from_image(self, context, instance, image_meta,
                                       block_device_info):
        image_dict = {}
        image_id = instance.image_ref

        if isinstance(image_meta, objects.ImageMeta):
            image_id = image_meta.id or image_id
        elif isinstance(image_meta, dict):
            image_dict = dict(image_meta)
            image_id = image_dict.get('id', image_id)

        if not image_dict.get('locations'):
            image_dict = self.image_api.show(
                context, image_id, include_locations=True)

        LOG.debug('Image locations are: %(locs)s', {'locs': image_dict.get('locations')},
                  instance=instance)

        # libvirt-style fast path: only raw/iso are acceptable for clone
        if image_dict.get('disk_format') not in ['raw', 'iso']:
            raise exception.ImageUnacceptable(
                image_id=image_id,
                reason=_('Image is not raw or iso format'))

        # Use DPU-specific pool if configured, otherwise fall back to libvirt pool
        pool = CONF.dpu.images_rbd_pool or CONF.libvirt.images_rbd_pool
        if not pool:
            raise exception.NovaException(
                _("Either [dpu]/images_rbd_pool or [libvirt]/images_rbd_pool "
                  "must be configured for image-based booting"))
        
        driver = rbd_utils.RBDDriver(pool=pool)
        volume_name = self._build_root_volume_name(instance)

        if driver.exists(volume_name):
            LOG.info("Deleting existing temporary RBD image %s before rebuild",
                     volume_name, instance=instance)
            driver.destroy_volume(volume_name)

        # Try to clone from locations (libvirt-style fast path)
        cloned = False
        locations = image_dict.get('locations', [])
        for location in locations:
            if not location:
                continue
            # Check if location is cloneable (same cluster, raw format, accessible)
            if driver.is_cloneable(location, image_dict):
                try:
                    LOG.info("Cloning Glance image %(image)s to RBD pool %(pool)s/%(dest)s from location %(loc)s",
                             {'image': image_id, 'pool': pool, 'dest': volume_name,
                              'loc': location.get('url')},
                             instance=instance)
                    driver.clone(location, volume_name, dest_pool=driver.pool)
                    cloned = True
                    break
                except Exception as exc:
                    LOG.warning("Clone failed for location %(loc)s: %(err)s",
                                {'loc': location.get('url'), 'err': exc},
                                instance=instance)
                    continue

        # Fallback to download and import (libvirt-style fallback)
        if not cloned:
            LOG.info("No cloneable locations found for image %(image)s, "
                     "falling back to download and import",
                     {'image': image_id}, instance=instance)
            # Download image to temporary file
            with tempfile.NamedTemporaryFile(delete=False, prefix='dpu_image_') as tmp_file:
                tmp_path = tmp_file.name
            try:
                LOG.info("Downloading image %(image)s to temporary file %(path)s",
                         {'image': image_id, 'path': tmp_path}, instance=instance)
                images.fetch_to_raw(context, image_id, tmp_path)
                # Import downloaded image to RBD
                LOG.info("Importing image %(image)s from %(path)s to RBD pool %(pool)s/%(dest)s",
                         {'image': image_id, 'path': tmp_path, 'pool': pool,
                          'dest': volume_name}, instance=instance)
                driver.import_image(tmp_path, volume_name)
            finally:
                # Clean up temporary file
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception as exc:
                    LOG.warning("Failed to remove temporary file %(path)s: %(err)s",
                                {'path': tmp_path, 'err': exc}, instance=instance)

        # Keep original image size, do not resize based on flavor
        connection_info = self._build_rbd_connection_info(driver, volume_name)
        try:
            connection_info['data']['volume_size_bytes'] = int(
                driver.size(volume_name))
        except Exception as exc:
            LOG.warning(
                "Could not read RBD image size for SPDK bdev_rbd_resize: "
                "pool=%(pool)s vol=%(vol)s: %(err)s",
                {'pool': driver.pool, 'vol': volume_name, 'err': exc},
                instance=instance)
        mountpoint = self._get_root_mountpoint(instance, block_device_info)

        try:
            connection_info = self._ensure_volume_connected(
                connection_info, instance)
            self.spdk_client.add_volume(connection_info, instance, mountpoint)
        except Exception:
            with excutils.save_and_reraise_exception():
                try:
                    self._disconnect_volume(connection_info, instance,
                                            force=True)
                except Exception as exc:
                    LOG.warning("Failed to disconnect temporary root volume "
                                "%(vol)s: %(err)s",
                                {'vol': volume_name, 'err': exc},
                                instance=instance)
                self._delete_rbd_volume(driver.pool, volume_name)

        self._ephemeral_root_volumes[instance.uuid] = {
            'pool': driver.pool,
            'volume': volume_name,
            'connection_info': deepcopy(connection_info),
            'mountpoint': mountpoint,
            'image_id': image_id,
        }
        LOG.info("Attached image-based root volume %(vol)s for instance",
                 {'vol': volume_name}, instance=instance)

    def _cleanup_root_volume(self, instance, delete_volume=True):
        """Cleanup root volume (SPDK + RBD)."""
        root_info = self._ephemeral_root_volumes.pop(instance.uuid, None)

        # If cache is missing (e.g. after restart), rebuild a minimal root_info
        if not root_info:
            volume_name = self._build_root_volume_name(instance)
            pool = CONF.dpu.images_rbd_pool or CONF.libvirt.images_rbd_pool
            if not pool:
                LOG.warning("No pool configured; cannot cleanup root volume %s",
                            volume_name, instance=instance)
                return

            bdev_name = self.spdk_client._sanitize_bdev_name(volume_name)
            root_info = {
                'volume': volume_name,
                'pool': pool,
                'mountpoint': '/dev/vda',
                'connection_info': {
                    'driver_volume_type': 'rbd',
                    'data': {
                        'volume_id': volume_name,
                        'name': f"{pool}/{volume_name}",
                        'cluster_name': self._get_ceph_cluster_name(),
                        # nsid may be unknown after restart; bdev_name is enough to try cleanup
                        'spdk': {'bdev_name': bdev_name},
                    },
                },
            }

        connection_info = root_info.get('connection_info')
        mountpoint = root_info.get('mountpoint') or '/dev/vda'

        if connection_info:
            try:
                self.spdk_client.remove_volume(connection_info,
                                               instance, mountpoint)
            except Exception as exc:
                LOG.warning("Failed to remove SPDK namespace for root volume: %s",
                            exc, instance=instance)
            try:
                self._disconnect_volume(connection_info, instance, force=True)
            except Exception as exc:
                LOG.warning("Failed to disconnect root volume: %s", exc,
                            instance=instance)

        if delete_volume:
            volume = root_info.get('volume')
            pool = root_info.get('pool')
            if volume and pool:
                try:
                    driver = rbd_utils.RBDDriver(pool=pool)
                    driver.destroy_volume(volume, pool=pool)
                    LOG.debug("Destroyed temporary RBD volume %(pool)s/%(vol)s",
                              {'pool': pool, 'vol': volume}, instance=instance)
                except Exception as exc:
                    LOG.warning("Failed to destroy RBD volume %(pool)s/%(vol)s: "
                                "%(err)s",
                                {'pool': pool, 'vol': volume, 'err': exc},
                                instance=instance)

    def _delete_rbd_volume(self, pool, volume):
        if not volume:
            return
        try:
            driver = rbd_utils.RBDDriver(pool=pool)
            driver.destroy_volume(volume, pool=pool)
        except Exception as exc:
            LOG.warning("Failed to delete RBD volume %(pool)s/%(vol)s: %(err)s",
                        {'pool': pool, 'vol': volume, 'err': exc})

    def _get_ceph_cluster_name(self):
        return CONF.dpu.ceph_cluster_name or 'ceph'

    def _expand_partition_after_map(self, device_path, instance):
        """Expand partition after RBD device is mapped.
        
        This is done immediately after rbd map to ensure partition table is updated.
        The operation tries partition numbers 1, 2, 3... until one succeeds.
        The operation is non-blocking - failures are logged but do not interrupt
        the volume attachment process.
        
        :param device_path: Path to the mapped RBD device (e.g., /dev/rbd0)
        :param instance: Instance object for logging
        """
        # 尝试分区号 1-10，通常最后一个分区是需要扩展的
        max_partition = 10
        success = False
        
        for partition_num in range(1, max_partition + 1):
            try:
                growpart_cmd = ['growpart', device_path, str(partition_num)]
                LOG.info("Executing growpart command: %(cmd)s",
                        {'cmd': ' '.join(growpart_cmd)}, instance=instance)
                processutils.execute(*growpart_cmd, run_as_root=True, root_helper='sudo')
                LOG.info("Successfully executed growpart on %(device)s partition %(part)d",
                        {'device': device_path, 'part': partition_num}, instance=instance)
                success = True
                break
            except processutils.ProcessExecutionError as e:
                # 如果分区不存在或已经最大，继续尝试下一个分区
                # growpart 返回非 0 退出码表示失败，可能是分区不存在
                LOG.debug("growpart failed for partition %(part)d on %(device)s: %(error)s, trying next",
                         {'part': partition_num, 'device': device_path, 'error': e},
                         instance=instance)
                continue
            except Exception as e:
                # 其他错误也继续尝试
                LOG.debug("Unexpected error during growpart for partition %(part)d on %(device)s: %(error)s, trying next",
                         {'part': partition_num, 'device': device_path, 'error': e},
                         instance=instance)
                continue
        
        if not success:
            LOG.warning("growpart failed for all partitions (1-%(max)d) on %(device)s, continuing",
                       {'max': max_partition, 'device': device_path}, instance=instance)

    def _ensure_volume_connected(self, connection_info, instance):
        """Ensure a volume is locally attached via rbd map before SPDK use."""
        driver_type = connection_info.get('driver_volume_type')
        if driver_type != 'rbd':
            return connection_info

        if vol_policy.resolve_volume_spdk_backend(
                connection_info, configdrive=False) == vol_policy.BACKEND_RBD:
            LOG.info(
                "Skipping kernel rbd map; SPDK will use bdev_rbd for volume %(vol)s",
                {'vol': (connection_info.get('data') or {}).get('volume_id')},
                instance=instance)
            return connection_info

        original_data = connection_info.get('data') or {}
        device_path = original_data.get('device_path')

        if device_path and os.path.exists(device_path):
            return connection_info

        # Extract RBD name (format: pool/image)
        rbd_name = original_data.get('name')
        if not rbd_name:
            msg = _("RBD name not found in connection data")
            raise exception.NovaException(msg)
        
        # Parse pool and image from name
        if '/' in rbd_name:
            pool, image = rbd_name.split('/', 1)
        else:
            msg = _("Invalid RBD name format: %s") % rbd_name
            raise exception.NovaException(msg)
        
        # Build rbd map command
        cmd = ['rbd', 'map', image, '--pool', pool]
        
        # Add cluster name if specified
        cluster_name = original_data.get('cluster_name')
        if cluster_name and cluster_name != 'ceph':
            cmd.extend(['--cluster', cluster_name])
        
        # Add authentication if enabled
        auth_enabled = original_data.get('auth_enabled', False)
        if auth_enabled:
            auth_username = original_data.get('auth_username')
            if not auth_username:
                msg = _("auth_username not found but auth_enabled is True")
                raise exception.NovaException(msg)
            
            # Add --name option (format: client.username)
            cmd.extend(['--name', f'client.{auth_username}'])
            
            # Add keyring if available
            keyring = original_data.get('keyring')
            if not keyring:
                keyring = self._find_ceph_keyring(original_data)
            
            if keyring:
                cmd.extend(['--keyring', keyring])
            else:
                LOG.warning("No keyring file found for RBD authentication",
                           instance=instance)
        
        LOG.info("Mapping RBD volume %(name)s to local device",
                {'name': rbd_name}, instance=instance)
        LOG.info("Executing rbd map command: %(cmd)s",
                {'cmd': ' '.join(cmd)}, instance=instance)
        
        try:
            # Execute rbd map command
            # rbd map output format: "/dev/rbd0" or "/dev/rbd/pool/image"
            out, err = processutils.execute(*cmd, run_as_root=True, root_helper='sudo')
            
            # Print shell execution results for debugging
            LOG.info("rbd map stdout: %(out)s", {'out': out}, instance=instance)
            LOG.info("rbd map stderr: %(err)s", {'err': err}, instance=instance)
            
            # Parse device path from output
            device_path = out.strip() if out and out.strip() else None
            LOG.info("Parsed device_path from stdout: %(path)s",
                    {'path': device_path}, instance=instance)
            
            if not device_path:
                # Try to find the device by checking /dev/rbd/pool/image first
                rbd_dev_path = f'/dev/rbd/{pool}/{image}'
                LOG.info("Checking for device at %(path)s",
                        {'path': rbd_dev_path}, instance=instance)
                if os.path.exists(rbd_dev_path):
                    device_path = rbd_dev_path
                    LOG.info("Found device at %(path)s",
                            {'path': device_path}, instance=instance)
                else:
                    # Try to find the latest /dev/rbdX device
                    import glob
                    rbd_devs = sorted(glob.glob('/dev/rbd*'), reverse=True)
                    LOG.info("Found RBD devices: %(devs)s",
                            {'devs': rbd_devs}, instance=instance)
                    if rbd_devs:
                        device_path = rbd_devs[0]
                        LOG.info("Using latest RBD device: %(device)s",
                                {'device': device_path}, instance=instance)
                    else:
                        msg = _("Failed to find mapped RBD device for %s") % rbd_name
                        raise exception.NovaException(msg)
        except processutils.ProcessExecutionError as e:
            # If device is already mapped, rbd map may fail
            # Check if the device already exists
            LOG.warning("rbd map command failed: %(error)s, checking for existing device",
                       {'error': e}, instance=instance)
            LOG.warning("rbd map stdout: %(out)s", {'out': e.stdout}, instance=instance)
            LOG.warning("rbd map stderr: %(err)s", {'err': e.stderr}, instance=instance)
            
            # Try to find the device
            rbd_dev_path = f'/dev/rbd/{pool}/{image}'
            if os.path.exists(rbd_dev_path):
                device_path = rbd_dev_path
            else:
                # Try to find any /dev/rbd* device
                import glob
                rbd_devs = sorted(glob.glob('/dev/rbd*'), reverse=True)
                if rbd_devs:
                    device_path = rbd_devs[0]
                    LOG.info("Using existing RBD device: %(device)s",
                            {'device': device_path}, instance=instance)
                else:
                    msg = _("Failed to map RBD volume %s: %s") % (rbd_name, e)
                    raise exception.NovaException(msg)
        
        # If device_path is a partition (e.g., /dev/rbd4p3), extract the base device (e.g., /dev/rbd4)
        # RBD devices should be whole devices, not partitions
        if device_path and 'p' in os.path.basename(device_path):
            # Extract base device (e.g., /dev/rbd4 from /dev/rbd4p3)
            base_match = re.match(r'^(/dev/rbd\d+)', device_path)
            if base_match:
                base_device = base_match.group(1)
                if os.path.exists(base_device):
                    LOG.info("Extracted base device %(base)s from partition %(part)s",
                            {'base': base_device, 'part': device_path}, instance=instance)
                    device_path = base_device
                else:
                    LOG.warning("Base device %(base)s does not exist, using partition %(part)s",
                               {'base': base_device, 'part': device_path}, instance=instance)
        
        # Verify device_path exists
        if not device_path or not os.path.exists(device_path):
            msg = _("Mapped device path %s does not exist for volume %s") % (
                device_path, original_data.get('volume_id', 'unknown'))
            raise exception.NovaException(msg)
        
        LOG.info("RBD volume mapped to device %(device)s",
                {'device': device_path}, instance=instance)
        
        # Expand partition 1 after mapping
        self._expand_partition_after_map(device_path, instance)
        
        # Update connection_info with device_path
        original_data['device_path'] = device_path
        connection_info['data'] = original_data
        
        return connection_info

    def _disconnect_volume(self, connection_info, instance, force=False):
        """Disconnect a previously attached volume."""
        driver_type = connection_info.get('driver_volume_type')
        if driver_type != 'rbd':
            return

        if vol_policy.resolve_volume_spdk_backend(
                connection_info, configdrive=False) == vol_policy.BACKEND_RBD:
            LOG.debug(
                "Skipping kernel rbd unmap; volume was attached via SPDK bdev_rbd",
                instance=instance)
            return

        data = connection_info.get('data') or {}
        
        # 优先使用 device_path 来 unmap（更准确，避免使用错误的 RBD 名称）
        device_path = data.get('device_path')
        if device_path and os.path.exists(device_path):
            # 使用设备路径 unmap
            cmd = ['rbd', 'unmap', device_path]
            LOG.info("Unmapping RBD device %(device)s",
                    {'device': device_path}, instance=instance)
        else:
            # 回退到使用 RBD 名称 unmap
            rbd_name = data.get('name')
            if not rbd_name:
                LOG.warning("Neither device_path nor RBD name found in connection data, cannot unmap",
                           instance=instance)
                return
            
            # Parse pool and image from name
            if '/' in rbd_name:
                pool, image = rbd_name.split('/', 1)
            else:
                LOG.warning("Invalid RBD name format: %s, cannot unmap", rbd_name,
                           instance=instance)
                return
            
            # Build rbd unmap command using RBD name
            cmd = ['rbd', 'unmap', image, '--pool', pool]
            
            # Add cluster name if specified
            cluster_name = data.get('cluster_name')
            if cluster_name and cluster_name != 'ceph':
                cmd.extend(['--cluster', cluster_name])
            
            # Add authentication if enabled
            auth_enabled = data.get('auth_enabled', False)
            if auth_enabled:
                auth_username = data.get('auth_username')
                if auth_username:
                    cmd.extend(['--name', f'client.{auth_username}'])
                
                # Add keyring if available
                keyring = data.get('keyring')
                if not keyring:
                    keyring = self._find_ceph_keyring(data)
                
                if keyring:
                    cmd.extend(['--keyring', keyring])
            
            LOG.info("Unmapping RBD volume %(name)s (device_path not available)",
                    {'name': rbd_name}, instance=instance)
        
        try:
            processutils.execute(*cmd, run_as_root=True, root_helper='sudo')
            LOG.info("RBD volume unmapped successfully", instance=instance)
        except processutils.ProcessExecutionError as e:
            # If device is already unmapped, rbd unmap will fail
            if 'not mapped' in str(e).lower() or 'not found' in str(e).lower():
                LOG.debug("RBD volume already unmapped", instance=instance)
            else:
                LOG.warning("Error unmapping RBD volume: %(error)s",
                           {'error': e}, instance=instance)
                if not force:
                    raise
        except Exception as e:
            LOG.warning("Unexpected error unmapping RBD volume: %(error)s",
                       {'error': e}, instance=instance)
            if not force:
                raise

    def _cleanup_all_rbd_mappings(self, instance):
        """Cleanup all RBD mapped devices for the instance.
        
        This method first tries to use 'rbd showmapped' to precisely find all
        currently mapped RBD devices. If that fails or returns no devices,
        it falls back to scanning /dev/rbd* devices directly.
        
        :param instance: Instance object
        """
        LOG.info("Cleaning up all RBD mappings for instance %s", instance.uuid,
                instance=instance)
        
        devices_unmapped = False
        
        try:
            # First, try to use 'rbd showmapped' to precisely query all mapped devices
            cluster_name = self._get_ceph_cluster_name()
            cmd = ['rbd', 'showmapped', '--format', 'json']
            if cluster_name and cluster_name != 'ceph':
                cmd.extend(['--cluster', cluster_name])
            
            try:
                out, err = processutils.execute(*cmd, run_as_root=True,
                                               root_helper='sudo')
                if out and out.strip():
                    mapped_devices = json.loads(out)
                    
                    # rbd showmapped --format json returns either a dict or a list
                    # Handle both cases
                    if isinstance(mapped_devices, list):
                        device_list = mapped_devices
                    elif isinstance(mapped_devices, dict):
                        device_list = mapped_devices.values()
                    else:
                        LOG.warning("Unexpected format from rbd showmapped: %s", type(mapped_devices),
                                   instance=instance)
                        device_list = []
                    
                    # Unmap all devices found
                    for device_info in device_list:
                        device_path = device_info.get('device')
                        pool = device_info.get('pool')
                        image = device_info.get('name')
                        
                        if device_path:
                            try:
                                LOG.info("Unmapping RBD device %(device)s (pool=%(pool)s, image=%(image)s)",
                                        {'device': device_path, 'pool': pool, 'image': image},
                                        instance=instance)
                                # Try unmap with device path first (most reliable)
                                # Build unmap command with same auth parameters as map
                                unmap_cmd = ['rbd', 'unmap', device_path]
                                if cluster_name and cluster_name != 'ceph':
                                    unmap_cmd.extend(['--cluster', cluster_name])
                                
                                # Try to get auth info from device_info or use defaults
                                # Note: device_info from showmapped may not have auth info,
                                # so we'll try without auth first, then with common auth users
                                try:
                                    processutils.execute(*unmap_cmd, run_as_root=True,
                                                        root_helper='sudo')
                                    LOG.info("Successfully unmapped RBD device %s", device_path,
                                            instance=instance)
                                    devices_unmapped = True
                                except processutils.ProcessExecutionError as e:
                                    # If unmap by device path fails, try by image name with auth
                                    error_str = str(e).lower()
                                    if 'not mapped' in error_str or 'not found' in error_str:
                                        LOG.debug("RBD device %s already unmapped", device_path,
                                                 instance=instance)
                                    else:
                                        LOG.warning("Failed to unmap RBD device %(device)s by path, "
                                                   "trying by image name: %(error)s. "
                                                   "Stdout: %(stdout)s, Stderr: %(stderr)s",
                                                   {'device': device_path, 'error': e,
                                                    'stdout': getattr(e, 'stdout', ''),
                                                    'stderr': getattr(e, 'stderr', '')},
                                                   instance=instance)
                                        # Try unmap by image name as fallback with auth
                                        if pool and image:
                                            unmap_cmd2 = ['rbd', 'unmap', image, '--pool', pool]
                                            if cluster_name and cluster_name != 'ceph':
                                                unmap_cmd2.extend(['--cluster', cluster_name])
                                            
                                            # Try with common auth users (cinder, nova, etc.)
                                            auth_users = ['cinder', 'nova', 'admin']
                                            unmap_success = False
                                            for auth_user in auth_users:
                                                try:
                                                    unmap_cmd_with_auth = unmap_cmd2 + ['--name', f'client.{auth_user}']
                                                    # Try to find keyring
                                                    keyring = self._find_ceph_keyring_for_user(cluster_name, auth_user, pool)
                                                    if keyring:
                                                        unmap_cmd_with_auth.extend(['--keyring', keyring])
                                                    
                                                    processutils.execute(*unmap_cmd_with_auth, run_as_root=True,
                                                                        root_helper='sudo')
                                                    LOG.info("Successfully unmapped RBD image %(pool)s/%(image)s "
                                                            "with auth user %(user)s",
                                                            {'pool': pool, 'image': image, 'user': auth_user},
                                                            instance=instance)
                                                    devices_unmapped = True
                                                    unmap_success = True
                                                    break
                                                except processutils.ProcessExecutionError:
                                                    continue
                                            
                                            if not unmap_success:
                                                # Try without auth as last resort
                                                try:
                                                    processutils.execute(*unmap_cmd2, run_as_root=True,
                                                                        root_helper='sudo')
                                                    LOG.info("Successfully unmapped RBD image %(pool)s/%(image)s "
                                                            "without auth",
                                                            {'pool': pool, 'image': image},
                                                            instance=instance)
                                                    devices_unmapped = True
                                                except processutils.ProcessExecutionError as e2:
                                                    LOG.warning("Failed to unmap RBD image %(pool)s/%(image)s: %(error)s. "
                                                               "Stdout: %(stdout)s, Stderr: %(stderr)s",
                                                               {'pool': pool, 'image': image, 'error': e2,
                                                                'stdout': getattr(e2, 'stdout', ''),
                                                                'stderr': getattr(e2, 'stderr', '')},
                                                               instance=instance)
                            except Exception as e:
                                LOG.warning("Unexpected error unmapping RBD device %(device)s: %(error)s",
                                           {'device': device_path, 'error': e},
                                           instance=instance)
            except processutils.ProcessExecutionError as e:
                # If rbd showmapped fails, log and fall back to scanning
                LOG.debug("rbd showmapped failed: %s, will scan /dev/rbd* devices", e,
                         instance=instance)
        except Exception as e:
            LOG.warning("Error during rbd showmapped query: %s", e, instance=instance)
        
        # Always scan /dev/rbd* devices as a fallback or additional check
        # This ensures we catch any devices that showmapped might have missed
        LOG.info("Scanning /dev/rbd* devices to ensure all RBD mappings are cleaned up",
                instance=instance)
        try:
            self._cleanup_rbd_devices_by_scan(instance)
        except Exception as e:
            LOG.warning("Error during RBD device scan cleanup: %s", e, instance=instance)
    
    def _cleanup_rbd_devices_by_scan(self, instance):
        """Fallback method to cleanup RBD devices by scanning /dev/rbd*.
        
        This scans /dev/rbd* devices and attempts to unmap them.
        This is used when 'rbd showmapped' fails or to catch any devices it missed.
        
        :param instance: Instance object
        """
        try:
            import glob
            cluster_name = self._get_ceph_cluster_name()
            
            # Find all /dev/rbd* block devices
            # Check both /dev/rbd* (like /dev/rbd0, /dev/rbd1) and /dev/rbd/* (like /dev/rbd/pool/image)
            rbd_devs = []
            
            # Check /dev/rbd0, /dev/rbd1, etc.
            for i in range(100):  # Check rbd0 to rbd99
                device_path = f'/dev/rbd{i}'
                if os.path.exists(device_path):
                    try:
                        mode = os.stat(device_path).st_mode
                        if stat.S_ISBLK(mode):
                            rbd_devs.append(device_path)
                    except OSError:
                        # Skip if stat fails
                        continue
            
            # Also check /dev/rbd/pool/image format
            rbd_dir = '/dev/rbd'
            if os.path.isdir(rbd_dir):
                for pool_dir in os.listdir(rbd_dir):
                    pool_path = os.path.join(rbd_dir, pool_dir)
                    if os.path.isdir(pool_path):
                        for image_file in os.listdir(pool_path):
                            image_path = os.path.join(pool_path, image_file)
                            if os.path.exists(image_path):
                                try:
                                    mode = os.stat(image_path).st_mode
                                    if stat.S_ISBLK(mode) or stat.S_ISREG(mode):
                                        rbd_devs.append(image_path)
                                except OSError:
                                    # Skip if stat fails
                                    continue
            
            if rbd_devs:
                LOG.info("Found %d RBD device(s) to unmap: %s", len(rbd_devs), rbd_devs,
                        instance=instance)
                for device_path in sorted(rbd_devs):
                    try:
                        LOG.info("Attempting to unmap RBD device %s", device_path,
                                instance=instance)
                        unmap_cmd = ['rbd', 'unmap', device_path]
                        if cluster_name and cluster_name != 'ceph':
                            unmap_cmd.extend(['--cluster', cluster_name])
                        
                        unmap_success = False
                        try:
                            # First try without auth
                            processutils.execute(*unmap_cmd, run_as_root=True,
                                                root_helper='sudo')
                            LOG.info("Successfully unmapped RBD device %s", device_path,
                                    instance=instance)
                            unmap_success = True
                        except processutils.ProcessExecutionError as e:
                            error_str = str(e).lower()
                            if 'not mapped' in error_str or 'not found' in error_str:
                                LOG.debug("RBD device %s already unmapped or not a mapped device",
                                         device_path, instance=instance)
                                unmap_success = True
                            else:
                                # Try with common auth users (cinder, nova, admin)
                                LOG.debug("Failed to unmap RBD device %s without auth, trying with auth users",
                                         device_path, instance=instance)
                                auth_users = ['cinder', 'nova', 'admin']
                                for auth_user in auth_users:
                                    try:
                                        unmap_cmd_with_auth = unmap_cmd + ['--name', f'client.{auth_user}']
                                        # Try to find keyring
                                        keyring = self._find_ceph_keyring_for_user(cluster_name, auth_user, None)
                                        if keyring:
                                            unmap_cmd_with_auth.extend(['--keyring', keyring])
                                        
                                        processutils.execute(*unmap_cmd_with_auth, run_as_root=True,
                                                            root_helper='sudo')
                                        LOG.info("Successfully unmapped RBD device %s with auth user %s",
                                                device_path, auth_user, instance=instance)
                                        unmap_success = True
                                        break
                                    except processutils.ProcessExecutionError:
                                        continue
                                
                                if not unmap_success:
                                    # Log the full error for debugging
                                    LOG.warning("Failed to unmap RBD device %(device)s: %(error)s. "
                                               "Stdout: %(stdout)s, Stderr: %(stderr)s",
                                               {'device': device_path, 'error': e,
                                                'stdout': getattr(e, 'stdout', ''),
                                                'stderr': getattr(e, 'stderr', '')},
                                               instance=instance)
                    except Exception as e:
                        LOG.warning("Unexpected error checking RBD device %(device)s: %(error)s",
                                   {'device': device_path, 'error': e},
                                   instance=instance)
            else:
                LOG.debug("No RBD devices found in /dev/", instance=instance)
        except Exception as e:
            LOG.warning("Error during RBD device scan cleanup: %s", e, instance=instance)

    def _find_ceph_keyring_for_user(self, cluster_name, auth_username, pool=None):
        """Find Ceph keyring file for a specific user.
        
        :param cluster_name: Ceph cluster name
        :param auth_username: Ceph username (e.g., 'cinder')
        :param pool: Optional pool name
        :returns: Keyring file path if found, None otherwise
        """
        if not auth_username:
            return None
        
        cluster_name = cluster_name or 'ceph'
        keyring_paths = []
        
        # Standard location: /etc/ceph/{cluster}.client.{user}.keyring
        keyring_paths.append(
            f'/etc/ceph/{cluster_name}.client.{auth_username}.keyring')
        
        # Alternative: /etc/ceph/{cluster}.keyring
        keyring_paths.append(f'/etc/ceph/{cluster_name}.keyring')
        
        # Pool-specific: /etc/ceph/{cluster}.client.{pool}.keyring
        if pool:
            keyring_paths.append(
                f'/etc/ceph/{cluster_name}.client.{pool}.keyring')
        
        # Check each path
        for keyring_path in keyring_paths:
            if os.path.exists(keyring_path):
                LOG.debug("Found Ceph keyring: %s", keyring_path)
                return keyring_path
        
        return None

    def _find_ceph_keyring(self, data):
        """Auto-detect Ceph keyring file path.
        
        :param data: Connection data dictionary
        :returns: Keyring file path if found, None otherwise
        """
        cluster_name = data.get('cluster_name', 'ceph')
        auth_username = data.get('auth_username')
        
        if not auth_username:
            return None
        
        # Extract pool name from RBD name (format: pool/image)
        rbd_name = data.get('name', '')
        if '/' in rbd_name:
            pool = rbd_name.split('/')[0]
        else:
            pool = None
        
        # Try common keyring file locations
        keyring_paths = []
        
        # Standard location: /etc/ceph/{cluster}.client.{user}.keyring
        if cluster_name:
            keyring_paths.append(
                f'/etc/ceph/{cluster_name}.client.{auth_username}.keyring')
        
        # Alternative: /etc/ceph/{cluster}.keyring (if user matches cluster)
        if cluster_name:
            keyring_paths.append(f'/etc/ceph/{cluster_name}.keyring')
        
        # Pool-specific: /etc/ceph/{cluster}.client.{pool}.keyring
        if pool and cluster_name:
            keyring_paths.append(
                f'/etc/ceph/{cluster_name}.client.{pool}.keyring')
        
        # Check each path
        for keyring_path in keyring_paths:
            if os.path.exists(keyring_path):
                LOG.debug("Found Ceph keyring: %s", keyring_path)
                return keyring_path
        
        LOG.debug("No Ceph keyring file found in standard locations")
        return None

    def _read_ceph_keyring_password(self, keyring_path, auth_username):
        """Read Ceph password from keyring file.
        
        Ceph keyring file format:
        [client.cinder]
            key = AQAb...==
        
        or:
        [client.cinder]
        key = AQAb...==
        
        :param keyring_path: Path to keyring file
        :param auth_username: Ceph username (e.g., 'cinder')
        :returns: Password string if found, None otherwise
        """
        if not os.path.exists(keyring_path):
            LOG.warning("Keyring file does not exist: %s", keyring_path)
            return None
        
        try:
            with open(keyring_path, 'r') as f:
                content = f.read()
            
            # Look for the client section matching auth_username
            # Format: [client.{username}] or [client.{username}]
            section_pattern = f'[client.{auth_username}]'
            
            # Parse the keyring file
            lines = content.split('\n')
            in_section = False
            for line in lines:
                line = line.strip()
                
                # Check if we're in the right section
                if line == section_pattern:
                    in_section = True
                    continue
                
                # If we hit another section, stop looking
                if line.startswith('[') and line.endswith(']'):
                    if in_section:
                        # We were in the section but didn't find key, try next section
                        break
                    in_section = False
                    continue
                
                # If we're in the right section, look for 'key = ...'
                if in_section:
                    if line.startswith('key'):
                        # Extract the key value
                        # Format: key = AQAb...==
                        parts = line.split('=', 1)
                        if len(parts) == 2:
                            password = parts[1].strip()
                            if password:
                                LOG.debug("Found password in keyring file for user %s",
                                         auth_username)
                                return password
            
            # If we didn't find it in a specific section, try to find any key
            # (some keyring files don't have section headers)
            for line in lines:
                line = line.strip()
                if line.startswith('key'):
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        password = parts[1].strip()
                        if password:
                            LOG.debug("Found password in keyring file (no section header)")
                            return password
            
            LOG.warning("No password found in keyring file: %s", keyring_path)
            return None
            
        except Exception as e:
            LOG.error("Error reading keyring file %(path)s: %(error)s",
                     {'path': keyring_path, 'error': e})
            return None

    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator and the hostname of the machine as follows::

            {
                'ip': ip,
                'initiator': initiator,
                'host': hostname
            }

        :param instance: Instance object
        :returns: Dictionary with connector properties
        """
        connector = {
            'ip': CONF.my_ip,
            'host': CONF.host,
            'initiator': None,
            'multipath': getattr(CONF.libvirt, 'volume_use_multipath', False),
        }
        if getattr(instance, 'architecture', None):
            connector['platform'] = instance.architecture
        if getattr(instance, 'os_type', None):
            connector['os_type'] = instance.os_type
        return connector

    def _plug_vifs(self, context, node, instance, network_info):
        """Plug VIFs into networks using SF.

        :param node: DPUNode object
        :param instance: Instance object
        :param network_info: Network information
        """
        LOG.debug("plug: instance_uuid=%(uuid)s",
                  {'uuid': instance.uuid})
        
        for vif in network_info:
            try:
                self.sf_client.add_interface(context, vif, instance)
            except Exception as e:
                LOG.error("Failed to add interface %(vif)s to instance "
                         "%(instance)s: %(reason)s",
                         {'vif': vif.get('id', 'unknown'),
                          'instance': instance.uuid,
                          'reason': str(e)},
                         instance=instance)
                raise exception.VirtualInterfacePlugException(
                    _("Failed to add interface: %s") % str(e))

    def _unplug_vifs(self, node, instance, network_info):
        """Unplug VIFs from networks using SF.

        :param node: DPUNode object
        :param instance: Instance object
        :param network_info: Network information
        """
        LOG.debug("unplug: instance_uuid=%(uuid)s",
                  {'uuid': instance.uuid})
        
        if not network_info:
            return
        
        for vif in network_info:
            try:
                self.sf_client.remove_interface(vif, instance)
            except Exception as e:
                LOG.warning("Failed to remove interface %(vif)s from instance "
                           "%(instance)s: %(reason)s",
                           {'vif': vif.get('id', 'unknown'),
                            'instance': instance.uuid,
                            'reason': str(e)},
                           instance=instance)

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks (compatibility method).

        :param instance: Instance object
        :param network_info: Network information
        """
        node = self._get_node(instance.node)
        ctxt = nova_context.get_admin_context()
        self._plug_vifs(ctxt, node, instance, network_info)

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks.

        :param instance: Instance object
        :param network_info: Network information
        """
        node = self._get_node(instance.node)
        self._unplug_vifs(node, instance, network_info)

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the disk to the instance at mountpoint.

        :param context: Security context
        :param connection_info: Connection information dictionary
        :param instance: Instance object
        :param mountpoint: Mount point (e.g., '/dev/vdb')
        :param disk_bus: Disk bus type (unused)
        :param device_type: Device type (unused)
        :param encryption: Encryption information (unused)
        """
        LOG.debug('Attaching volume to instance %s at %s',
                  instance.uuid, mountpoint)
        
        try:
            connection_info = self._ensure_volume_connected(
                connection_info, instance)
            self.spdk_client.add_volume(connection_info, instance, mountpoint)
        except Exception as e:
            LOG.error("Failed to attach volume to instance %(instance)s: "
                     "%(reason)s",
                     {'instance': instance.uuid,
                      'reason': str(e)},
                     instance=instance)
            raise exception.VolumeAttachFailed(
                _("Failed to attach volume: %s") % str(e))

    def detach_volume(self, context, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the disk attached to the instance.

        :param context: Security context
        :param connection_info: Connection information dictionary
        :param instance: Instance object
        :param mountpoint: Mount point (e.g., '/dev/vdb')
        :param encryption: Encryption information (unused)
        """
        LOG.debug('Detaching volume from instance %s at %s',
                  instance.uuid, mountpoint)
        
        try:
            self.spdk_client.remove_volume(connection_info, instance, mountpoint)
        except Exception as e:
            LOG.error("Failed to detach volume from instance %(instance)s: "
                     "%(reason)s",
                     {'instance': instance.uuid,
                      'reason': str(e)},
                     instance=instance)
            raise exception.VolumeDetachFailed(
                _("Failed to detach volume: %s") % str(e))
        finally:
            self._disconnect_volume(connection_info, instance)

    def attach_interface(self, context, instance, image_meta, vif):
        """Use hotplug to add a network interface to a running instance.

        :param context: The request context.
        :param instance: The instance which will get an additional interface.
        :param image_meta: Image metadata (unused)
        :param vif: The VIF object with interface information.
        :raises: nova.exception.NovaException if the attach fails.
        """
        LOG.debug('Attaching interface to instance %s', instance.uuid)
        
        try:
            self.sf_client.add_interface(context, vif, instance)
        except Exception as e:
            LOG.error("Failed to attach interface to instance %(instance)s: "
                     "%(reason)s",
                     {'instance': instance.uuid,
                      'reason': str(e)},
                     instance=instance)
            raise exception.VirtualInterfacePlugException(
                _("Failed to attach interface: %s") % str(e))

    def detach_interface(self, context, instance, vif):
        """Use hotunplug to remove a network interface from a running instance.

        :param context: The request context.
        :param instance: The instance which gets an interface removed.
        :param vif: The VIF object with interface information.
        :raises: nova.exception.NovaException if the detach fails.
        """
        LOG.debug('Detaching interface from instance %s', instance.uuid)
        
        try:
            self.sf_client.remove_interface(vif, instance)
        except Exception as e:
            LOG.error("Failed to detach interface from instance %(instance)s: "
                     "%(reason)s",
                     {'instance': instance.uuid,
                      'reason': str(e)},
                     instance=instance)
            raise exception.VirtualInterfaceUnplugException(
                _("Failed to detach interface: %s") % str(e))

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance.

        This method cleans up local DPU resources (SPDK/SNAP and SF) and
        triggers IPMI reset. It does NOT delete volumes or ports via APIs.

        :param instance: Instance object
        :param timeout: Time to wait for GuestOS to shutdown (unused for DPU)
        :param retry_interval: How often to signal guest while waiting (unused for DPU)
        """
        LOG.info('Powering off instance %s', instance.uuid, instance=instance)
        
        node_id = instance.get('node')
        if not node_id:
            LOG.warning('No node ID found for instance %s', instance.uuid)
            return
        
        try:
            node = self._get_node(node_id)
        except exception.InstanceNotFound:
            LOG.warning('Node %s not found for instance %s',
                       node_id, instance.uuid)
            return
        
        # Get network_info and block_device_info from instance
        # Since we don't have context here, we'll use admin context
        try:
            context = nova_context.get_admin_context()
            
            # Get network_info from instance info_cache if available
            network_info = None
            if hasattr(instance, 'info_cache') and instance.info_cache:
                try:
                    network_info = instance.info_cache.network_info
                except Exception:
                    LOG.debug("Could not get network_info from info_cache",
                             instance=instance)
            
            # Get block_device_info from BDMs
            block_device_info = None
            try:
                bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                    context, instance.uuid)
                if bdms:
                    # Use virt_driver.get_block_device_info to build block_device_info
                    block_device_info = virt_driver.get_block_device_info(
                        instance, bdms)
            except Exception as e:
                LOG.warning("Could not get block_device_info: %s", e,
                           instance=instance)
            
            # Cleanup local resources
            self.cleanup_local_resources(context, instance, network_info,
                                        block_device_info)
            
        except Exception as e:
            LOG.error("Error powering off instance %(instance)s: %(reason)s",
                     {'instance': instance.uuid, 'reason': str(e)},
                     instance=instance)
            # Still update node state even if cleanup failed
            try:
                node.instance_id = None
                node.provision_state = dpu_states.AVAILABLE
                node.power_state = dpu_states.POWER_OFF
            except Exception:
                pass
            raise

    def power_on(self, context, instance, network_info,
                 block_device_info=None, accel_info=None, share_info=None):
        """Power on the specified instance.

        This method restores local DPU resources (SPDK/SNAP and SF) and
        triggers IPMI reset. It does NOT create volumes or ports via APIs.

        :param context: Security context
        :param instance: Instance object
        :param network_info: Network information
        :param block_device_info: Block device information
        :param accel_info: Accelerator information (unused)
        :param share_info: Share information (unused)
        """
        LOG.info('Powering on instance %s', instance.uuid, instance=instance)
        
        # Restore local resources (SPDK/SNAP and SF)
        self.restore_local_resources(context, instance, network_info,
                                     block_device_info)
        
        LOG.info('Successfully powered on instance %s', instance.uuid,
                instance=instance)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None,
               accel_info=None, share_info=None):
        """Reboot the specified instance.

        This method always performs:
        1. Call power_off to cleanup local resources
        2. Call power_on to restore local resources (mount disks and network)
        
        This ensures that disks and network interfaces are properly mounted
        after reboot, regardless of the instance's current power state.

        :param context: Security context
        :param instance: Instance object
        :param network_info: Network information
        :param reboot_type: Either HARD or SOFT reboot
        :param block_device_info: Block device information
        :param bad_volumes_callback: Callback for bad volumes (unused)
        :param accel_info: Accelerator information (unused)
        :param share_info: Share information (unused)
        """
        LOG.info('Rebooting instance %s (type: %s, power_state: %s)',
                 instance.uuid, reboot_type, instance.power_state, instance=instance)
        
        node = self._get_node(instance.node)
        
        try:
            # Always power off first to cleanup local resources
            LOG.info('Powering off instance %s before reboot', instance.uuid,
                    instance=instance)
            self.power_off(instance)
            
            # Get block_device_info if not provided
            if not block_device_info:
                try:
                    bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                        context, instance.uuid)
                    if bdms:
                        block_device_info = virt_driver.get_block_device_info(
                            instance, bdms)
                except Exception as e:
                    LOG.warning("Could not get block_device_info for reboot: %s", e,
                               instance=instance)
            
            # Get network_info if not provided
            if not network_info:
                try:
                    if hasattr(instance, 'info_cache') and instance.info_cache:
                        network_info = instance.info_cache.network_info
                except Exception as e:
                    LOG.warning("Could not get network_info for reboot: %s", e,
                               instance=instance)
            
            # Then power on to restore local resources
            LOG.info('Powering on instance %s after power off', instance.uuid,
                    instance=instance)
            self.power_on(context, instance, network_info, block_device_info)
            
            LOG.info('Successfully rebooted instance %s (power off -> power on)',
                    instance.uuid, instance=instance)
        except Exception as e:
            LOG.error('Failed to reboot instance %s (power off -> power on): %s',
                     instance.uuid, e, instance=instance)
            raise

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        """Cold-migration source step: power off and return disk_info."""
        LOG.info("Cold-migration source step for instance %(inst)s: "
                 "powering off before migrate to %(dest)s",
                 {'inst': instance.uuid, 'dest': dest}, instance=instance)
        # DPU driver has no local instance disk copy phase; volumes are
        # re-attached on destination. Return empty disk_info per Nova contract.
        self.power_off(instance, timeout=timeout, retry_interval=retry_interval)
        return jsonutils.dumps([])

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         allocations, block_device_info=None, power_on=True):
        """Cold-migration destination step: spawn instance on destination."""
        LOG.info("Finishing cold migration on destination for instance %(inst)s "
                 "(power_on=%(power_on)s, resize_instance=%(resize)s)",
                 {'inst': instance.uuid, 'power_on': power_on,
                  'resize': resize_instance},
                 instance=instance)
        self._cleanup_migration_target_residuals(
            context, instance, network_info, block_device_info)
        # 目的端的残留 ISO 已在 _cleanup_migration_target_residuals 中删除，
        # 这里 spawn 会按需在本机重新生成 config drive 并挂载（force_config_drive
        # 或 image meta 要求时），保证 metadata/network_info 与目的端一致。
        LOG.info("Spawning instance %s on migration target; config drive will "
                 "be regenerated locally if required",
                 instance.uuid, instance=instance)
        self.spawn(context, instance, image_meta, injected_files=[],
                   admin_password=None, allocations=allocations,
                   network_info=network_info,
                   block_device_info=block_device_info,
                   power_on=power_on)

    def confirm_migration(self, context, migration, instance, network_info):
        """Confirm resize/cold-migration on source host.

        Source-side local resources should already be cleaned by
        migrate_disk_and_power_off/power_off. Keep as best-effort cleanup.
        """
        LOG.info("Confirming migration for instance %s on source host",
                 instance.uuid, instance=instance)
        # 清理源节点 RoCE SF；传 caller_host=CONF.host，sidecar 发现 DB 已属于
        # 目标节点则跳过 DB 删除，确保迁移后 DB 记录不被误删。
        try:
            self._teardown_roce(instance, caller_host=CONF.host)
        except Exception as exc:
            LOG.warning("RoCE teardown on confirm_migration failed  "
                        "instance=%s: %s", instance.uuid, exc, instance=instance)
        try:
            self.cleanup_local_resources(context, instance, network_info,
                                         block_device_info=None)
        except Exception as exc:
            LOG.warning("Best-effort source cleanup on confirm_migration "
                        "failed for instance %(inst)s: %(err)s",
                        {'inst': instance.uuid, 'err': exc},
                        instance=instance)

    def finish_revert_migration(self, context, migration, instance, network_info,
                                block_device_info=None,
                                power_on=True):
        """Rollback resize/cold-migration to source host."""
        LOG.info("Finishing revert migration for instance %(inst)s "
                 "(power_on=%(power_on)s)",
                 {'inst': instance.uuid, 'power_on': power_on},
                 instance=instance)
        # Ensure source-side local resources are restored after revert.
        self.restore_local_resources(context, instance, network_info,
                                     block_device_info)
        # 重新在源节点注册 RoCE 并创建 SF；sidecar 检测到 node_host 从目标节点
        # 变回源节点时自动 UPDATE DB，DHCP 恢复由本节点 sidecar 响应。
        try:
            self._setup_roce(instance, instance.flavor)
        except Exception as exc:
            LOG.warning("RoCE setup on revert migration failed  "
                        "instance=%s: %s", instance.uuid, exc, instance=instance)
        if not power_on:
            # Match Nova contract: leave VM stopped if requested.
            self.power_off(instance)

    def _cleanup_migration_target_residuals(self, context, instance,
                                            network_info, block_device_info):
        """Best-effort cleanup for stale local resources on migration target."""
        node = None
        try:
            node = self._get_node(instance.node)
        except Exception as exc:
            LOG.warning("Could not resolve target node for residual cleanup: %s",
                        exc, instance=instance)

        try:
            if block_device_info:
                self._remove_volumes(context, instance, block_device_info)
        except Exception as exc:
            LOG.warning("Best-effort residual volume cleanup failed for "
                        "instance %(inst)s: %(err)s",
                        {'inst': instance.uuid, 'err': exc},
                        instance=instance)

        try:
            if not self._has_boot_volume(block_device_info):
                # For image-backed root, only remove local SPDK state.
                self._cleanup_root_volume(instance, delete_volume=False)
        except Exception as exc:
            LOG.warning("Best-effort residual root cleanup failed for "
                        "instance %(inst)s: %(err)s",
                        {'inst': instance.uuid, 'err': exc},
                        instance=instance)

        try:
            if node and network_info:
                self._unplug_vifs(node, instance, network_info)
        except Exception as exc:
            LOG.warning("Best-effort residual network cleanup failed for "
                        "instance %(inst)s: %(err)s",
                        {'inst': instance.uuid, 'err': exc},
                        instance=instance)

        # NOTE(resize/cold-migrate): config drive 文件不会跨主机迁移；
        # 当 instances_path 走共享存储时，源端的旧 ISO 会在目的端可见，
        # create_configdrive 看到 iso_path 已存在会直接复用，导致挂上去的
        # 还是源端那份旧的（network_info / metadata 可能已变）。
        # 这里强制把目的端的残留 ISO 一并删掉，让后续 spawn 流程在本机
        # 重新生成一份并挂上去。
        try:
            self.configdrive_manager.cleanup_configdrive(instance, delete_iso=True)
        except Exception as exc:
            LOG.warning("Best-effort residual config drive cleanup failed for "
                        "instance %(inst)s: %(err)s",
                        {'inst': instance.uuid, 'err': exc},
                        instance=instance)

    def get_console_output(self, context, instance):
        """Get console output for an instance.

        :param context: Security context
        :param instance: Instance object
        :returns: Console output (not implemented)
        """
        raise NotImplementedError("Console output not implemented for DPU driver")

    def get_vnc_console(self, context, instance):
        """Get VNC console for an instance.

        Returns (host, port) for nova-novncproxy to connect to. Typically:
        - host: [dpu] vnc_listen_address (DPU management IP when using
          sol-vnc-agent on DPU, or compute node when using dpu-vnc-proxy).
        - port: [dpu] vnc_listen_port if set (e.g. 5900 for SOL-VNC agent),
          else port_base + (hash(instance.uuid) % port_range).

        :param context: Security context
        :param instance: Instance object
        :returns: ConsoleVNC with host and port for the VNC proxy
        :raises: exception.ConsoleTypeUnavailable if VNC not configured
        """
        if not CONF.vnc.enabled:
            raise exception.ConsoleTypeUnavailable(console_type='vnc')

        host = CONF.dpu.vnc_listen_address
        if not host:
            host = CONF.vnc.server_proxyclient_address
        if not host:
            LOG.warning('VNC console not configured: set [dpu] '
                        'vnc_listen_address or [vnc] server_proxyclient_address',
                        instance=instance)
            raise exception.ConsoleTypeUnavailable(console_type='vnc')

        fixed_port = getattr(CONF.dpu, 'vnc_listen_port', 0) or 0
        if fixed_port > 0:
            port = fixed_port
        else:
            base = CONF.dpu.vnc_listen_port_base
            port_range = CONF.dpu.vnc_listen_port_range
            digest = hashlib.md5(instance.uuid.encode()).hexdigest()
            port_offset = int(digest[:8], 16) % port_range
            port = base + port_offset

        LOG.debug('Returning VNC console for instance at %s:%s',
                  host, port, instance=instance)
        return console_type.ConsoleVNC(host=host, port=port)

    def get_spice_console(self, context, instance):
        """Get SPICE console for an instance.

        :param context: Security context
        :param instance: Instance object
        :returns: Console information
        """
        raise NotImplementedError("SPICE console not implemented for DPU driver")

    def get_serial_console(self, context, instance):
        """Get serial console for an instance.

        :param context: Security context
        :param instance: Instance object
        :returns: Console information
        """
        raise NotImplementedError("Serial console not implemented for DPU driver")

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True,
                destroy_secrets=True):
        """Cleanup the instance resources.

        Instance should have been destroyed from the Hypervisor before calling
        this method.

        :param context: Security context
        :param instance: Instance object
        :param network_info: Instance network information
        :param block_device_info: Information about block devices
        :param destroy_disks: Indicates if disks should be destroyed
        :param migrate_data: Implementation specific params (unused)
        :param destroy_vifs: Indicates if VIFs should be unplugged
        :param destroy_secrets: Indicates if secrets should be destroyed (unused)
        """
        LOG.debug('Cleanup called for instance', instance=instance)
        
        node_id = instance.get('node')
        if not node_id:
            LOG.warning('No node ID found for instance %s during cleanup',
                       instance.uuid)
            return

        try:
            node = self._get_node(node_id)
        except exception.InstanceNotFound:
            LOG.warning('Node %s not found for instance %s during cleanup',
                       node_id, instance.uuid)
            return

        try:
            # Cleanup network interfaces if needed
            if destroy_vifs and network_info:
                self._unplug_vifs(node, instance, network_info)

            # Cleanup volumes if needed
            if destroy_disks and block_device_info:
                self._remove_volumes(context, instance, block_device_info)

        except Exception as e:
            LOG.warning("Error during cleanup of instance %(instance)s: "
                       "%(reason)s",
                       {'instance': instance.uuid,
                        'reason': str(e)},
                       instance=instance)

    def extend_volume(self, context, connection_info, instance,
                      requested_size):
        """Extend the disk attached to the instance.

        :param context: Security context
        :param connection_info: Connection information dictionary
        :param instance: Instance object
        :param requested_size: New size of the volume in bytes
        """
        LOG.info('Extending volume for instance %(instance)s to %(size)s bytes',
                 {'instance': instance.uuid, 'size': requested_size},
                 instance=instance)

        # 1) 先在来宾可见的分区上执行 growpart（基于 device_path）
        try:
            device_path = self.spdk_client.get_device_path_for_volume(
                connection_info, instance)

            if not device_path:
                LOG.warning(
                    "Could not determine device_path for extended volume on "
                    "instance %(instance)s; skipping partition grow "
                    "(requested_size=%(size)s bytes). This may be normal if "
                    "the volume is not yet attached or SPDK bdev is not found.",
                    {'instance': instance.uuid, 'size': requested_size},
                    instance=instance)
                return

            LOG.info(
                "Extending partition 1 on device %(dev)s after volume extend "
                "(instance=%(instance)s, requested_size=%(size)s bytes)",
                {'dev': device_path,
                 'instance': instance.uuid,
                 'size': requested_size},
                instance=instance)

            # 调用已有的分区扩展函数（内部已处理 growpart 失败的情况）
            self._expand_partition_after_map(device_path, instance)

            LOG.info(
                "Completed partition extension for volume on instance %(instance)s "
                "(device=%(dev)s, requested_size=%(size)s bytes)",
                {'instance': instance.uuid,
                 'dev': device_path,
                 'size': requested_size},
                instance=instance)

        except Exception as exc:
            # _expand_partition_after_map 内部已经对 growpart 做了 try/except，
            # 这里是兜底，任何异常只记 warning，不影响后端 bdev 扩容
            LOG.warning(
                "Unexpected error while growing partition before backend resize "
                "for instance %(instance)s: %(err)s; continuing to SPDK resize",
                {'instance': instance.uuid, 'err': exc},
                instance=instance,
                exc_info=True)

        # 2) 然后让 SPDK 后端执行 RBD bdev 扩容（bdev_rbd_resize）
        try:
            self.spdk_client.extend_volume(connection_info, instance,
                                           requested_size)
        except Exception:
            LOG.error(
                "SPDK backend resize failed for volume on instance %(instance)s",
                {'instance': instance.uuid},
                instance=instance,
                exc_info=True)
            raise

    def list_instance_uuids(self):
        """Return the UUIDs of all instances known to the DPU driver.
        
        This method queries SPDK/SNAP and SF to find existing instances
        that were created before the compute node restarted.
        
        :returns: List of instance UUIDs
        """
        instance_uuids = set()
        
        # 1. Query SPDK bdevs to find instance UUIDs from volume names
        try:
            bdevs = spdk_rpc.bdev_get_bdevs(socket_path=self.spdk_client.endpoint)
            if bdevs:
                for bdev in bdevs:
                    bdev_name = bdev.get('name', '')
                    # Extract instance UUID from bdev names
                    # Format: nova_root_{uuid} or volume-{uuid} or {uuid}
                    uuid_match = re.search(
                        r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                        bdev_name, re.IGNORECASE)
                    if uuid_match:
                        instance_uuids.add(uuid_match.group(1).lower())
                    # Also check for nova-root-{uuid} pattern
                    if 'nova' in bdev_name.lower() and 'root' in bdev_name.lower():
                        uuid_match = re.search(
                            r'nova[-_]root[-_]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                            bdev_name, re.IGNORECASE)
                        if uuid_match:
                            instance_uuids.add(uuid_match.group(1).lower())
        except Exception as exc:
            LOG.warning("Failed to query SPDK bdevs for instance UUIDs: %s", exc)
        
        # 2. Query OVS to find instance UUIDs from port bindings
        # OVS ports have iface-id set to Neutron port ID, which we can use
        # to query Neutron for instance UUID (if needed)
        # For now, we rely on SPDK bdev names as the primary source
        
        LOG.info("Found %d instance UUIDs from SPDK/SNAP: %s",
                 len(instance_uuids), list(instance_uuids))
        return list(instance_uuids)

    def _recover_existing_instances(self):
        """Recover storage and network for instances that exist on this host.
        
        This method is called during init_host to restore the state of
        instances that were running before the compute node restarted.
        It queries SPDK/SNAP and SF to find existing instances and then
        restores their storage and network configuration.
        
        This recovery is performed for ALL instances found on the DPU,
        regardless of their power state (running or shutdown), because
        even shutdown instances need their storage and network resources
        restored so they can be powered on later.
        """
        LOG.info("Recovering existing instances after compute node restart")
        
        # Get admin context to access all instances
        context = nova_context.get_admin_context()
        
        # Get list of instance UUIDs from multiple sources:
        # 1. From SPDK/SNAP (instances that have resources on DPU)
        # 2. From database (all instances on this host, including shutdown ones)
        instance_uuids_from_spdk = set(self.list_instance_uuids())
        
        # Also query database for all instances on this host
        # This ensures we recover shutdown instances that may not have SPDK resources
        instance_uuids_from_db = set()
        try:
            instances = objects.InstanceList.get_by_host(context, CONF.host)
            for instance in instances:
                instance_uuids_from_db.add(instance.uuid)
            LOG.info("Found %d instances on this host from database: %s",
                    len(instance_uuids_from_db), list(instance_uuids_from_db))
        except Exception as e:
            LOG.warning("Failed to query instances from database: %s", e)
        
        # Combine both sets to get all instances that need recovery
        instance_uuids = instance_uuids_from_spdk | instance_uuids_from_db
        
        if not instance_uuids:
            LOG.info("No existing instances found to recover")
            return
        
        LOG.info("Found %d existing instances to recover (SPDK: %d, DB: %d): %s",
                 len(instance_uuids), len(instance_uuids_from_spdk),
                 len(instance_uuids_from_db), list(instance_uuids))
        
        # For each instance UUID, recover storage and network
        for instance_uuid in instance_uuids:
            try:
                # Get instance object from database
                instance = objects.Instance.get_by_uuid(context, instance_uuid)
                
                # Check if instance is on this host
                if instance.host != CONF.host:
                    LOG.debug("Instance %s is not on this host (%s), skipping recovery",
                             instance_uuid, CONF.host)
                    continue
                
                LOG.info("Recovering instance %s (power_state: %s, vm_state: %s)",
                        instance.uuid, instance.power_state, instance.vm_state)
                
                # Get network info
                network_info = None
                try:
                    if hasattr(instance, 'info_cache') and instance.info_cache:
                        network_info = instance.info_cache.network_info
                except Exception as e:
                    LOG.warning("Could not get network_info from info_cache for instance %s: %s",
                               instance.uuid, e)
                
                # Get block device info
                block_device_info = None
                try:
                    bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                        context, instance.uuid)
                    if bdms:
                        block_device_info = virt_driver.get_block_device_info(instance, bdms)
                except Exception as e:
                    LOG.warning("Could not get block_device_info for instance %s: %s",
                               instance.uuid, e)
                
                # Recover storage (volumes and ephemeral root volume)
                try:
                    self._recover_storage_for_instance(context, instance, block_device_info)
                    LOG.info("Successfully recovered storage for instance %s", instance.uuid)
                except Exception as e:
                    LOG.warning("Failed to recover storage for instance %s: %s",
                               instance.uuid, e)
                
                # Recover network (SF interfaces)
                if network_info:
                    try:
                        for vif in network_info:
                            try:
                                mac_address = vif.get('address')
                                if not mac_address:
                                    LOG.warning("VIF %s missing MAC address, skipping",
                                               vif.get('id', 'unknown'), instance=instance)
                                    continue
                                
                                # Check if SF already exists by MAC address
                                existing_sf_port_id, existing_sf_num = self.sf_client._find_sf_by_mac(mac_address)
                                if existing_sf_port_id and existing_sf_num is not None:
                                    LOG.info("SF already exists for MAC %s (port_id=%s, sf_num=%d), "
                                            "skipping recovery for VIF %s",
                                            mac_address, existing_sf_port_id, existing_sf_num,
                                            vif.get('id', 'unknown'), instance=instance)
                                    # Still call add_interface to ensure OVS configuration is correct
                                    # (it will detect existing SF and reuse it)
                                    self.sf_client.add_interface(context, vif, instance)
                                    LOG.debug("Verified/updated SF interface for VIF %s on instance %s",
                                             vif.get('id', 'unknown'), instance.uuid)
                                else:
                                    # SF doesn't exist, create it
                                    self.sf_client.add_interface(context, vif, instance)
                                    LOG.debug("Recovered SF interface for VIF %s on instance %s",
                                             vif.get('id', 'unknown'), instance.uuid)
                            except Exception as e:
                                LOG.warning("Failed to recover SF interface for VIF %(vif)s "
                                           "on instance %(instance)s: %(reason)s",
                                           {'vif': vif.get('id', 'unknown'),
                                            'instance': instance.uuid,
                                            'reason': str(e)})
                        LOG.info("Successfully recovered network for instance %s", instance.uuid)
                    except Exception as e:
                        LOG.warning("Failed to recover network for instance %s: %s",
                                   instance.uuid, e)
                else:
                    LOG.warning("No network_info available for instance %s, skipping network recovery",
                               instance.uuid)

                # Recover RoCE SF if the instance has an allocation in sidecar DB
                try:
                    allocation = self.roce_client.get_allocation(instance.uuid)
                    if allocation:
                        mac = allocation['mac']
                        vlan_id = allocation.get('vlan_id', 0)
                        representor = self.roce_sf_client.add_roce_interface(
                            instance_uuid=instance.uuid,
                            mac_address=mac,
                            vlan_id=vlan_id,
                            mtu=CONF.dpu.roce_mtu,
                        )
                        LOG.info("Recovered RoCE SF  instance=%s  mac=%s"
                                 "  vlan=%s  repr=%s",
                                 instance.uuid, mac, vlan_id, representor)
                    else:
                        LOG.debug("No RoCE allocation for instance %s,"
                                  " skipping RoCE recovery", instance.uuid)
                except Exception as exc:
                    LOG.warning("Failed to recover RoCE SF for instance %s: %s",
                                instance.uuid, exc)

                # Recover configdrive if it exists
                try:
                    if configdrive.required_by(instance):
                        from nova.virt.libvirt import utils as libvirt_utils
                        instance_dir = libvirt_utils.get_instance_path(instance)
                        iso_path = os.path.join(instance_dir, 'disk.config')
                        
                        if os.path.exists(iso_path):
                            LOG.info('Recovering config drive for instance %s from %s',
                                    instance.uuid, iso_path, instance=instance)
                            connection_info = self.configdrive_manager.build_connection_info(
                                iso_path, instance)
                            self.configdrive_manager.attach_configdrive(connection_info, instance)
                            LOG.info('Successfully recovered config drive for instance %s',
                                    instance.uuid, instance=instance)
                        else:
                            LOG.debug('Config drive ISO not found at %s for instance %s, skipping',
                                     iso_path, instance.uuid, instance=instance)
                except Exception as e:
                    LOG.warning('Failed to recover config drive for instance %s: %s',
                               instance.uuid, e, instance=instance)
                    # Don't fail the entire recovery process if configdrive recovery fails
                
            except exception.InstanceNotFound:
                LOG.warning("Instance %s not found in database, skipping recovery",
                           instance_uuid)
            except Exception as e:
                LOG.warning("Failed to recover instance %s: %s", instance_uuid, e)

    def _recover_storage_for_instance(self, context, instance, block_device_info=None):
        """Recover storage configuration for an instance.
        
        This method ensures that all volumes attached to the instance
        have their SPDK bdevs and SNAP namespaces properly configured.
        It is called during instance initialization after a compute node restart.
        
        Supports both:
        - Boot from volume: System disk is a Cinder volume (boot_index=0)
        - Boot from image: System disk is an ephemeral RBD volume created from image
        
        :param context: Security context
        :param instance: Instance object
        :param block_device_info: Block device information (optional)
        """
        LOG.info("Recovering storage for instance %s", instance.uuid)
        
        # Get block device mappings for this instance
        try:
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                context, instance.uuid)
        except Exception as exc:
            LOG.warning("Failed to get block device mappings for instance %s: %s",
                       instance.uuid, exc)
            return
        
        # Check if instance is booting from volume
        boot_from_volume = self._has_boot_volume(block_device_info) if block_device_info else False
        
        # Recover each volume (including boot volume if booting from volume)
        for bdm in bdms:
            if bdm.is_volume:
                try:
                    # Get connection info from BDM (stored during attach)
                    if bdm.connection_info:
                        connection_info = jsonutils.loads(bdm.connection_info)
                    else:
                        # Fallback: get from Cinder if not in BDM
                        connection_info = self._get_volume_connection_info(
                            context, bdm.volume_id)
                    
                    # Determine mountpoint
                    # For boot volume, use root device name
                    if boot_from_volume and bdm.boot_index in (0, '0'):
                        mountpoint = self._get_root_mountpoint(instance, block_device_info)
                        LOG.info("Recovering boot volume %s for instance %s at %s",
                                bdm.volume_id, instance.uuid, mountpoint)
                    else:
                        mountpoint = bdm.device_name or '/dev/vdb'
                        LOG.info("Recovering data volume %s for instance %s at %s",
                                bdm.volume_id, instance.uuid, mountpoint)
                    
                    # Check if SPDK bdev already exists before recovery
                    data = connection_info.get('data', {})
                    volume_id = self.spdk_client._volume_identifier(data, instance)
                    bdev_name = self.spdk_client._sanitize_bdev_name(volume_id)
                    
                    # Ensure volume is connected (rbd map if needed)
                    connection_info = self._ensure_volume_connected(
                        connection_info, instance)
                    
                    # Always call add_volume to ensure SPDK namespace exists
                    # add_volume will check if bdev/namespace already exists and reuse it
                    self.spdk_client.add_volume(connection_info, instance, mountpoint)
                    
                    LOG.info("Recovered storage for volume %s on instance %s",
                            bdm.volume_id, instance.uuid)
                except Exception as exc:
                    LOG.warning("Failed to recover storage for volume %s on instance %s: %s",
                               bdm.volume_id, instance.uuid, exc)
        
        # Recover root volume if it's an ephemeral root volume (boot from image)
        # Only recover if NOT booting from volume
        if not boot_from_volume:
            # First try to get from cache (normal case)
            root_info = self._ephemeral_root_volumes.get(instance.uuid)
            
            # If cache is missing (e.g., after service restart), rebuild from
            # deterministic root volume name and let add_volume recreate bdev.
            if not root_info:
                LOG.info("Ephemeral root volume cache missing for instance %s, "
                        "rebuilding root connection_info", instance.uuid)
                
                volume_name = self._build_root_volume_name(instance)
                pool = CONF.dpu.images_rbd_pool or CONF.libvirt.images_rbd_pool
                if not pool:
                    LOG.warning("No pool configured; cannot recover root volume %s",
                               volume_name, instance=instance)
                else:
                    if not self._root_rbd_image_exists(pool, volume_name, instance):
                        LOG.warning(
                            "Ephemeral root image %(pool)s/%(vol)s does not exist; "
                            "skip root recover for instance %(inst)s",
                            {'pool': pool, 'vol': volume_name, 'inst': instance.uuid},
                            instance=instance)
                        root_info = None
                    else:
                        root_data = {
                            'volume_id': volume_name,
                            'name': f"{pool}/{volume_name}",
                            'cluster_name': self._get_ceph_cluster_name(),
                            'auth_enabled': bool(CONF.libvirt.rbd_user),
                            'auth_username': CONF.libvirt.rbd_user,
                            'secret_type': 'ceph',
                            'serial': volume_name,
                        }
                        keyring = self._find_ceph_keyring(root_data)
                        if keyring:
                            root_data['keyring'] = keyring
                        root_info = {
                            'volume': volume_name,
                            'pool': pool,
                            'mountpoint': self._get_root_mountpoint(instance, block_device_info),
                            'connection_info': {
                                'driver_volume_type': 'rbd',
                                'data': root_data,
                            },
                        }
                        # Restore to cache for future use
                        self._ephemeral_root_volumes[instance.uuid] = root_info
                        LOG.info("Rebuilt ephemeral root volume info for instance %s",
                                 instance.uuid, instance=instance)
            
            # If we have root_info (from cache or recovered), restore the volume
            if root_info:
                connection_info = root_info.get('connection_info')
                mountpoint = root_info.get('mountpoint', '/dev/vda')
                
                try:
                    # Ensure root volume is connected (rbd map if needed)
                    connection_info = self._ensure_volume_connected(
                        connection_info, instance)
                    
                    # Ensure SPDK namespace exists (will reuse existing if present)
                    self.spdk_client.add_volume(connection_info, instance, mountpoint)
                    
                    LOG.info("Recovered ephemeral root volume for instance %s", instance.uuid)
                except Exception as exc:
                    LOG.warning("Failed to recover ephemeral root volume for instance %s: %s",
                               instance.uuid, exc)

    def _get_volume_connection_info(self, context, volume_id):
        """Get connection info for a volume from Cinder.
        
        :param context: Security context
        :param volume_id: Volume ID
        :returns: Connection info dictionary
        """
        from nova.volume import cinder
        
        try:
            volume = cinder.API().get(context, volume_id)
            return volume.get('connection_info', {})
        except Exception as exc:
            LOG.warning("Failed to get connection info for volume %s: %s",
                       volume_id, exc)
            raise

