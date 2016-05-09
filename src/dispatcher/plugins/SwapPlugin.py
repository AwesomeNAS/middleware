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
import re
import logging
import bsd.kld
from task import Provider
from lib.system import system, SubprocessException
from lib.geom import confxml
from freenas.dispatcher.rpc import accepts, returns, description, SchemaHelper as h
from freenas.utils.decorators import delay


logger = logging.getLogger('SwapPlugin')


@description("Provides informations about system swap")
class SwapProvider(Provider):
    @accepts()
    @returns(h.array(h.ref('swap-mirror')))
    @description("Returns information about swap mirrors present in the system")
    def info(self):
        return list(get_swap_info(self.dispatcher).values())


def get_available_disks(dispatcher):
    disks = []
    for i in dispatcher.call_sync('volume.query'):
        disks += dispatcher.call_sync('volume.get_volume_disks', i['id'])

    return disks


def get_swap_partition(dispatcher, disk):
    disk = dispatcher.call_sync('disk.query', [('path', '=', disk)], {'single': True})
    if not disk:
        return None

    return disk['status'].get('swap_partition_path')


def get_swap_name():
    for i in range(0, 100):
        name = 'swap{0}'.format(i)
        if not os.path.exists(os.path.join('/dev/mirror', name)):
            return name


def get_swap_info(dispatcher):
    xml = confxml()
    result = {}

    for mirror in xml.xpath("/mesh/class[name='MIRROR']/geom"):
        name = mirror.find('name').text
        if not re.match(r'^swap\d+$', name):
            continue

        swap = {
            'name': name,
            'disks': []
        }

        for cons in mirror.findall('consumer'):
            prov = cons.find('provider').attrib['ref']
            prov = xml.xpath(".//provider[@id='{0}']".format(prov))[0]
            disk_geom = prov.find('geom').attrib['ref']
            disk_geom = xml.xpath(".//geom[@id='{0}']".format(disk_geom))[0]
            swap['disks'].append(os.path.join('/dev', disk_geom.find('name').text))

        result[name] = swap

    return result


def configure_dumpdev(path):
    if os.path.lexists('/dev/dumpdev'):
        os.unlink('/dev/dumpdev')

    os.symlink(path, '/dev/dumpdev')
    system('/sbin/dumpon', path)


def clear_swap(dispatcher):
    logger.info('Clearing all swap mirrors in system')
    for swap in list(get_swap_info(dispatcher).values()):
        logger.debug('Clearing swap mirror %s', swap['name'])
        try:
            system('/sbin/swapoff', os.path.join('/dev/mirror', swap['name']))
        except SubprocessException:
            pass

        system('/sbin/gmirror', 'destroy', swap['name'])


def remove_swap(dispatcher, disks):
    disks = set(disks)
    for swap in list(get_swap_info(dispatcher).values()):
        if disks & set(swap['disks']):
            try:
                system('/sbin/swapoff', os.path.join('/dev/mirror', swap['name']))
                system('/sbin/gmirror', 'destroy', swap['name'])
            except SubprocessException as err:
                logger.warn('Failed to disable swap on {0}: {1}'.format(swap['name'], err.err.strip()))
                logger.warn('Continuing without {0}'.format(swap['name']))

    # Try to create new swap partitions, as at this stage we
    # might have two unused data disks
    if len(disks) > 0:
        rearrange_swap(dispatcher)


def create_swap(dispatcher, disks):
    disks = [x for x in [get_swap_partition(dispatcher, x) for x in disks] if x is not None]
    for pair in zip(*[iter(disks)] * 2):
        name = get_swap_name()
        disk_a, disk_b = pair
        logger.info('Creating swap partition %s from disks: %s, %s', name, disk_a, disk_b)
        system('/sbin/gmirror', 'label', '-b', 'prefer', name, disk_a, disk_b)
        system('/sbin/swapon', '/dev/mirror/{0}'.format(name))
        configure_dumpdev('/dev/mirror/{0}'.format(name))


def rearrange_swap(dispatcher):
    swap_info = list(get_swap_info(dispatcher).values())
    swap_disks = set(get_available_disks(dispatcher))
    active_swap_disks = set(sum([s['disks'] for s in swap_info], []))

    logger.debug('Rescanning available disks')
    logger.debug('Disks already used for swap: %s', ', '.join(active_swap_disks))
    logger.debug('Disks that could be used for swap: %s', ', '.join(swap_disks - active_swap_disks))
    logger.debug('Disks that can\'t be used for swap anymore: %s', ', '.join(active_swap_disks - swap_disks))

    create_swap(dispatcher, list(swap_disks - active_swap_disks))
    remove_swap(dispatcher, list(active_swap_disks - swap_disks))


def _depends():
    return ['VolumePlugin']


def _init(dispatcher, plugin):
    def volumes_pre_detach(args):
        disks = dispatcher.call_sync('volume.get_volume_disks', args['name'])
        remove_swap(dispatcher, disks)
        return True

    @delay(minutes=1)
    def on_volumes_change(args):
        with dispatcher.get_lock('swap'):
            rearrange_swap(dispatcher)

    plugin.register_schema_definition('swap-mirror', {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'disks': {
                'type': 'array',
                'items': {'type': 'string'}
            }
        }
    })

    plugin.register_provider('swap', SwapProvider)
    plugin.register_event_handler('volume.changed', on_volumes_change)
    plugin.attach_hook('volume.pre_destroy', volumes_pre_detach)
    plugin.attach_hook('volume.pre_detach', volumes_pre_detach)

    try:
        bsd.kld.kldload('/boot/kernel/geom_mirror.ko')
    except FileExistsError:
        pass

    clear_swap(dispatcher)
    rearrange_swap(dispatcher)
