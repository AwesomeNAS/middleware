#+
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
import uuid
import logging
import shutil
import time
import libzfs
from dispatcher.rpc import RpcException, accepts, returns, description, private
from dispatcher.rpc import SchemaHelper as h
from task import Task, Provider
from fnutils.copytree import copytree

SYSTEM_DIR = '/var/db/system'
LINK_DIRS = {
    'riak': {"owner": "riak", "group": "riak", '/var/db/riak'},
    'riak-cs': {"owner": "riakcs", "group": "riak", '/var/db/riak-cs'},
    'samba': '/var/db/samba4',
    'stanchion': {"owner": "stanchion", "group": "riak", '/var/db/stanchion'},
    'log': '/var/log'
}
SKELETON_DIRS = {
    'log': {
        'riak': {"owner": "riak", "group": "riak"},
        'riak-cs': {"owner": "riakcs", "group": "riak"},
        'stanchion': {"owner": "stanchion", "group": "riak"},
        'samba': ['private'],
    },
}

logger = logging.getLogger('SystemDataset')


def link_directories(dispatcher):
    for name, directory in LINK_DIRS.items():
        target = dispatcher.call_sync('system_dataset.request_directory', name)
        if os.path.islink(directory):
            if os.readlink(directory) == target:
                # properly linked already
                continue

        shutil.move(directory, directory + '.{0}.bak'.format(int(time.time())))
        os.symlink(target, directory)

        if name in SKELETON_DIRS:
            for i in SKELETON_DIRS[name]:
                try:
                    os.mkdir(os.path.join(target, i))
                except OSError, err:
                    if err.errno != errno.EEXIST:
                        logger.warning('Cannot create skeleton directory {0}: {1}'.format(
                            os.path.join(target, i),
                            str(err))
                        )


def create_system_dataset(dispatcher, dsid, pool):
    logger.warning('Creating system dataset on pool {0}'.format(pool))
    zfs = libzfs.ZFS()
    pool = zfs.get(pool)

    try:
        ds = zfs.get_dataset('{0}/.system-{1}'.format(pool.name, dsid))
    except libzfs.ZFSException:
        pool.create('{0}/.system-{1}'.format(pool.name, dsid), {'mountpoint': 'none'})
        ds = zfs.get_dataset('{0}/.system-{1}'.format(pool.name, dsid))

    try:
        ds.properties['canmount'].value = 'noauto'
        ds.properties['mountpoint'].value = SYSTEM_DIR
    except libzfs.ZFSException, err:
        logger.warning('Cannot set properties on .system dataset: {0}', str(err))


def remove_system_dataset(dispatcher, dsid, pool):
    logger.warning('Removing system dataset from pool {0}'.format(pool))
    zfs = libzfs.ZFS()
    pool = zfs.get(pool)
    try:
        ds = zfs.get_dataset('{0}/.system-{1}'.format(pool.name, dsid))
        ds.umount(force=True)
        ds.delete()
    except libzfs.ZFSException:
        pass


def mount_system_dataset(dispatcher, dsid, pool, path):
    logger.warning('Mounting system dataset from pool {0} on {1}'.format(pool, path))
    zfs = libzfs.ZFS()
    pool = zfs.get(pool)
    try:
        ds = zfs.get_dataset('{0}/.system-{1}'.format(pool.name, dsid))
        if ds.mountpoint:
            logger.warning('.system dataset already mounted')
            return

        ds.properties['mountpoint'].value = path
        ds.mount()
    except libzfs.ZFSException, err:
        logger.error('Cannot mount .system dataset on pool {0}: {1}'.format(pool.name, str(err)))
        raise err


def umount_system_dataset(dispatcher, dsid, pool):
    zfs = libzfs.ZFS()
    pool = zfs.get(pool)
    try:
        ds = zfs.get_dataset('{0}/.system-{1}'.format(pool.name, dsid))
        ds.umount(force=True)
        return
    except libzfs.ZFSException, err:
        logger.error('Cannot unmount .system dataset on pool {0}: {1}'.format(pool.name, str(err)))


def move_system_dataset(dispatcher, dsid, src_pool, dst_pool):
    logger.warning('Migrating system dataset from pool {0} to {1}'.format(src_pool, dst_pool))
    tmpath = os.tempnam('/tmp')
    create_system_dataset(dispatcher, dsid, dst_pool)
    mount_system_dataset(dispatcher, dsid, dst_pool, tmpath)

    try:
        copytree(SYSTEM_DIR, tmpath)
    except shutil.Error, err:
        logger.warning('Following errors were encountered during migration:')
        for i in err:
            logger.warning('{0} -> {1}: {2}'.format(*i[0]))

    umount_system_dataset(dispatcher, dsid, dst_pool)
    umount_system_dataset(dispatcher, dsid, src_pool)
    mount_system_dataset(dispatcher, dsid, dst_pool, SYSTEM_DIR)
    remove_system_dataset(dispatcher, dsid, src_pool)


class SystemDatasetProvider(Provider):
    @private
    @description("Initializes the .system dataset")
    @accepts()
    @returns()
    def init(self):
        pool = self.configstore.get('system.dataset.pool')
        dsid = self.configstore.get('system.dataset.id')
        create_system_dataset(self.dispatcher, dsid, pool)
        mount_system_dataset(self.dispatcher, dsid, pool, SYSTEM_DIR)
        link_directories(self.dispatcher)

    @private
    @description("Creates directory in .system dataset and returns reference to it")
    @accepts(str)
    @returns(str)
    def request_directory(self, name):
        path = os.path.join(SYSTEM_DIR, name)
        if os.path.exists(path):
            if os.path.isdir(path):
                return path

            raise RpcException(errno.EPERM, 'Cannot grant directory {0}'.format(name))

        os.mkdir(path)
        return path

    @description("Returns current .system dataset parameters")
    @returns(h.object())
    def status(self):
        return {
            'id': self.configstore.get('system.dataset.id'),
            'pool': self.configstore.get('system.dataset.pool')
        }


@description("Updates .system dataset configuration")
@accepts(str)
class SystemDatasetConfigure(Task):
    def verify(self, pool):
        return ['system']

    def run(self, pool):
        status = self.dispatcher.call_sync('system_dataset.status')
        if status['pool'] != pool:
            move_system_dataset(self.dispatcher, self.configstore.get('system.dataset.id'), status['pool'], pool)

        self.configstore.set('system.dataset.pool', pool)


def _depends():
    return ['ZfsPlugin', 'VolumePlugin']


def _init(dispatcher, plugin):
    def on_volumes_changed(args):
        if args['operation'] == 'create':
            pass

    def volume_pre_destroy(args):
        # Evacuate .system dataset from the pool
        if dispatcher.configstore.get('system.dataset.pool') == args['name']:
            dispatcher.call_task_sync('system_dataset.configure', 'freenas-boot')

    if not dispatcher.configstore.get('system.dataset.id'):
        dsid = uuid.uuid4().hex[:8]
        dispatcher.configstore.set('system.dataset.id', dsid)
        logger.info('New system dataset ID: {0}'.format(dsid))

    plugin.register_event_handler('volumes.changed', on_volumes_changed)
    plugin.attach_hook('volumes.pre_destroy', volume_pre_destroy)
    plugin.attach_hook('volumes.pre_detach', volume_pre_destroy)
    plugin.register_provider('system_dataset', SystemDatasetProvider)
    plugin.register_task_handler('system_dataset.configure', SystemDatasetConfigure)

    plugin.register_hook('system_dataset.pre_detach')
    plugin.register_hook('system_dataset.pre_attach')

    dispatcher.call_sync('system_dataset.init')
