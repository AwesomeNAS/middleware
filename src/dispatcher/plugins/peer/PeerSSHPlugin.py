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

import errno
import socket
import logging
from datetime import datetime
from freenas.dispatcher.rpc import SchemaHelper as h, description, accepts, returns, private, generator
from task import Task, Provider, TaskException, VerifyException, query, TaskDescription
from freenas.utils import query as q


logger = logging.getLogger(__name__)


@description('Provides information about SSH peers')
class PeerSSHProvider(Provider):
    @query('peer')
    @generator
    def query(self, filter=None, params=None):
        return q.query(
            self.dispatcher.call_sync('peer.query', [('type', '=', 'ssh')]),
            *(filter or []),
            stream=True,
            **(params or {})
        )

    @private
    @accepts(str)
    @returns(h.tuple(str, h.ref('peer-status')))
    def get_status(self, id):
        peer = self.dispatcher.call_sync('peer.query', [('id', '=', id), ('type', '=', 'ssh')], {'single': True})
        if not peer:
            return id, {'state': 'UNKNOWN', 'rtt': None}

        credentials = peer['credentials']

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            start_time = datetime.now()
            s.connect((credentials['address'], credentials['port']))
            delta = datetime.now() - start_time
            return id, {'state': 'ONLINE', 'rtt': delta.seconds + delta.microseconds / 1E6}
        except socket.error:
            return id, {'state': 'OFFLINE', 'rtt': None}
        finally:
            s.close()


@private
@description('Creates a SSH peer entry')
@accepts(h.all_of(
    h.ref('peer'),
    h.required('type', 'credentials')
))
class SSHPeerCreateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Creating SSH peer entry'

    def describe(self, peer):
        return TaskDescription('Creating SSH peer entry {name}', name=peer.get('name', ''))

    def verify(self, peer):
        if peer.get('type') != 'ssh':
            raise VerifyException(errno.EINVAL, 'Peer type must be selected as SSH')

        return ['system']

    def run(self, peer):
        if 'name' not in peer:
            raise TaskException(errno.EINVAL, 'Name has to be specified')

        if self.datastore.exists('peers', ('name', '=', peer['name'])):
            raise TaskException(errno.EINVAL, 'Peer entry {0} already exists'.format(peer['name']))

        if peer['type'] != peer['credentials']['type']:
            raise TaskException(errno.EINVAL, 'Peer type and credentials type must match')

        return self.datastore.insert('peers', peer)


@private
@description('Updates a SSH peer entry')
@accepts(str, h.ref('peer'))
class SSHPeerUpdateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Updating SSH peer entry'

    def describe(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Updating SSH peer entry {name}', name=peer.get('name', ''))

    def verify(self, id, updated_fields):
        if 'type' in updated_fields:
            raise VerifyException(errno.EINVAL, 'Type of peer cannot be updated')

        return ['system']

    def run(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        if not peer:
            raise TaskException(errno.ENOENT, 'Peer {0} does not exist'.format(id))

        if 'type' in updated_fields and peer['type'] != updated_fields['type']:
            raise TaskException(errno.EINVAL, 'Peer type cannot be updated')

        peer.update(updated_fields)
        if self.datastore.exists('peers', ('name', '=', peer['name'])):
            raise TaskException(errno.EINVAL, 'Peer entry {0} already exists'.format(peer['name']))

        self.datastore.update('peers', id, peer)


@private
@description('Deletes SSH peer entry')
@accepts(str)
class SSHPeerDeleteTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Deleting SSh peer entry'

    def describe(self, id):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Deleting SSH peer entry {name}', name=peer.get('name', ''))

    def verify(self, id):
        return ['system']

    def run(self, id):
        if not self.datastore.exists('peers', ('id', '=', id)):
            raise TaskException(errno.EINVAL, 'Peer entry {0} does not exist'.format(id))

        self.datastore.delete('peers', id)


def _depends():
    return ['PeerPlugin']


def _metadata():
    return {
        'type': 'peering',
        'subtype': 'ssh'
    }


def _init(dispatcher, plugin):
    # Register schemas
    plugin.register_schema_definition('ssh-credentials', {
        'type': 'object',
        'properties': {
            'type': {'enum': ['ssh']},
            'address': {'type': 'string'},
            'username': {'type': 'string'},
            'port': {'type': 'number'},
            'password': {'type': 'string'},
            'privkey': {'type': 'string'},
            'hostkey': {'type': 'string'},
        },
        'additionalProperties': False
    })

    # Register providers
    plugin.register_provider('peer.ssh', PeerSSHProvider)

    # Register tasks
    plugin.register_task_handler("peer.ssh.create", SSHPeerCreateTask)
    plugin.register_task_handler("peer.ssh.delete", SSHPeerDeleteTask)
    plugin.register_task_handler("peer.ssh.update", SSHPeerUpdateTask)

