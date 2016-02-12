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

import re
import errno
from freenas.dispatcher.rpc import RpcException, description, accepts, returns
from freenas.dispatcher.rpc import SchemaHelper as h
from task import Provider, Task, VerifyException, TaskException, query
from freenas.utils.query import wrap


UNITS = {
    'Ops/s': {
        'match': lambda x: re.match(r'(.*)(disk_merged|disk_ops)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    },
    'B/s': {
        'match': lambda x: re.match(r'(.*)(disk_octets|if_octets)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    },
    'B': {
        'match': lambda x: re.match(r'(.*)(df-|memory)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    },
    'C': {
        'match': lambda x: re.match(r'(.*)(temperature)(.*)', x),
        'normalize': lambda x: None if (x == -1) or (x is None) else (x - 2732)/10,
        'raw': lambda x: None if x is None else x * 10 + 2732
    },
    'Jiffies': {
        'match': lambda x: re.match(r'(.*)(cpu-)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    },
    'Packets/s': {
        'match': lambda x: re.match(r'(.*)(if_packets)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    },
    'Errors/s': {
        'match': lambda x: re.match(r'(.*)(if_errors)(.*)', x),
        'normalize': lambda x: x,
        'raw': lambda x: x
    }
}


class StatProvider(Provider):
    @query('stat')
    def query(self, filter=None, params=None):
        stats = self.dispatcher.call_sync('statd.output.get_current_state')
        return wrap(stats).query(*(filter or []), **(params or {}))

    def normalize(self, name, value):
        return normalize(name, value)


class CpuStatProvider(Provider):
    @query('stat')
    def query(self, filter=None, params=None):
        stats = self.dispatcher.call_sync('stat.query', [('name', '~', 'cpu')])

        for stat in stats:
            type = stat['name'].split('.', 3)[2]
            if 'aggregation' in stat['name']:
                stat['short_name'] = dash_to_underscore('aggregated-' + type)
            else:
                stat['short_name'] = dash_to_underscore('cpu-' + re.search(r'\d+', stat['name']).group() + '-' + type)

            normalize_values(stat)

        return wrap(stats).query(*(filter or []), **(params or {}))


class DiskStatProvider(Provider):
    @query('stat')
    def query(self, filter=None, params=None):
        stats = self.dispatcher.call_sync('stat.query', [('name', '~', 'disk')])

        for stat in stats:
            split_name = stat['name'].split('.', 3)
            stat['short_name'] = dash_to_underscore(
                split_name[1] + '-' + split_name[3] + '-' + split_name[2].split('_', 2)[1]
            )

            normalize_values(stat)

        return wrap(stats).query(*(filter or []), **(params or {}))


class NetworkStatProvider(Provider):
    @query('stat')
    def query(self, filter=None, params=None):
        stats = self.dispatcher.call_sync('stat.query', [('name', '~', 'interface')])

        for stat in stats:
            split_name = stat['name'].split('.', 3)
            stat['short_name'] = dash_to_underscore(
                split_name[1] + '-' + split_name[3] + '-' + split_name[2].split('_', 2)[1]
            )

            normalize_values(stat)

        return wrap(stats).query(*(filter or []), **(params or {}))


class SystemStatProvider(Provider):
    @query('stat')
    def query(self, filter=None, params=None):
        stats = self.dispatcher.call_sync(
            'stat.query',
            [
                ['or', [('name', '~', 'load'), ('name', '~', 'processes'), ('name', '~', 'memory'), ('name', '~', 'df')]],
                ['nor', [('name', '~', 'zfs')]]
            ]
        )

        for stat in stats:
            split_name = stat['name'].split('.', 3)
            if 'df' in stat['name']:
                stat['short_name'] = dash_to_underscore(
                    split_name[1].split('-', 1)[1] + '-' + split_name[2].split('-', 1)[1]
                )
            elif 'load' in stat['name']:
                stat['short_name'] = dash_to_underscore(split_name[1] + '-' + split_name[3])
            else:
                stat['short_name'] = dash_to_underscore(split_name[2])

            normalize_values(stat)

        return wrap(stats).query(*(filter or []), **(params or {}))


@accepts(str, h.ref('stat'))
class UpdateAlertTask(Task):
    def verify(self, name, stat):
        if name not in self.dispatcher.call_sync('statd.output.get_data_sources'):
            raise VerifyException(errno.ENOENT, 'Statistic {0} not found.'.format(name))
        return ['system']

    def run(self, name, stat):
        updated_alerts = stat.get('alerts')

        for field in updated_alerts:
            if isinstance(updated_alerts[field], bool):
                self.dispatcher.call_sync(
                    'statd.alert.set_alert',
                    name,
                    field,
                    updated_alerts[field]
                )
            elif field in ['alert_high', 'alert_low']:
                self.dispatcher.call_sync(
                    'statd.alert.set_alert',
                    name,
                    field,
                    raw(name, updated_alerts[field])
                )

        self.dispatcher.dispatch_event('stat.alert.changed', {
            'operation': 'update',
            'ids': [name]
        })


def normalize_values(stat):
    stat['unit'], stat['normalized_value'] = normalize(stat['name'], stat['last_value'])
    stat['unit'], stat['alerts']['normalized_alert_high'] = normalize(stat['name'], stat['alerts']['alert_high'])
    stat['unit'], stat['alerts']['normalized_alert_low'] = normalize(stat['name'], stat['alerts']['alert_low'])


def normalize(name, value):
    for key, unit in UNITS.items():
        if unit['match'](name):
            return key, unit['normalize'](value)

    return '', value


def raw(name, value):
    for key, unit in UNITS.items():
        if unit['match'](name):
            return unit['raw'](value)

    return value


def dash_to_underscore(name):
    return name.replace('-', '_')


def _init(dispatcher, plugin):
    plugin.register_provider('stat', StatProvider)
    plugin.register_provider('stat.cpu', CpuStatProvider)
    plugin.register_provider('stat.disk', DiskStatProvider)
    plugin.register_provider('stat.network', NetworkStatProvider)
    plugin.register_provider('stat.system', SystemStatProvider)
    plugin.register_task_handler('stat.alert_update', UpdateAlertTask)
    plugin.register_event_type('stat.alert.changed')

