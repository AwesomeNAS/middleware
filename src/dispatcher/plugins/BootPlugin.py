#+
# Copyright 2014 iXsystems, Inc.
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
import sys
import errno
from freenas.utils.query import wrap
from task import Provider, Task, ProgressTask, VerifyException, TaskException, query
from freenas.dispatcher.rpc import accepts, returns, description, SchemaHelper as h

sys.path.append('/usr/local/lib')
from freenasOS.Update import (
    ListClones, FindClone, RenameClone, ActivateClone, DeleteClone, CreateClone
)


@description("Provides information on Boot Environments")
class BootEnvironmentsProvider(Provider):

    @query('boot-environment')
    def query(self, filter=None, params=None):
        def extend(obj):
            nr = obj['active']
            obj['active'] = 'N' in nr
            obj['on_reboot'] = 'R' in nr
            obj['id'] = obj.pop('name')
            return obj

        clones = list(map(extend, ListClones()))
        return wrap(clones).query(*(filter or []), **(params or {}))


@description(
    "Creates a clone of the current Boot Environment or of the specified source (optional)"
 )
@accepts(str, h.any_of(str, None))
class BootEnvironmentCreate(Task):
    def verify(self, newname, source=None):
        return ['system']

    def run(self, newname, source=None):
        if not CreateClone(newname, bename=source):
            raise TaskException(errno.EIO, 'Cannot create the {0} boot environment'.format(newname))


@description("Activates the specified Boot Environment to be selected on reboot")
@accepts(str)
class BootEnvironmentActivate(Task):
    def verify(self, name):
        be = FindClone(name)
        if not be:
            raise VerifyException(errno.ENOENT, 'Boot environment {0} not found'.format(name))

        return ['system']

    def run(self, name):
        if not ActivateClone(name):
            raise TaskException(errno.EIO, 'Cannot activate the {0} boot environment'.format(name))


@description("Renames the given Boot Environment with the alternate name provieded")
@accepts(str, str)
class BootEnvironmentRename(Task):
    def verify(self, oldname, newname):
        be = FindClone(oldname)
        if not be:
            raise VerifyException(errno.ENOENT, 'Boot environment {0} not found'.format(oldname))

        return ['system']

    def run(self, oldname, newname):
        if not RenameClone(oldname, newname):
            raise TaskException(errno.EIO, 'Cannot rename the {0} boot evironment'.format(newname))


@description("Deletes the given Boot Environments. Note: It cannot delete an activated BE")
@accepts(h.array(str))
class BootEnvironmentsDelete(Task):
    def verify(self, names):
        for n in names:
            be = FindClone(n)
            if not be:
                raise VerifyException(errno.ENOENT, 'Boot environment {0} not found'.format(n))

        return ['system']

    def run(self, names):
        for n in names:
            if not DeleteClone(n):
                raise TaskException(errno.EIO, 'Cannot delete the {0} boot environment'.format(n))


@description("Attaches the given Disk to the Boot Pool")
@accepts(str, str)
class BootAttachDisk(ProgressTask):
    def verify(self, guid, disk):
        boot_pool_name = self.configstore.get('system.boot_pool_name')
        return ['zpool:{0}'.format(boot_pool_name), 'disk:{0}'.format(disk)]

    def run(self, guid, disk):
        # Format disk
        self.join_subtasks(self.run_subtask('disk.format.boot', disk))
        self.set_progress(30)

        # Attach disk to the pool
        boot_pool_name = self.configstore.get('system.boot_pool_name')
        self.join_subtasks(self.run_subtask('zfs.pool.extend', boot_pool_name, None, [{
            'target_guid': guid,
            'vdev': {
                'type': 'disk',
                'path': os.path.join('/dev', disk)
            }
        }]))

        self.set_progress(80)

        # Install grub
        self.join_subtasks(self.run_subtask('disk.install_bootloader', disk))
        self.set_progress(100)


@description("Detaches the specified Disk fron the Boot Pool (not functional yet)")
@accepts(str)
class BootDetachDisk(Task):
    def verify(self, disk):
        pass

    def run(self, disk):
        pass


def _depends():
    return ['DiskPlugin', 'ZfsPlugin']


def _init(dispatcher, plugin):
    plugin.register_schema_definition('boot-environment', {
        'type': 'object',
        'properties': {
            'id': {'type': 'string'},
            'realname': {'type': 'string'},
            'active': {'type': 'boolean'},
            'on_reboot': {'type': 'boolean'},
            'mountpoint': {'type': 'string'},
            'space': {'type': 'integer'},
            'created': {'type': 'string'}
        }
    })

    plugin.register_provider('boot.environment', BootEnvironmentsProvider)
    plugin.register_task_handler('boot.environment.create', BootEnvironmentCreate)
    plugin.register_task_handler('boot.environment.activate', BootEnvironmentActivate)
    plugin.register_task_handler('boot.environment.rename', BootEnvironmentRename)
    plugin.register_task_handler('boot.environment.delete', BootEnvironmentsDelete)

    plugin.register_task_handler('boot.disk.attach', BootAttachDisk)
    plugin.register_task_handler('boot.disk.detach', BootDetachDisk)
