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
SF (SmartNIC Function) client for network interface management.

This module provides interfaces to manage SF instances on Mellanox NICs,
including creation, configuration, OVS integration, and deletion.
"""

import random
import re
import time

from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_utils import excutils

import nova.conf
from nova import exception
from nova.i18n import _
from nova.network import neutron as neutronapi

LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF

# Controller is fixed to 1 for SF creation
SF_CONTROLLER = 1
# SF 创建失败时，在范围内随机换号重试的次数上限（含首次共 3 次）
SF_CREATE_MAX_ATTEMPTS = 3


class SFClient(object):
    """Client for SF network interface operations."""

    def __init__(self, endpoint=None):
        """Initialize SF client.

        :param endpoint: SF service endpoint (reserved for future use)
        """
        self.endpoint = endpoint
        self.mlxdevm_path = CONF.dpu.mlxdevm_path
        self.pci_address = CONF.dpu.sf_pci_address
        self.pfnum = CONF.dpu.sf_pfnum
        self.sf_num_min = CONF.dpu.sf_num_min
        self.sf_num_max = CONF.dpu.sf_num_max
        self.ovs_bridge = CONF.dpu.ovs_bridge
        self.representor_prefix = CONF.dpu.sf_representor_prefix
        self.sf_ovs_offload_mode = CONF.dpu.sf_ovs_offload_mode
        
        # Track SF allocations: instance_uuid -> sf_info
        self._sf_allocations = {}
        
        LOG.info("SF client initialized - pci=%s pfnum=%d sf_range=[%d,%d] "
                 "ovs_offload_mode=%s",
                 self.pci_address, self.pfnum, self.sf_num_min,
                 self.sf_num_max, self.sf_ovs_offload_mode)

    def _rebind_port_with_dpu_mac(self, context, instance, port_id,
                                  mac_address):
        """Unbind and rebind Neutron port with DPU MAC prefix.

        目标：
        - 在创建 SF 之前，对 Neutron port 做一次「解绑 → 改 MAC → 重新绑定」：
          * 新 MAC = fa:17:3e:XX:YY:ZZ（保留原来的后 3 个字节）
          * device_id 绑定回当前 instance.uuid
          * binding:host_id 绑定到 DPU 所在 host

        若任一步失败，返回原始 MAC，不影响后续 SF 创建流程。
        """
        original = (mac_address or "").lower()
        if not original or original.count(":") != 5:
            return mac_address

        parts = original.split(":")
        suffix = parts[3:]
        new_mac = "fa:17:3e:" + ":".join(suffix)
        if original == new_mac:
            return mac_address

        try:
            client = neutronapi.get_client(context, admin=True)
        except Exception as exc:
            LOG.warning("Failed to get Neutron client for DPU MAC update on "
                        "port %(port)s: %(err)s",
                        {"port": port_id, "err": exc})
            return mac_address

        host = getattr(instance, "host", None) or CONF.host

        # 1) 先解绑：清空 device_id / binding:host_id
        try:
            unbind_body = {"port": {"device_id": "", "binding:host_id": None}}
            client.update_port(port_id, unbind_body)
            LOG.info("Unbound Neutron port %(port)s before DPU MAC update",
                     {"port": port_id})
        except Exception as exc:
            LOG.warning("Failed to unbind Neutron port %(port)s before DPU "
                        "MAC update: %(err)s",
                        {"port": port_id, "err": exc})

        # 2) 改 MAC + 重新绑定到当前实例和 host
        body = {"port": {"mac_address": new_mac,
                         "device_id": instance.uuid,
                         "binding:host_id": host}}
        try:
            updated = client.update_port(port_id, body)
            updated_mac = (updated.get("port", {}).get("mac_address")
                           or new_mac)
            LOG.info("Rebound Neutron port %(port)s with MAC %(mac)s for "
                     "instance %(instance)s on host %(host)s",
                     {"port": port_id, "mac": updated_mac,
                      "instance": instance.uuid, "host": host})
            return updated_mac
        except Exception as exc:
            LOG.warning("Failed to rebind Neutron port %(port)s with new MAC "
                        "%(new)s (old=%(old)s): %(err)s",
                        {"port": port_id, "new": new_mac,
                         "old": original, "err": exc})
            return mac_address

    def _execute(self, *cmd, **kwargs):
        """Execute a command with proper error handling.

        :param cmd: Command and arguments
        :param kwargs: Additional arguments for processutils.execute
        :returns: Tuple of (stdout, stderr)
        :raises: exception.NovaException if command fails
        """
        try:
            # 使用 sudo 执行需要 root 权限的命令
            return processutils.execute(*cmd, run_as_root=True, root_helper='sudo', **kwargs)
        except processutils.ProcessExecutionError as e:
            LOG.error("Command failed: %s, error: %s", ' '.join(cmd), e)
            raise exception.NovaException(
                _("Failed to execute command: %(cmd)s, error: %(err)s") % {
                    'cmd': ' '.join(cmd), 'err': str(e)})

    def _get_used_sf_nums(self):
        """Get list of currently used SF numbers.

        :returns: Set of used SF numbers
        :raises: exception.NovaException if command fails
        """
        try:
            out, err = self._execute(self.mlxdevm_path, 'port', 'show')
            
            used_nums = set()
            # Parse output to find sfnum values
            # Example line: pci/0000:03:00.0/229409: ... sfnum 4 ...
            for line in out.split('\n'):
                match = re.search(r'sfnum\s+(\d+)', line)
                if match:
                    sf_num = int(match.group(1))
                    used_nums.add(sf_num)
            
            LOG.debug("Found %d used SF numbers: %s", len(used_nums), used_nums)
            return used_nums
            
        except exception.NovaException:
            # If command fails, return empty set and log warning
            LOG.warning("Failed to get used SF numbers, assuming none are used")
            return set()

    def _find_available_sf_num(self):
        """Find an available SF number in the configured range.

        :returns: Available SF number
        :raises: exception.NovaException if no SF numbers available
        """
        used_nums = self._get_used_sf_nums()
        
        # Find first available number in range
        for sf_num in range(self.sf_num_min, self.sf_num_max + 1):
            if sf_num not in used_nums:
                LOG.debug("Found available SF number: %d", sf_num)
                return sf_num
        
        # No available SF numbers
        msg = _("No available SF numbers in range [%(min)d, %(max)d]. "
                "All %(count)d numbers are in use.") % {
            'min': self.sf_num_min,
            'max': self.sf_num_max,
            'count': self.sf_num_max - self.sf_num_min + 1
        }
        LOG.error(msg)
        raise exception.NovaException(msg)

    def _random_sf_num_for_retry(self, excluded):
        """在配置范围内随机选一个未占用且不在 excluded 中的 sfnum。

        :param excluded: 已失败或已尝试过的 sfnum 集合
        :returns: 可用的 sfnum，若无候选则返回 None
        """
        used_nums = self._get_used_sf_nums()
        pool = [
            n for n in range(self.sf_num_min, self.sf_num_max + 1)
            if n not in used_nums and n not in excluded
        ]
        if not pool:
            return None
        return random.choice(pool)

    def _cleanup_failed_sf_setup(self, representor_name, sf_port_id):
        """创建 / 配置 SF 失败后的尽力清理。"""
        if representor_name:
            try:
                self._remove_from_ovs(representor_name)
            except Exception as cleanup_err:
                LOG.warning("Failed to cleanup OVS port %s: %s",
                            representor_name, cleanup_err)

        if sf_port_id:
            try:
                self._deactivate_sf(sf_port_id)
            except Exception as cleanup_err:
                LOG.warning("Failed to deactivate SF %s: %s",
                            sf_port_id, cleanup_err)

            try:
                self._delete_sf(sf_port_id)
            except Exception as cleanup_err:
                LOG.warning("Failed to delete SF %s: %s",
                            sf_port_id, cleanup_err)

    def _create_sf(self, sf_num):
        """Create an SF instance.

        :param sf_num: SF number to create
        :returns: SF port ID (e.g., pci/0000:03:00.0/229409)
        :raises: exception.NovaException if creation fails
        """
        cmd = [
            self.mlxdevm_path, 'port', 'add',
            self.pci_address,
            'flavour', 'pcisf',
            'pfnum', str(self.pfnum),
            'sfnum', str(sf_num),
            'controller', str(SF_CONTROLLER)
        ]
        
        LOG.info("Creating SF: pci=%s pfnum=%d sfnum=%d controller=%d",
                 self.pci_address, self.pfnum, sf_num, SF_CONTROLLER)
        
        out, err = self._execute(*cmd)
        
        # Parse output to get SF port ID
        # Expected output format: pci/0000:03:00.0/229409
        # or the command might not output anything, so we need to query
        
        # Query to find the newly created SF
        out, err = self._execute(self.mlxdevm_path, 'port', 'show')
        
        # Find the port ID for this sfnum
        candidate_port_id = None
        for line in out.split('\n'):
            # mlxdevm output example:
            # pci/0000:03:00.0/262144: type eth netdev ...
            if 'flavour pcisf' in line and f'pfnum {self.pfnum}' in line:
                pid = line.split()[0].rstrip(':')
                if pid:
                    candidate_port_id = pid
            if f'sfnum {sf_num}' in line:
                pid = line.split()[0].rstrip(':')
                if pid:
                    LOG.info("Created SF with port ID: %s", pid)
                    return pid
        
        # If we can't find it, log and fall back to a best-effort port id
        if candidate_port_id:
            LOG.warning("Could not find exact port ID for sfnum %d, "
                        "using candidate %s", sf_num, candidate_port_id)
            return candidate_port_id

        LOG.warning("Could not find port ID for sfnum %d in output, "
                   "using constructed ID", sf_num)
        # Expected format is pci/<bdf>/<port_index>
        return f"{self.pci_address}/{sf_num}"

    def _configure_sf(self, sf_port_id, mac_address):
        """Configure SF with MAC address and activate it.

        :param sf_port_id: SF port ID (e.g., pci/0000:03:00.0/229409)
        :param mac_address: MAC address to set
        :raises: exception.NovaException if configuration fails
        """
        cmd = [
            self.mlxdevm_path, 'port', 'function', 'set',
            sf_port_id,
            'hw_addr', mac_address,
            'trust', 'on',
            'state', 'active'
        ]
        
        LOG.info("Configuring SF %s: mac=%s state=active trust=on",
                 sf_port_id, mac_address)
        
        self._execute(*cmd)
        
        # Give the system a moment to activate the SF
        time.sleep(0.5)
        
        LOG.info("SF %s configured successfully", sf_port_id)

    def _add_to_ovs(self, representor_name, port_id):
        """Add SF representor to OVS bridge.

        :param representor_name: Name of representor interface (e.g., en3f0c1pf0sf4)
        :param port_id: Neutron port ID to set as iface-id
        :raises: exception.NovaException if OVS operations fail
        """
        cmd = ['ovs-vsctl', 'add-port', self.ovs_bridge, representor_name]
        cmd.extend([
            '--', 'set', 'Interface', representor_name,
            f'type={self.sf_ovs_offload_mode}'
        ])
        
        LOG.info("Adding representor %s to OVS bridge %s (offload_mode=%s)",
                 representor_name, self.ovs_bridge, self.sf_ovs_offload_mode)
        
        self._execute(*cmd)
        
        # Set iface-id for Neutron integration
        cmd = [
            'ovs-vsctl', 'set', 'Interface', representor_name,
            f'external_ids:iface-id={port_id}'
        ]
        
        LOG.info("Setting iface-id=%s for representor %s", port_id, representor_name)
        
        self._execute(*cmd)
        
        LOG.info("Representor %s added to OVS successfully", representor_name)

    def _find_sf_by_mac(self, mac_address):
        """Find SF port ID and number by MAC address.

        :param mac_address: MAC address to search for
        :returns: Tuple of (sf_port_id, sf_num) or (None, None) if not found
        """
        try:
            out, err = self._execute(self.mlxdevm_path, 'port', 'show')
            
            # Parse output to find SF with matching MAC
            # Example output:
            # pci/0000:03:00.0/229409: type eth netdev eth1 flavour pcisf controller 1 pfnum 0 sfnum 4
            #   function:
            #     hw_addr 00:00:00:00:04:00 state active
            
            lines = out.split('\n')
            current_port_id = None
            current_sf_num = None
            
            for i, line in enumerate(lines):
                # Check if this is a port line
                # Port ID format: pci/0000:03:00.0/262144:
                # Match the full port ID including BDF and port index
                port_match = re.match(r'(pci/[^/\s]+/[^:\s]+):', line)
                if port_match:
                    current_port_id = port_match.group(1)
                    # Extract sfnum from same line
                    sfnum_match = re.search(r'sfnum\s+(\d+)', line)
                    if sfnum_match:
                        current_sf_num = int(sfnum_match.group(1))
                
                # Check if this line contains the MAC address
                if mac_address.lower() in line.lower():
                    if current_port_id and current_sf_num is not None:
                        LOG.debug("Found SF by MAC %s: port_id=%s sfnum=%d",
                                 mac_address, current_port_id, current_sf_num)
                        return current_port_id, current_sf_num
            
            LOG.warning("SF with MAC address %s not found", mac_address)
            return None, None
            
        except exception.NovaException as e:
            LOG.error("Failed to search for SF by MAC %s: %s", mac_address, e)
            return None, None

    def _remove_from_ovs(self, representor_name):
        """Remove SF representor from OVS bridge.

        :param representor_name: Name of representor interface
        :raises: exception.NovaException if removal fails
        """
        cmd = ['ovs-vsctl', 'del-port', self.ovs_bridge, representor_name]
        
        LOG.info("Removing representor %s from OVS bridge %s",
                 representor_name, self.ovs_bridge)
        
        try:
            self._execute(*cmd)
            LOG.info("Representor %s removed from OVS successfully", representor_name)
        except exception.NovaException as e:
            # Port might not exist in OVS, which is okay
            if 'no port named' in str(e).lower():
                LOG.debug("Representor %s not found in OVS, skipping removal",
                         representor_name)
            else:
                raise

    def _deactivate_sf(self, sf_port_id):
        """Deactivate an SF instance.

        :param sf_port_id: SF port ID
        :raises: exception.NovaException if deactivation fails
        """
        cmd = [
            self.mlxdevm_path, 'port', 'function', 'set',
            sf_port_id,
            'state', 'inactive'
        ]
        
        LOG.info("Deactivating SF %s", sf_port_id)
        
        self._execute(*cmd)
        
        # Give the system a moment to deactivate
        time.sleep(0.5)
        
        LOG.info("SF %s deactivated successfully", sf_port_id)

    def _delete_sf(self, sf_port_id):
        """Delete an SF instance.

        :param sf_port_id: SF port ID
        :raises: exception.NovaException if deletion fails
        """
        cmd = [self.mlxdevm_path, 'port', 'del', sf_port_id]
        
        LOG.info("Deleting SF %s", sf_port_id)
        
        self._execute(*cmd)
        
        LOG.info("SF %s deleted successfully", sf_port_id)

    def add_interface(self, context, vif, instance):
        """Add a network interface to the instance using SF.

        This method:
        1. Finds an available SF number
        2. Creates the SF
        3. Configures the SF with MAC address and activates it
        4. Adds the SF representor to OVS bridge
        5. Sets the Neutron port ID as iface-id

        :param vif: VIF (Virtual Interface) object containing network
                    information
        :param instance: Instance object
        :returns: True if successful
        :raises: exception.NovaException if operation fails
        """
        port_id = vif.get('id')
        mac_address = vif.get('address')
        
        if not port_id:
            raise exception.NovaException(
                _("VIF missing 'id' field"))
        
        if not mac_address:
            raise exception.NovaException(
                _("VIF missing 'address' field"))

        # 在创建 SF 之前对 Neutron 端口做「解绑 → 改 MAC → 重新绑定」：
        # 新 MAC 使用 fa:17:3e 前缀，后缀沿用原来的后 3 个字节。
        mac_address = self._rebind_port_with_dpu_mac(
            context, instance, port_id, mac_address)
        vif["address"] = mac_address
        
        LOG.info("Adding SF interface - instance=%s port_id=%s mac=%s",
                 instance.uuid, port_id, mac_address)
        
        sf_port_id = None
        sf_num = None
        representor_name = None
        
        # Check if SF already exists (recovery scenario after compute node restart)
        existing_sf_port_id, existing_sf_num = self._find_sf_by_mac(mac_address)
        if existing_sf_port_id and existing_sf_num is not None:
            LOG.info("Found existing SF for MAC %s: port_id=%s sf_num=%d (recovery scenario)",
                     mac_address, existing_sf_port_id, existing_sf_num)
            sf_port_id = existing_sf_port_id
            sf_num = existing_sf_num
            representor_name = f"{self.representor_prefix}{sf_num}"
            
            # Verify that the existing SF belongs to the correct port
            # This prevents reusing an SF that belongs to a different instance
            try:
                cmd = ['ovs-vsctl', 'get', 'Interface', representor_name,
                       'external_ids:iface-id']
                out, _ = self._execute(*cmd)
                existing_port_id = out.strip().strip('"')
                if existing_port_id != port_id:
                    LOG.warning(
                        "SF exists but port_id mismatch: existing=%s, requested=%s. "
                        "This may indicate a conflict. The SF will be reconfigured for "
                        "the new port_id.",
                        existing_port_id, port_id)
                    # Update iface-id to match the new port_id
                    cmd = ['ovs-vsctl', 'set', 'Interface', representor_name,
                           f'external_ids:iface-id={port_id}']
                    self._execute(*cmd)
                    LOG.info("Updated iface-id for representor %s to %s",
                             representor_name, port_id)
                else:
                    LOG.debug("Verified existing SF belongs to correct port_id %s",
                             port_id)
            except exception.NovaException as exc:
                # If representor doesn't exist in OVS or can't be queried,
                # it's likely a recovery scenario where OVS state was lost
                LOG.debug("Could not verify port_id for existing SF (representor may "
                         "not be in OVS): %s. This is expected in recovery scenarios.",
                         exc)
            
            # Ensure SF is active and configured correctly
            try:
                self._configure_sf(sf_port_id, mac_address)
            except Exception as exc:
                LOG.warning("Failed to reconfigure existing SF %s: %s, continuing anyway",
                           sf_port_id, exc)
            
            # Check if representor is already in OVS, if not add it
            try:
                # Try to get OVS port info to check if it exists
                cmd = ['ovs-vsctl', 'get', 'Interface', representor_name, 'external_ids']
                try:
                    self._execute(*cmd)
                    LOG.debug("Representor %s already in OVS", representor_name)
                except exception.NovaException:
                    # Port doesn't exist, add it
                    LOG.info("Representor %s not in OVS, adding it", representor_name)
                    self._add_to_ovs(representor_name, port_id)
            except Exception as exc:
                LOG.warning("Failed to check/add representor %s to OVS: %s, adding anyway",
                           representor_name, exc)
                try:
                    self._add_to_ovs(representor_name, port_id)
                except Exception as add_exc:
                    LOG.warning("Failed to add representor %s to OVS: %s",
                               representor_name, add_exc)
        else:
            # Normal case: create new SF（失败则在范围内随机换号重试，最多共 3 次）
            failed_sfnums = set()
            last_error = None
            for attempt in range(SF_CREATE_MAX_ATTEMPTS):
                sf_port_id = None
                representor_name = None
                if attempt == 0:
                    sf_num = self._find_available_sf_num()
                else:
                    sf_num = self._random_sf_num_for_retry(failed_sfnums)
                    if sf_num is None:
                        LOG.warning(
                            "No alternative SF number for retry "
                            "(excluded=%s), instance=%s",
                            failed_sfnums, instance.uuid)
                        if last_error is not None:
                            raise last_error
                        raise exception.NovaException(
                            _("No available SF number for retry after "
                              "failed attempts."))

                try:
                    sf_port_id = self._create_sf(sf_num)
                    self._configure_sf(sf_port_id, mac_address)
                    representor_name = f"{self.representor_prefix}{sf_num}"
                    self._add_to_ovs(representor_name, port_id)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    failed_sfnums.add(sf_num)
                    LOG.warning(
                        "SF create/setup failed (attempt %(att)d/%(max)d) "
                        "instance=%(inst)s sf_num=%(num)d: %(err)s",
                        {"att": attempt + 1, "max": SF_CREATE_MAX_ATTEMPTS,
                         "inst": instance.uuid, "num": sf_num, "err": e})
                    if attempt < SF_CREATE_MAX_ATTEMPTS - 1:
                        self._cleanup_failed_sf_setup(
                            representor_name, sf_port_id)
                    else:
                        LOG.error(
                            "Failed to create SF interface for instance %s "
                            "after %d attempts: %s",
                            instance.uuid, SF_CREATE_MAX_ATTEMPTS, e)
                        with excutils.save_and_reraise_exception():
                            self._cleanup_failed_sf_setup(
                                representor_name, sf_port_id)
        
        try:
            
            # Store SF allocation info
            sf_info = {
                'sf_port_id': sf_port_id,
                'sf_num': sf_num,
                'representor_name': representor_name,
                'mac_address': mac_address,
                'port_id': port_id,
            }
            
            # Use port_id as key since instance might have multiple interfaces
            if instance.uuid not in self._sf_allocations:
                self._sf_allocations[instance.uuid] = {}
            self._sf_allocations[instance.uuid][port_id] = sf_info
            
            LOG.info("Successfully added SF interface - instance=%s port_id=%s "
                    "sf_num=%d sf_port_id=%s representor=%s",
                    instance.uuid, port_id, sf_num, sf_port_id, representor_name)
            
            return True
        except Exception as e:
            # This should not happen if we're reusing existing SF
            LOG.error("Unexpected error after SF setup for instance %s: %s",
                     instance.uuid, e)
            raise

    def remove_interface(self, vif, instance):
        """Remove a network interface from the instance using SF.

        This method:
        1. Finds the SF by MAC address
        2. Removes the representor from OVS bridge
        3. Deactivates the SF
        4. Deletes the SF

        :param vif: VIF (Virtual Interface) object
        :param instance: Instance object
        :returns: True if successful
        :raises: exception.NovaException if operation fails
        """
        port_id = vif.get('id')
        mac_address = vif.get('address')
        
        if not mac_address:
            raise exception.NovaException(
                _("VIF missing 'address' field"))
        
        LOG.info("Removing SF interface - instance=%s port_id=%s mac=%s",
                 instance.uuid, port_id, mac_address)
        
        # Try to get SF info from cache first
        sf_info = None
        if instance.uuid in self._sf_allocations:
            if port_id and port_id in self._sf_allocations[instance.uuid]:
                sf_info = self._sf_allocations[instance.uuid][port_id]
            else:
                # Search by MAC address in cache
                for cached_port_id, cached_info in \
                        self._sf_allocations[instance.uuid].items():
                    if cached_info.get('mac_address') == mac_address:
                        sf_info = cached_info
                        break
        
        # If not in cache, search by MAC address
        if not sf_info:
            LOG.debug("SF info not in cache, searching by MAC address")
            sf_port_id, sf_num = self._find_sf_by_mac(mac_address)
            
            if not sf_port_id or sf_num is None:
                LOG.warning("SF with MAC %s not found, may already be deleted",
                           mac_address)
                return True
            
            representor_name = f"{self.representor_prefix}{sf_num}"
            sf_info = {
                'sf_port_id': sf_port_id,
                'sf_num': sf_num,
                'representor_name': representor_name,
            }
        
        sf_port_id = sf_info['sf_port_id']
        representor_name = sf_info['representor_name']
        
        try:
            # Remove from OVS
            self._remove_from_ovs(representor_name)
            
            # Deactivate SF
            self._deactivate_sf(sf_port_id)
            
            # Delete SF
            self._delete_sf(sf_port_id)
            
            # Remove from cache
            if instance.uuid in self._sf_allocations:
                if port_id and port_id in self._sf_allocations[instance.uuid]:
                    self._sf_allocations[instance.uuid].pop(port_id, None)
                else:
                    # If port_id is None, try to find and remove by MAC address
                    for cached_port_id, cached_info in list(
                            self._sf_allocations[instance.uuid].items()):
                        if cached_info.get('mac_address') == mac_address:
                            self._sf_allocations[instance.uuid].pop(
                                cached_port_id, None)
                            break
                
                # Clean up empty instance entry
                if not self._sf_allocations[instance.uuid]:
                    del self._sf_allocations[instance.uuid]
            
            LOG.info("Successfully removed SF interface - instance=%s "
                    "sf_port_id=%s representor=%s",
                    instance.uuid, sf_port_id, representor_name)
            
            return True
            
        except Exception as e:
            LOG.error("Failed to remove SF interface for instance %s: %s",
                     instance.uuid, e, exc_info=True)
            # Re-raise the exception so the caller knows the operation failed
            raise exception.NovaException(
                _("Failed to remove SF interface: %s") % str(e))


class RoCESFClient(SFClient):
    """SF client for RoCE-dedicated scalable functions."""

    def __init__(self):
        super().__init__()
        self.sf_num_min = CONF.dpu.roce_sf_num_min
        self.sf_num_max = CONF.dpu.roce_sf_num_max
        self.ovs_bridge = CONF.dpu.roce_ovs_bridge
        self._roce_alloc: dict = {}
        LOG.info('RoCE SF client: sf_range=[%d,%d]  bridge=%s',
                 self.sf_num_min, self.sf_num_max, self.ovs_bridge)

    def add_roce_interface(self, instance_uuid: str, mac_address: str,
                          vlan_id: int = 0, mtu: int = 0) -> str:
        """Create (or recover) a RoCE SF, configure MAC, add to br-ex."""
        LOG.info('Adding RoCE SF  instance=%s  mac=%s  mtu=%d', instance_uuid, mac_address, mtu)
        sf_port_id, sf_num = self._find_sf_by_mac(mac_address)
        if sf_port_id and sf_num is not None:
            representor = f'{self.representor_prefix}{sf_num}'
            LOG.info('Found existing RoCE SF  port_id=%s  sf_num=%d (recovery)',
                     sf_port_id, sf_num)
            try:
                self._configure_sf(sf_port_id, mac_address)
            except Exception as exc:
                LOG.warning('Reconfigure existing RoCE SF %s failed: %s', sf_port_id, exc)
            self._ensure_in_ovs(representor, vlan_id, mtu)
        else:
            sf_port_id, sf_num, representor = self._create_roce_sf(instance_uuid, mac_address, vlan_id, mtu)
        self._roce_alloc[instance_uuid] = {
            'sf_port_id': sf_port_id,
            'sf_num': sf_num,
            'representor_name': representor,
            'mac_address': mac_address,
        }
        LOG.info('RoCE SF ready  instance=%s  sf_num=%d  representor=%s',
                 instance_uuid, sf_num, representor)
        return representor

    def remove_roce_interface(self, instance_uuid: str) -> bool:
        """Remove RoCE SF from OVS, deactivate and delete it."""
        info = self._roce_alloc.get(instance_uuid)
        if not info:
            LOG.debug('No cached RoCE SF for %s, nothing to remove', instance_uuid)
            return True
        sf_port_id = info['sf_port_id']
        representor = info['representor_name']
        for step, fn, args in [
            ('OVS del-port',  self._remove_from_ovs, (representor,)),
            ('SF deactivate', self._deactivate_sf,    (sf_port_id,)),
            ('SF delete',     self._delete_sf,         (sf_port_id,)),
        ]:
            try:
                fn(*args)
            except Exception as exc:
                LOG.warning('RoCE teardown step [%s] failed: %s', step, exc)
        self._roce_alloc.pop(instance_uuid, None)
        try:
            self._execute('ovs-vsctl', 'clear', 'port', 'roce-dhcp', 'tag')
            LOG.info('Cleared roce-dhcp VLAN tag')
        except Exception as exc:
            LOG.warning('Failed to clear roce-dhcp VLAN: %s', exc)
        LOG.info('RoCE SF removed  instance=%s  sf_port_id=%s', instance_uuid, sf_port_id)
        return True

    def _create_roce_sf(self, instance_uuid, mac_address, vlan_id: int = 0, mtu: int = 0):
        """Create a new RoCE SF, retrying with different sfnums on failure."""
        import time as _time
        failed: set = set()
        last_err = None
        sf_port_id = representor = None
        for attempt in range(SF_CREATE_MAX_ATTEMPTS):
            if attempt == 0:
                sf_num = self._find_available_sf_num()
            else:
                sf_num = self._random_sf_num_for_retry(failed)
                if sf_num is None:
                    if last_err:
                        raise last_err
                    raise exception.NovaException(
                        _("No available RoCE SF number after retries"))
            try:
                sf_port_id = self._create_sf(sf_num)
                self._configure_sf(sf_port_id, mac_address)
                representor = f'{self.representor_prefix}{sf_num}'
                self._ensure_in_ovs(representor, vlan_id, mtu)
                return sf_port_id, sf_num, representor
            except Exception as exc:
                last_err = exc
                failed.add(sf_num)
                LOG.warning(
                    'RoCE SF create attempt %d/%d failed  instance=%s  sf_num=%d: %s',
                    attempt + 1, SF_CREATE_MAX_ATTEMPTS, instance_uuid, sf_num, exc)
                if attempt < SF_CREATE_MAX_ATTEMPTS - 1:
                    self._cleanup_failed_sf_setup(representor, sf_port_id)
                    sf_port_id = representor = None
                else:
                    with excutils.save_and_reraise_exception():
                        self._cleanup_failed_sf_setup(representor, sf_port_id)

    def _wait_for_representor(self, representor: str, timeout: int = 15):
        """Poll /sys/class/net until representor interface appears."""
        import os as _os, time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if _os.path.exists(f'/sys/class/net/{representor}'):
                return
            _time.sleep(0.5)
        raise exception.NovaException(
            _('Timed out waiting for representor %(rep)s (%(timeout)ds)')
            % {'rep': representor, 'timeout': timeout})

    def _ensure_in_ovs(self, representor: str, vlan_id: int = 0, mtu: int = 0):
        """Add representor to br-ex, set VLAN access tag and MTU."""
        try:
            self._execute('ovs-vsctl', 'get', 'Interface', representor, 'name')
            LOG.debug('RoCE representor %s already in %s', representor, self.ovs_bridge)
        except exception.NovaException:
            self._wait_for_representor(representor)
            self._execute('ovs-vsctl', 'add-port', self.ovs_bridge, representor)
            LOG.info('Added RoCE representor %s to %s', representor, self.ovs_bridge)
        if mtu:
            self._execute('ip', 'link', 'set', representor, 'mtu', str(mtu))
            LOG.info('Set MTU %d on RoCE representor %s', mtu, representor)
        if vlan_id:
            self._execute('ovs-vsctl', 'set', 'port', representor, f'tag={vlan_id}')
            self._execute('ovs-vsctl', 'set', 'port', 'roce-dhcp', f'tag={vlan_id}')
            LOG.info('Set VLAN %d on %s and roce-dhcp', vlan_id, representor)
