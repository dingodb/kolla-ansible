# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from string import Template
from time import sleep

import dbus


TEMPLATE = '''# ${service_name}
# autogenerated by Kolla-Ansible

[Unit]
Description=${engine} ${service_name}
After=${deps}
Requires=${deps}
StartLimitInterval=${restart_timeout}
StartLimitBurst=${restart_retries}

[Service]
ExecStart=/usr/bin/${engine} start -a ${name}
ExecStop=/usr/bin/${engine} stop ${name} -t ${graceful_timeout}
Restart=${restart_policy}
RestartSec=${restart_duration}

[Install]
WantedBy=multi-user.target
'''


class SystemdWorker(object):
    def __init__(self, params):
        name = params.get('name', None)

        # systemd is not needed
        if not name:
            return None

        container_engine = params.get('container_engine')
        if container_engine == 'docker':
            dependencies = 'docker.service'
        else:
            dependencies = 'network-online.target'

        restart_policy = params.get('restart_policy', 'no')
        if restart_policy == 'unless-stopped':
            restart_policy = 'always'

        # NOTE(hinermar): duration * retries should be less than timeout
        # otherwise service will indefinitely try to restart.
        # Also, correct timeout and retries values should probably be
        # checked at the module level inside kolla_container.py
        restart_timeout = params.get('client_timeout', 120)
        restart_retries = params.get('restart_retries', 10)
        restart_duration = (restart_timeout // restart_retries) - 1

        # container info
        self.container_dict = dict(
            name=name,
            service_name='kolla-' + name + '-container.service',
            engine=container_engine,
            deps=dependencies,
            graceful_timeout=params.get('graceful_timeout'),
            restart_policy=restart_policy,
            restart_timeout=restart_timeout,
            restart_retries=restart_retries,
            restart_duration=restart_duration
        )

        # systemd
        self.manager = self.get_manager()
        self.job_mode = 'replace'
        self.sysdir = '/etc/systemd/system/'

        # templating
        self.template = Template(TEMPLATE)

    def get_manager(self):
        sysbus = dbus.SystemBus()
        systemd1 = sysbus.get_object(
            'org.freedesktop.systemd1',
            '/org/freedesktop/systemd1'
        )
        return dbus.Interface(systemd1, 'org.freedesktop.systemd1.Manager')

    def start(self):
        if self.perform_action(
            'StartUnit',
            self.container_dict['service_name'],
            self.job_mode
        ):
            return self.wait_for_unit(self.container_dict['restart_timeout'])
        return False

    def restart(self):
        if self.perform_action(
            'RestartUnit',
            self.container_dict['service_name'],
            self.job_mode
        ):
            return self.wait_for_unit(self.container_dict['restart_timeout'])
        return False

    def stop(self):
        if self.perform_action(
            'StopUnit',
            self.container_dict['service_name'],
            self.job_mode
        ):
            return self.wait_for_unit(
                self.container_dict['restart_timeout'],
                state='dead'
            )
        return False

    def reload(self):
        return self.perform_action(
            'Reload',
            self.container_dict['service_name'],
            self.job_mode
        )

    def enable(self):
        return self.perform_action(
            'EnableUnitFiles',
            [self.container_dict['service_name']],
            False,
            True
        )

    def perform_action(self, function, *args):
        try:
            getattr(self.manager, function)(*args)
            return True
        except Exception:
            return False

    def check_unit_file(self):
        return os.path.isfile(
            self.sysdir + self.container_dict['service_name']
        )

    def check_unit_change(self, new_content=''):
        if not new_content:
            new_content = self.generate_unit_file()

        if self.check_unit_file():
            with open(
                self.sysdir + self.container_dict['service_name'], 'r'
            ) as f:
                curr_content = f.read()

                # return whether there was change in the unit file
                return curr_content != new_content

        return True

    def generate_unit_file(self):
        return self.template.substitute(self.container_dict)

    def create_unit_file(self):
        file_content = self.generate_unit_file()

        if self.check_unit_change(file_content):
            with open(
                self.sysdir + self.container_dict['service_name'], 'w'
            ) as f:
                f.write(file_content)

            self.reload()
            self.enable()
            return True

        return False

    def remove_unit_file(self):
        if self.check_unit_file():
            os.remove(self.sysdir + self.container_dict['service_name'])
            self.reload()

            return True
        else:
            return False

    def get_unit_state(self):
        unit_list = self.manager.ListUnits()

        for service in unit_list:
            if str(service[0]) == self.container_dict['service_name']:
                return str(service[4])

        return None

    def wait_for_unit(self, timeout, state='running'):
        delay = 5
        elapsed = 0

        while True:
            if self.get_unit_state() == state:
                return True
            elif elapsed > timeout:
                return False
            else:
                sleep(delay)
                elapsed += delay
