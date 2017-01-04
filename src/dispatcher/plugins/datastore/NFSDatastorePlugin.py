#
# Copyright 2016 iXsystems, Inc.
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
import bsd
import errno
from lib.system import system
from task import Task, TaskDescription, Provider
from freenas.dispatcher.rpc import SchemaHelper as h
from freenas.dispatcher.rpc import RpcException, private, accepts, returns, generator, description


@description('Provides information about NFS VM datastores')
class NFSDatastoreProvider(Provider):
    @private
    @generator
    def discover(self):
        return

    @private
    @accepts(str, str)
    @returns(str)
    @description('Converts remote NFS VM datastore path to local filesystem path')
    def get_filesystem_path(self, datastore_id, datastore_path):
        ds = self.datastore.get_by_id('vm.datastores', datastore_id)
        if ds['type'] != 'nfs':
            raise RpcException(errno.EINVAL, 'Invalid datastore type')

        return os.path.join('/nfs', ds['name'], datastore_path)

    @private
    @accepts(str, str)
    @returns(bool)
    @description('Checks for directory existence in NFS VM datastore')
    def directory_exists(self, datastore_id, datastore_path):
        ds = self.datastore.get_by_id('vm.datastores', datastore_id)
        if ds['type'] != 'nfs':
            raise RpcException(errno.EINVAL, 'Invalid datastore type')

        return os.path.exists(os.path.join('/nfs', ds['name'], datastore_path))

    @private
    @accepts(str)
    @returns(h.array(str))
    @description('Returns list of resources which have to be locked to safely perform VM datastore operations')
    def get_resources(self, datastore_id):
        return ['system']

    @private
    @generator
    @accepts(str, str)
    @description('Returns a list of snapshots on a given VM datastore path')
    def get_snapshots(self, datastore_id, path):
        return


@accepts(h.ref('vm-datastore'))
@returns(str)
@description('Creates a NFS VM datastore')
class NFSDatastoreCreateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Creating a NFS datastore'

    def describe(self, datastore):
        return TaskDescription('Creating the NFS datastore {name}', name=datastore['name'])

    def verify(self, datastore):
        return ['system']

    def run(self, datastore):
        mount(datastore['name'], datastore['properties'])


@accepts(str, h.ref('vm-datastore'))
@returns(str)
@description('Updates a NFS VM datastore')
class NFSDatastoreUpdateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Updating a NFS datastore'

    def describe(self, id, updated_fields):
        ds = self.datastore.get_by_id('vm.datastores', id) or {}
        return TaskDescription('Updating the NFS datastore {name}', name=ds.get('name', ''))

    def verify(self, id, updated_fields):
        return ['system']

    def run(self, id, updated_fields):
        ds = self.datastore.get_by_id('vm.datastores', id)
        umount(ds['name'])
        mount(ds['name'], ds['properties'])


@accepts(str)
@description('Deletes a NFS VM datastore')
class NFSDatastoreDeleteTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Deleting a NFS datastore'

    def describe(self, id):
        ds = self.datastore.get_by_id('vm.datastores', id) or {}
        return TaskDescription('Deleting the NFS datastore {name}', name=ds.get('name', ''))

    def verify(self, id):
        return ['system']

    def run(self, id):
        ds = self.datastore.get_by_id('vm.datastores', id)
        umount(ds['name'])


def _metadata():
    return {
        'type': 'datastore',
        'driver': 'nfs',
        'clones': False,
        'snapshots': False
    }


def mount(name, properties):
    path = os.path.join('/nfs', name)
    if not os.path.isdir(path):
        os.makedirs(path)

    try:
        stat = bsd.statfs(path)
        if stat.fstype == 'nfs':
            umount(name)
    except:
        pass

    # XXX: Couldn't figure out how to do that with py-bsd's nmount
    system('/sbin/mount_nfs', '-osoft,intr,retrans=1,timeout=100', '{address}:{path}'.format(**properties), path)


def umount(name):
    bsd.unmount(os.path.join('/nfs', name))


def _depends():
    return ['LocalDatastorePlugin']


def _init(dispatcher, plugin):
    plugin.register_schema_definition('vm-datastore-nfs-version', {
        'type': 'string',
        'enum': ['NFSV3', 'NFSV4']
    })

    plugin.register_schema_definition('vm-datastore-properties-nfs', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            '%type': {'enum': ['vm-datastore-nfs']},
            'address': {'type': 'string'},
            'path': {'type': 'string'},
            'version': {'$ref': 'vm-datastore-nfs-version'}
        }
    })

    plugin.register_provider('vm.datastore.nfs', NFSDatastoreProvider)
    plugin.register_task_handler('vm.datastore.nfs.create', NFSDatastoreCreateTask)
    plugin.register_task_handler('vm.datastore.nfs.update', NFSDatastoreUpdateTask)
    plugin.register_task_handler('vm.datastore.nfs.delete', NFSDatastoreDeleteTask)
    plugin.register_task_alias('vm.datastore.nfs.directory.create', 'vm.datastore.local.directory.create')
    plugin.register_task_alias('vm.datastore.nfs.directory.delete', 'vm.datastore.local.directory.delete')
    plugin.register_task_alias('vm.datastore.nfs.directory.rename', 'vm.datastore.local.directory.rename')
    plugin.register_task_alias('vm.datastore.nfs.block_device.create', 'vm.datastore.local.directory.create')
    plugin.register_task_alias('vm.datastore.nfs.block_device.delete', 'vm.datastore.local.directory.delete')
    plugin.register_task_alias('vm.datastore.nfs.block_device.rename', 'vm.datastore.local.directory.rename')
    plugin.register_task_alias('vm.datastore.nfs.block_device.resize', 'vm.datastore.local.directory.resize')

    if not os.path.isdir('/nfs'):
        os.mkdir('/nfs')

    def init(args):
        for i in dispatcher.datastore.query('vm.datastores', ('type', '=', 'nfs')):
            mount(i['name'], i['properties'])

    dispatcher.register_event_handler_once('network.changed', init)
