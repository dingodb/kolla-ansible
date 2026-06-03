"""HTTP client for the RoCE DHCP sidecar.

Deployed to nova/virt/dpu/ on the DPU node alongside driver.py.
Communicates with the local roce_sidecar.py via HTTP.
"""

import json
import urllib.parse
import urllib.request
import urllib.error

from oslo_log import log as logging

import nova.conf

LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF


class RoCEClient:
    """Thin HTTP client for /roce/* endpoints on the local sidecar."""

    def __init__(self):
        self._url = CONF.dpu.roce_sidecar_url.rstrip('/')

    def register(self, instance_uuid, flavor_name='', project_id='',
                 node_host='', node_type='dpu'):
        """POST /roce/register.

        Returns the parsed JSON dict from the sidecar.  The sidecar returns
        ``{'roce_enabled': False}`` (HTTP 200) when the flavor has no
        ``hw:roce_enabled`` extra-spec, so callers must check that field.

        Raises urllib.error.HTTPError / OSError on transport failures.
        """
        payload = json.dumps({
            'instance_uuid': instance_uuid,
            'flavor_name': flavor_name,
            'project_id': project_id,
            'node_host': node_host,
            'node_type': node_type,
        }).encode()
        req = urllib.request.Request(
            f'{self._url}/roce/register',
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            LOG.error('RoCE register HTTP %d: %s  uuid=%s', exc.code, body,
                      instance_uuid)
            raise

    def get_allocation(self, instance_uuid):
        """GET /roce/instance/<uuid>.

        Returns the parsed JSON dict if an allocation exists, or None on 404.
        Used during recovery to check whether a RoCE SF needs to be rebuilt.
        """
        try:
            url = f'{self._url}/roce/instance/{instance_uuid}'
            with urllib.request.urlopen(url, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            LOG.warning('RoCE get_allocation HTTP %d  uuid=%s', exc.code,
                        instance_uuid)
            return None
        except Exception:
            LOG.exception('RoCE get_allocation error  uuid=%s', instance_uuid)
            return None

    def deregister(self, instance_uuid, caller_host=''):
        """DELETE /roce/instance/<uuid>.

        caller_host: 传入本节点标识，冷迁移时 sidecar 据此判断是否跳过 DB 删除。
        Best-effort: HTTP 404 is silently treated as success (already gone).
        Returns empty dict on any other error rather than raising.
        """
        try:
            url = f'{self._url}/roce/instance/{instance_uuid}'
            if caller_host:
                url += f'?caller_host={urllib.parse.quote(caller_host)}'
            req = urllib.request.Request(
                url,
                method='DELETE',
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {'ok': True, 'note': 'not found'}
            LOG.warning('RoCE deregister HTTP %d  uuid=%s', exc.code,
                        instance_uuid)
            return {}
        except Exception:
            LOG.exception('RoCE deregister error  uuid=%s', instance_uuid)
            return {}

