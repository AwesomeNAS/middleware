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


import errno
import pwd
import grp
import os
import stat
import bsd
from bsd import acl
from freenas.dispatcher.rpc import (
    RpcException, description, accepts, returns, pass_sender, private
)
from freenas.dispatcher.rpc import SchemaHelper as h
from task import Provider, Task, TaskStatus, TaskWarning, VerifyException, TaskException, TaskDescription
from auth import FileToken
from freenas.utils.permissions import modes_to_oct, get_type


@description("Provides informations filesystem structure")
class FilesystemProvider(Provider):
    @description("Lists contents of given directory")
    @accepts(str)
    @returns(h.array(h.ref('directory')))
    def list_dir(self, path):
        result = []
        if not os.path.isdir(path):
            raise RpcException(errno.ENOENT, 'Path {0} is not a directory'.format(path))

        for i in os.listdir(path):
            try:
                st = os.stat(os.path.join(path, i))
            except OSError:
                continue

            item = {
                'name': i,
                'type': get_type(st),
                'size': st.st_size,
                'modified': st.st_mtime
            }

            result.append(item)

        return result

    @accepts(str)
    @returns(h.ref('stat'))
    def stat(self, path):
        try:
            st = os.stat(path)
            a = acl.ACL(file=path)
        except OSError as err:
            raise RpcException(err.errno, str(err))

        try:
            username = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            username = None

        try:
            groupname = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            groupname = None

        return {
            'path': path,
            'type': get_type(st),
            'atime': st.st_atime,
            'mtime': st.st_mtime,
            'ctime': st.st_ctime,
            'uid': st.st_uid,
            'user': username,
            'gid': st.st_gid,
            'group': groupname,
            'permissions': {
                'acls': a.__getstate__(),
                'user': username,
                'group': groupname,
                'modes': {
                    'value': st.st_mode & 0o777,
                    'user': {
                        'read': bool(st.st_mode & stat.S_IRUSR),
                        'write': bool(st.st_mode & stat.S_IWUSR),
                        'execute': bool(st.st_mode & stat.S_IXUSR)
                    },
                    'group': {
                        'read': bool(st.st_mode & stat.S_IRGRP),
                        'write': bool(st.st_mode & stat.S_IWGRP),
                        'execute': bool(st.st_mode & stat.S_IXGRP)
                    },
                    'others': {
                        'read': bool(st.st_mode & stat.S_IROTH),
                        'write': bool(st.st_mode & stat.S_IWOTH),
                        'execute': bool(st.st_mode & stat.S_IXOTH)
                    },
                }
            }
        }

    @pass_sender
    @accepts(str)
    @returns(str)
    def download(self, path, sender):
        try:
            f = open(path, 'rb')
        except OSError as e:
            raise RpcException(e.errno, e)

        token = self.dispatcher.token_store.issue_token(FileToken(
            user=sender.user,
            lifetime=60,
            direction='download',
            file=f,
            size=os.path.getsize(path)
        ))

        return token

    @pass_sender
    @accepts(str, int, str)
    @returns(str)
    def upload(self, dest_path, size, mode, sender):
        try:
            f = open(dest_path, 'wb')
        except OSError as e:
            raise RpcException(e.errno, e)

        token = self.dispatcher.token_store.issue_token(FileToken(
            user=sender.user,
            lifetime=60,
            direction='upload',
            file=f,
            size=size
        ))

        return token

    @accepts(str)
    @returns(h.array(h.ref('open-file')))
    def get_open_files(self, path):
        result = []
        for proc in bsd.getprocs(bsd.ProcessLookupPredicate.PROC):
            for f in proc.files:
                if not f.path:
                    continue

                if f.path.startswith(path):
                    result.append({
                        'pid': proc.pid,
                        'process_name': proc.command,
                        'path': f.path
                    })

        return result


@accepts(str)
@private
class DownloadFileTask(Task):
    def verify(self, connection):
        return []

    def run(self, connection):
        self.connection = connection
        self.connection.done.wait()

    def get_status(self):
        if not self.connection:
            return TaskStatus(0)

        percentage = (self.connection.bytes_done / self.connection.bytes_total) * 100
        return TaskStatus(percentage)


@accepts(str)
@private
class UploadFileTask(Task):
    def verify(self, connection):
        return []

    def run(self, connection):
        self.connection = connection
        self.connection.done.wait()

    def get_status(self):
        if not self.connection:
            return TaskStatus(0)

        percentage = (self.connection.bytes_done / self.connection.bytes_total) * 100
        return TaskStatus(percentage)


@accepts(str, h.ref('permissions'), bool)
class SetPermissionsTask(Task):
    def verify(self, path, permissions, recursive=False):
        if not os.path.exists(path):
            raise VerifyException(errno.ENOENT, 'Path {0} does not exist'.format(path))

        if recursive and not os.path.isdir(path):
            raise VerifyException(errno.EINVAL, 'Recursive specified, but {0} is not directory'.format(path))

        try:
            pool, ds, rest = self.dispatcher.call_sync('volume.decode_path', path)
            return ['zfs:{0}'.format(ds)]
        except RpcException:
            return []

    def run(self, path, permissions, recursive=False):
        if permissions.get('user') or permissions.get('group'):
            user = permissions.get('user')
            group = permissions.get('group')
            uid = gid = -1

            if user:
                try:
                    uid = pwd.getpwnam(user).pw_uid
                except KeyError:
                    raise TaskException(errno.ENOENT, 'User {0} not found'.format(user))

            if group:
                try:
                    gid = grp.getgrnam(group).gr_gid
                except KeyError:
                    raise TaskException(errno.ENOENT, 'Group {0} not found'.format(group))

            bsd.lchown(path, uid, gid, recursive)

        ds = None
        chmod_safe = True

        try:
            poolname, dsname, rest = self.dispatcher.call_sync('volume.decode_path', path)
            ds = self.dispatcher.call_sync('volume.dataset.query', [('id', '=', dsname)], {'single': True})
            chmod_safe = ds['permissions_type'] == 'PERM'
        except RpcException:
            pass

        if permissions.get('modes'):
            modes = permissions['modes']
            if modes.get('value'):
                modes = int(modes['value'])
            else:
                modes = modes_to_oct(modes)

            try:
                bsd.lchmod(path, modes, recursive)
            except OSError as err:
                if err.errno == errno.EPERM:
                    if chmod_safe:
                        self.add_warning(TaskWarning(err.errno, 'chmod() failed: {0}'.format(err.strerror)))
                else:
                    raise TaskException(err.errno, 'chmod() failed: {0}'.format(err.strerror))

        if permissions.get('acl'):
            a = acl.ACL()
            a.__setstate__(permissions['acl'])
            a.apply(path)
            if not recursive:
                return

            for root, dirs, files in os.walk(path):
                for n in files:
                    a.apply(file=os.path.join(root, n))

                for n in dirs:
                    a.apply(file=os.path.join(root, n))

        if ds:
            self.dispatcher.dispatch_event('zfs.dataset.changed', {
                'operation': 'update',
                'ids': [ds['id']]
            })

        self.dispatcher.dispatch_event('file.permissions.changed', {
            'path': path,
            'recursive': recursive,
            'permissions': permissions
        })


def _init(dispatcher, plugin):
    plugin.register_schema_definition('stat', {
        'type': 'object',
        'properties': {
            'path': {'type': 'string'},
            'type': {'type': 'string'},
            'size': {'type': 'integer'},
            'atime': {'type': 'string'},
            'mtime': {'type': 'string'},
            'ctime': {'type': 'string'},
            'permissions': {'$ref': 'permissions'}
        }
    })

    plugin.register_schema_definition('permissions', {
        'type': 'object',
        'properties': {
            'user': {'type': ['string', 'null']},
            'group': {'type': ['string', 'null']},
            'modes': {'$ref': 'unix-permissions'},
            'acl': {
                'type': ['array', 'null'],
                'items': {'$ref': 'acl-entry'}
            }
        }
    })

    plugin.register_schema_definition('unix-permissions', {
        'type': 'object',
        'properties': {
            'value': {'type': ['integer', 'null']},
            'user': {'$ref': 'unix-mode-tuple'},
            'group': {'$ref': 'unix-mode-tuple'},
            'others': {'$ref': 'unix-mode-tuple'}
        }
    })

    plugin.register_schema_definition('unix-mode-tuple', {
        'type': 'object',
        'properties': {
            'read': {'type': 'boolean'},
            'write': {'type': 'boolean'},
            'execute': {'type': 'boolean'}
        }
    })

    plugin.register_schema_definition('acl-entry', {
        'type': 'object',
        'properties': {
            'tag': {
                'type': 'string',
                'enum': list(acl.ACLEntryTag.__members__.keys())
            },
            'type': {
                'type': 'string',
                'enum': list(acl.ACLEntryType.__members__.keys())
            },
            'id': {'type': ['string', 'null']},
            'name': {'type': ['string', 'null']},
            'perms': {'type': 'object'},
            'flags': {'type': 'object'},
            'text': {'type': ['string', 'null']}
        }
    })

    plugin.register_schema_definition('open-file', {
        'type': 'object',
        'properties': {
            'pid': {'type': 'integer'},
            'process_name': {'type': 'string'},
            'path': {'type': 'string'}
        }
    })

    plugin.register_provider('filesystem', FilesystemProvider)
    plugin.register_task_handler('file.download', DownloadFileTask)
    plugin.register_task_handler('file.upload', UploadFileTask)
    plugin.register_task_handler('file.set_permissions', SetPermissionsTask)
