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
import gevent
import logging
from cache import CacheStore
from freenas.utils import query as q
from freenas.dispatcher.rpc import RpcException, SchemaHelper as h, description, accepts, returns, generator
from task import Task, Provider, TaskException, VerifyException, query, TaskDescription


logger = logging.getLogger(__name__)
peers_status = None


@description('Provides information about known peers')
class PeerProvider(Provider):
    @query('peer')
    @generator
    def query(self, filter=None, params=None):
        def extend(peer):
            peer['status'] = peers_status.get(peer['id'], {'state': 'UNKNOWN', 'rtt': None})
            return peer

        return q.query(
            self.datastore.query_stream('peers', callback=extend),
            *(filter or []),
            stream=True,
            **(params or {})
        )

    @returns(h.array(str))
    def peer_types(self):
        result = []
        for p in self.dispatcher.plugins.values():
            if p.metadata and p.metadata.get('type') == 'peering':
                result.append(p.metadata.get('subtype'))

        return result


@description('Creates a peer entry')
@accepts(
    h.all_of(
        h.ref('peer'),
        h.required('type', 'credentials')
    ),
    h.ref('peer-initial-credentials')
)
class PeerCreateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Creating peer entry'

    def describe(self, peer, initial_credentials=None):
        return TaskDescription('Creating peer entry {name}', name=peer.get('name', ''))

    def verify(self, peer, initial_credentials=None):
        if peer.get('type') not in self.dispatcher.call_sync('peer.peer_types'):
            raise VerifyException(errno.EINVAL, 'Unknown peer type {0}'.format(peer.get('type')))

        return ['system']

    def run(self, peer, initial_credentials=None):
        ids = self.join_subtasks(self.run_subtask(
            'peer.{0}.create'.format(peer.get('type')),
            peer,
            initial_credentials
        ))

        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'create',
            'ids': [ids[0]]
        })


@description('Updates peer entry')
@accepts(str, h.ref('peer'))
class PeerUpdateTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Updating peer entry'

    def describe(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Updating peer entry {name}', name=peer.get('name', ''))

    def verify(self, id, updated_fields):
        return ['system']

    def run(self, id, updated_fields):
        peer = self.datastore.get_by_id('peers', id)
        if not peer:
            raise TaskException(errno.ENOENT, 'Peer {0} does not exist'.format(id))

        if 'type' in updated_fields and peer['type'] != updated_fields['type']:
            raise TaskException(errno.EINVAL, 'Peer type cannot be updated')

        self.join_subtasks(self.run_subtask('peer.{0}.update'.format(peer.get('type')), id, updated_fields))
        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'update',
            'ids': [id]
        })


@description('Deletes peer entry')
@accepts(str)
class PeerDeleteTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Deleting peer entry'

    def describe(self, id):
        peer = self.datastore.get_by_id('peers', id)
        return TaskDescription('Deleting peer entry {name}', name=peer.get('name', ''))

    def verify(self, id):
        return ['system']

    def run(self, id):
        if not self.datastore.exists('peers', ('id', '=', id)):
            raise TaskException(errno.EINVAL, 'Peer entry {0} does not exist'.format(id))

        peer = self.datastore.get_by_id('peers', id)

        self.join_subtasks(self.run_subtask('peer.{0}.delete'.format(peer.get('type')), id))
        self.dispatcher.dispatch_event('peer.changed', {
            'operation': 'delete',
            'ids': [id]
        })


def _init(dispatcher, plugin):
    global peers_status
    peers_status = CacheStore()

    # Register schemas
    plugin.register_schema_definition('peer', {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'id': {'type': 'string'},
            'type': {'type': 'string'},
            'status': {'$ref': 'peer-status'},
            'credentials': {'$ref': 'peer-credentials'}
        },
        'additionalProperties': False
    })

    plugin.register_schema_definition('peer-status', {
        'type': 'object',
        'properties': {
            'state': {
                'type': 'string',
                'enum': ['ONLINE', 'OFFLINE', 'UNKNOWN', 'NOT_SUPPORTED'],
                'readOnly': True
            },
            'rtt': {'type': ['number', 'null'], 'readOnly': True}
        },
        'additionalProperties': False
    })

    def on_peer_change(args):
        if args['operation'] == 'create':
            items = list(dispatcher.datastore.query('peers', ('id', 'in', args['ids'])))
            peers_status.update(**{i['id']: {'state': 'UNKNOWN', 'rtt': None} for i in items})

            for i in items:
                update_peer_health(i)

        elif args['operation'] == 'delete':
            peers_status.remove_many(args['ids'])

    def update_peer_health(peer):
        def update_one(id, new_state):
            if isinstance(new_state, RpcException):
                logger.warning('Health check for peer {0} failed: {1}'.format(id, str(new_state)))
                return

            old_state = peers_status.get(id)
            if old_state != new_state:
                peers_status.update_one(id, state=new_state['state'], rtt=new_state['rtt'])
                dispatcher.dispatch_event('peer.changed', {
                    'operation': 'update',
                    'ids': [id]
                })

        dispatcher.call_async(
            'peer.{0}.get_status'.format(peer['type']),
            lambda result: update_one(peer['id'], result),
            peer['id']
        )

    def health_worker():
        interval = dispatcher.configstore.get('peer.ping_interval')
        while True:
            peer_types = dispatcher.call_sync('peer.peer_types')
            for p in dispatcher.call_sync('peer.query', [('type', 'in', peer_types)]):
                update_peer_health(p)

            gevent.sleep(interval)

    # Register credentials schema
    def update_peer_credentials_schema():
        credential_types = []
        initial_credential_types = []

        for p in dispatcher.plugins.values():
            if p.metadata and p.metadata.get('type') == 'peering':
                credential_types.append('{0}-credentials'.format(p.metadata['subtype']))
                if p.metadata.get('initial_credentials'):
                    initial_credential_types.append('{0}-initial-credentials'.format(p.metadata['subtype']))

        plugin.register_schema_definition('peer-credentials', {
            'discriminator': '%type',
            'oneOf': [
                {'$ref': name} for name in credential_types
            ]
        })

        plugin.register_schema_definition('peer-initial-credentials', {
            'discriminator': '%type',
            'oneOf':
                [{'$ref': name} for name in initial_credential_types] +
                [{'type': 'null'}]
        })

    # Register providers
    plugin.register_provider('peer', PeerProvider)
    plugin.register_event_handler('peer.changed', on_peer_change)

    # Register event handlers
    dispatcher.register_event_handler('server.plugin.loaded', update_peer_credentials_schema)

    # Register tasks
    plugin.register_task_handler("peer.create", PeerCreateTask)
    plugin.register_task_handler("peer.update", PeerUpdateTask)
    plugin.register_task_handler("peer.delete", PeerDeleteTask)

    # Register event types
    plugin.register_event_type('peer.changed')

    # Init peer credentials schema
    update_peer_credentials_schema()

    peers_status.update(**{i['id']: {'state': 'UNKNOWN', 'rtt': None} for i in dispatcher.datastore.query('peers')})

    gevent.spawn(health_worker)
