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

import errno
import logging

from datastore.config import ConfigNode
from freenas.dispatcher.rpc import RpcException, SchemaHelper as h, description, accepts, returns, private
from task import Task, Provider, TaskException, TaskDescription
from freenas.utils import exclude

logger = logging.getLogger('SSHPlugin')


@description('Provides info about SSH service configuration')
class SSHProvider(Provider):
    @private
    @accepts()
    @returns(h.ref('service-sshd'))
    def get_config(self):
        return exclude(ConfigNode('service.sshd', self.configstore).__getstate__(), 'keys')


@private
@description('Configure SSH service')
@accepts(h.ref('service-sshd'))
class SSHConfigureTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Configuring SSH service'

    def describe(self, ssh):
        return TaskDescription('Configuring SSH service')

    def verify(self, ssh):
        return ['system']

    def run(self, ssh):
        try:
            node = ConfigNode('service.sshd', self.configstore)
            node.update(ssh)
            self.dispatcher.call_sync('etcd.generation.generate_group', 'sshd')
            self.dispatcher.dispatch_event('service.sshd.changed', {
                'operation': 'updated',
                'ids': None,
            })
        except RpcException as e:
            raise TaskException(
                errno.ENXIO, 'Cannot reconfigure SSH: {0}'.format(str(e))
            )

        return 'RELOAD'


def _depends():
    return ['ServiceManagePlugin']


def _init(dispatcher, plugin):
    # Register schemas
    plugin.register_schema_definition('service-sshd', {
        'type': 'object',
        'properties': {
            'type': {'enum': ['service-sshd']},
            'enable': {'type': 'boolean'},
            'port': {
                'type': 'integer',
                'minimum': 1,
                'maximum': 65535
                },
            'permit_root_login': {'type': 'boolean'},
            'allow_gssapi_auth': {'type': 'boolean'},
            'allow_pubkey_auth': {'type': 'boolean'},
            'allow_password_auth': {'type': 'boolean'},
            'allow_port_forwarding': {'type': 'boolean'},
            'compression': {'type': 'boolean'},
            'sftp_log_level': {'type': 'string', 'enum': [
                'QUIET',
                'FATAL',
                'ERROR',
                'INFO',
                'VERBOSE',
                'DEBUG',
                'DEBUG2',
                'DEBUG3',
            ]},
            'sftp_log_facility': {'type': 'string', 'enum': [
                'DAEMON',
                'USER',
                'AUTH',
                'LOCAL0',
                'LOCAL1',
                'LOCAL2',
                'LOCAL3',
                'LOCAL4',
                'LOCAL5',
                'LOCAL6',
                'LOCAL7',
            ]},
            'auxiliary': {'type': ['string', 'null']},
        },
        'additionalProperties': False,
    })

    # Register providers
    plugin.register_provider("service.sshd", SSHProvider)

    # Register tasks
    plugin.register_task_handler("service.sshd.update", SSHConfigureTask)
