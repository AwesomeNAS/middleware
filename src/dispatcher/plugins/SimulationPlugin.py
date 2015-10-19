#
# Copyright 2015 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import os
import errno
from task import Task, TaskStatus, Provider, TaskException, VerifyException
from dispatcher.rpc import RpcException, description, accepts, returns, private
from dispatcher.rpc import SchemaHelper as h
from fnutils import normalize


SIMULATOR_ROOT = '/var/tmp/simulator'


class FakeDisksProvider(Provider):
    def query(self, filter=None, params=None):
        return self.datastore.query('simulator.disks', *(filter or []), **(params or {}))


@accepts(
    h.all_of(
        h.ref('simulated-disk'),
        h.required('id')
    )
)
class CreateFakeDisk(Task):
    def verify(self, disk):
        return ['system']

    def run(self, disk):
        normalize(disk, {
            'vendor': 'FreeNAS',
            'path': os.path.join(SIMULATOR_ROOT, disk['id']),
            'model': 'Virtual Disk',
            'serial': self.dispatcher.call_sync('shares.iscsi.generate_serial'),
            'block_size': 512,
            'ssd': False
        })

        disk['naa'] = self.dispatcher.call_sync('shares.iscsi.generate_naa')

        open(disk['path'], 'a').close()
        self.datastore.insert('simulator.disks', disk)
        self.dispatcher.call_sync('etcd.generation.generate_group', 'ctl')
        self.dispatcher.call_sync('services.reload', 'ctl')


class ConfigureFakeDisk(Task):
    def verify(self, id, updated_params):
        if not self.datastore.exists('simulator.disks', ('id', '=', id)):
            raise VerifyException(errno.ENOENT, 'Disk {0} not found'.format(id))

        return ['system']

    def run(self, id, updated_params):
        disk = self.datastore.query('simulator.disks', id)
        disk.update(updated_params)
        self.datastore.insert('simulator.disks', id, disk)
        self.dispatcher.call_sync('etcd.generate_group', 'ctl')
        self.dispatcher.call_sync('services.reload', 'ctl')


class DeleteFakeDisk(Task):
    def verify(self, id):
        if not self.datastore.exists('simulator.disks', ('id', '=', id)):
            raise VerifyException(errno.ENOENT, 'Disk {0} not found'.format(id))

        return ['system']

    def run(self, id):
        self.datastore.delete('simulator.disks', id)
        self.dispatcher.call_sync('etcd.generate_group', 'ctl')
        self.dispatcher.call_sync('services.reload', 'ctl')


def _init(dispatcher, plugin):
    plugin.register_schema_definition('simulated-disk', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'id': {'type': 'string'},
            'path': {'type': 'string'},
            'mediasize': {'type': 'integer'},
            'vendor': {'type': 'string'},
            'model': {'type': 'string'},
            'serial': {'type': 'string'},
            'block_size': {'type': 'integer'},
            'rpm': {
                'type': 'string',
                'enum': ['UNKNOWN', 'SSD', '5400', '7200', '10000', '15000']
            }
        }
    })

    if not os.path.isdir(SIMULATOR_ROOT):
        os.mkdir(SIMULATOR_ROOT)

    plugin.register_provider('simulator.disks', FakeDisksProvider)
    plugin.register_task_handler('simulator.disks.create', CreateFakeDisk)
    plugin.register_task_handler('simulator.disks.update', ConfigureFakeDisk)
    plugin.register_task_handler('simulator.disks.delete', DeleteFakeDisk)
