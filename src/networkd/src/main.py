#!/usr/local/bin/python2.7
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

from __future__ import print_function
import os
import sys
import argparse
import logging
import json
import subprocess
import errno
import threading
import setproctitle
import signal
import netif
import time
import ipaddress
import io
from datastore import get_datastore, DatastoreException
from datastore.config import ConfigStore
from freenas.dispatcher.client import Client, ClientError
from freenas.dispatcher.rpc import RpcService, RpcException, private
from freenas.utils.query import wrap
from freenas.utils.debug import DebugService
from freenas.utils import configure_logging
from functools import reduce


DEFAULT_CONFIGFILE = '/usr/local/etc/middleware.conf'


def cidr_to_netmask(cidr):
    iface = ipaddress.ip_interface('0.0.0.0/{0}'.format(cidr))
    return str(str(iface.netmask))


def convert_aliases(entity):
    for i in entity.get('aliases', []):
        addr = netif.InterfaceAddress()
        iface = ipaddress.ip_interface('{0}/{1}'.format(i['address'], i['netmask']))
        addr.af = getattr(netif.AddressFamily, i.get('type', 'INET'))
        addr.address = ipaddress.ip_address(i['address'])
        addr.netmask = iface.netmask
        addr.broadcast = iface.network.broadcast_address

        if i.get('broadcast'):
            addr.broadcast = ipaddress.ip_address(i['broadcast'])

        if i.get('dest-address'):
            addr.dest_address = ipaddress.ip_address(i['dest-address'])

        yield addr


def convert_route(entity):
    if not entity:
        return None

    if entity['network'] == 'default':
        entity['network'] = '0.0.0.0'
        entity['netmask'] = '0.0.0.0'

    netmask = cidr_to_netmask(entity['netmask'])
    r = netif.Route(
        entity['network'],
        netmask,
        entity.get('gateway'),
        entity.get('interface')
    )

    r.flags.add(netif.RouteFlags.STATIC)

    if not r.netmask:
        r.flags.add(netif.RouteFlags.HOST)

    if r.gateway:
        r.flags.add(netif.RouteFlags.GATEWAY)

    return r


def default_route(gateway):
    if not gateway:
        return None

    gw = ipaddress.ip_address(gateway)
    if gw.version == 4:
        r = netif.Route('0.0.0.0', '0.0.0.0', gateway)

    elif gw.version == 6:
        r = netif.Route('::', '::', gateway)

    else:
        return

    r.flags.add(netif.RouteFlags.STATIC)
    r.flags.add(netif.RouteFlags.GATEWAY)
    return r


def describe_route(route):
    bits = bin(int(route.netmask)).count('1') if route.netmask else 0
    return '{0}/{1} via {2}'.format(route.network, bits, route.gateway)


def filter_routes(routes):
    """
    Filter out routes for loopback addresses and local subnets
    :param routes: routes list
    :return: filtered routes list
    """

    aliases = [i.addresses for i in list(netif.list_interfaces().values())]
    aliases = reduce(lambda x, y: x+y, aliases)
    aliases = [a for a in aliases if a.af == netif.AddressFamily.INET]
    aliases = [ipaddress.ip_interface('{0}/{1}'.format(a.address, a.netmask)) for a in aliases]

    for i in routes:
        if type(i.gateway) is str:
            continue

        if i.af != netif.AddressFamily.INET:
            continue

        found = True
        for a in aliases:
            if i.network in a.network:
                found = False
                break

        if found:
            yield i


def get_addresses(entity):
    return [ipaddress.ip_address(i['address']) for i in entity.get('aliases', [])]


class RoutingSocketEventSource(threading.Thread):
    def __init__(self, context):
        super(RoutingSocketEventSource, self).__init__()
        self.context = context
        self.client = context.client
        self.mtu_cache = {}
        self.flags_cache = {}
        self.link_state_cache = {}

    def build_cache(self):
        # Build a cache of certain interface states so we'll later know what has changed
        for i in list(netif.list_interfaces().values()):
            self.mtu_cache[i.name] = i.mtu
            self.flags_cache[i.name] = i.flags
            self.link_state_cache[i.name] = i.link_state

    def alias_added(self, message):
        pass

    def alias_removed(self, message):
        pass

    def run(self):
        rtsock = netif.RoutingSocket()
        rtsock.open()

        self.build_cache()

        while True:
            message = rtsock.read_message()

            if type(message) is netif.InterfaceAnnounceMessage:
                args = {'name': message.interface}

                if message.type == netif.InterfaceAnnounceType.ARRIVAL:
                    self.context.interface_attached(message.interface)
                    self.client.emit_event('network.interface.attached', args)

                if message.type == netif.InterfaceAnnounceType.DEPARTURE:
                    self.context.interface_detached(message.interface)
                    self.client.emit_event('network.interface.detached', args)

                self.build_cache()

            if type(message) is netif.InterfaceInfoMessage:
                ifname = message.interface
                if self.mtu_cache[ifname] != message.mtu:
                    self.client.emit_event('network.interface.mtu_changed', {
                        'interface': ifname,
                        'old-mtu': self.mtu_cache[ifname],
                        'new-mtu': message.mtu
                    })

                if self.link_state_cache[ifname] != message.link_state:
                    if message.link_state == netif.InterfaceLinkState.LINK_STATE_DOWN:
                        self.context.logger.warn('Link down on interface {0}'.format(ifname))
                        self.client.emit_event('network.interface.link_down', {
                            'interface': ifname,
                        })

                    if message.link_state == netif.InterfaceLinkState.LINK_STATE_UP:
                        self.context.logger.warn('Link up on interface {0}'.format(ifname))
                        self.client.emit_event('network.interface.link_up', {
                            'interface': ifname,
                        })

                if self.flags_cache[ifname] != message.flags:
                    if (netif.InterfaceFlags.UP in self.flags_cache) and (netif.InterfaceFlags.UP not in message.flags):
                        self.client.emit_event('network.interface.down', {
                            'interface': ifname,
                        })

                    if (netif.InterfaceFlags.UP not in self.flags_cache) and (netif.InterfaceFlags.UP in message.flags):
                        self.client.emit_event('network.interface.up', {
                            'interface': ifname,
                        })

                    self.client.emit_event('network.interface.flags_changed', {
                        'interface': ifname,
                        'old-flags': [f.name for f in self.flags_cache[ifname]],
                        'new-flags': [f.name for f in message.flags]
                    })

                self.client.emit_event('network.interface.changed', {
                    'operation': 'update',
                    'ids': [ifname]
                })

                self.build_cache()

            if type(message) is netif.InterfaceAddrMessage:
                entity = self.context.datastore.get_by_id('network.interfaces', message.interface)
                if entity is None:
                    continue

                # Skip messagess with empty address
                if not message.address:
                    continue

                # Skip 0.0.0.0 aliases
                if message.address == ipaddress.IPv4Address('0.0.0.0'):
                    continue

                addr = netif.InterfaceAddress()
                addr.af = netif.AddressFamily.INET
                addr.address = message.address
                addr.netmask = message.netmask
                addr.broadcast = message.dest_address

                if message.type == netif.RoutingMessageType.NEWADDR:
                    self.context.logger.warn('New alias added to interface {0} externally: {1}/{2}'.format(
                        message.interface,
                        message.address,
                        message.netmask
                    ))

                if message.type == netif.RoutingMessageType.DELADDR:
                    self.context.logger.warn('Alias removed from interface {0} externally: {1}/{2}'.format(
                        message.interface,
                        message.address,
                        message.netmask
                    ))

                self.client.emit_event('network.interface.changed', {
                    'operation': 'update',
                    'ids': [entity['id']]
                })

            if type(message) is netif.RoutingMessage:
                if message.errno != 0:
                    continue

                if message.type == netif.RoutingMessageType.ADD:
                    self.context.logger.info('Route to {0} added'.format(describe_route(message.route)))
                    self.client.emit_event('network.route.added', message.__getstate__())

                if message.type == netif.RoutingMessageType.DELETE:
                    self.context.logger.info('Route to {0} deleted'.format(describe_route(message.route)))
                    self.client.emit_event('network.route.deleted', message.__getstate__())

        rtsock.close()


@private
class ConfigurationService(RpcService):
    def __init__(self, context):
        self.context = context
        self.logger = context.logger
        self.config = context.configstore
        self.datastore = context.datastore
        self.client = context.client

    def get_next_name(self, type):
        type_map = {
            'VLAN': 'vlan',
            'LAGG': 'lagg',
            'BRIDGE': 'bridge'
        }

        if type not in list(type_map.keys()):
            raise RpcException(errno.EINVAL, 'Invalid type: {0}'.format(type))

        ifaces = netif.list_interfaces()
        for i in range(2 if type == 'BRIDGE' else 0, 999):
            name = '{0}{1}'.format(type_map[type], i)
            if name not in list(ifaces.keys()) and not self.datastore.exists('network.interfaces', ('id', '=', name)):
                return name

        raise RpcException(errno.EBUSY, 'No free interfaces left')

    def query_interfaces(self):
        return netif.list_interfaces()

    def query_routes(self):
        rtable = netif.RoutingTable()
        return wrap(rtable.static_routes)

    def configure_network(self):
        if self.config.get('network.autoconfigure'):
            # Try DHCP on each interface until we find lease. Mark failed ones as disabled.
            self.logger.warn('Network in autoconfiguration mode')
            for i in list(netif.list_interfaces().values()):
                entity = self.datastore.get_by_id('network.interfaces', i.name)
                if i.type == netif.InterfaceType.LOOP:
                    continue

                self.logger.info('Trying to acquire DHCP lease on interface {0}...'.format(i.name))
                if self.context.configure_dhcp(i.name):
                    entity.update({
                        'enabled': True,
                        'dhcp': True
                    })

                    self.datastore.update('network.interfaces', entity['id'], entity)
                    self.config.set('network.autoconfigure', False)
                    self.config.set('container.default_nic', i.name)
                    self.logger.info('Successfully configured interface {0}'.format(i.name))
                    return

            self.logger.warn('Failed to configure any network interface')
            return

        for i in self.datastore.query('network.interfaces'):
            self.logger.info('Configuring interface {0}...'.format(i['id']))
            try:
                self.configure_interface(i['id'], False)
            except BaseException as e:
                self.logger.warning('Cannot configure {0}: {1}'.format(i['id'], str(e)), exc_info=True)

        # Are there any orphaned interfaces?
        for name, iface in list(netif.list_interfaces().items()):
            if not name.startswith(('vlan', 'lagg', 'bridge')):
                continue

            if not self.datastore.exists('network.interfaces', ('id', '=', name)):
                netif.destroy_interface(name)

        self.configure_routes()
        self.configure_dns()
        self.client.call_sync('service.restart', 'rtsold')
        self.client.emit_event('network.changed', {
            'operation': 'update'
        })

    def configure_routes(self):
        rtable = netif.RoutingTable()
        static_routes = filter_routes(rtable.static_routes)
        default_route_ipv4 = default_route(self.config.get('network.gateway.ipv4'))

        if not self.context.using_dhcp_for_gateway():
            # Default route was deleted
            if not default_route_ipv4 and rtable.default_route_ipv4:
                self.logger.info('Removing default route')
                try:
                    rtable.delete(rtable.default_route_ipv4)
                except OSError as e:
                    self.logger.error('Cannot remove default route: {0}'.format(str(e)))

            # Default route was added
            elif not rtable.default_route_ipv4 and default_route_ipv4:
                self.logger.info('Adding default route via {0}'.format(default_route_ipv4.gateway))
                try:
                    rtable.add(default_route_ipv4)
                except OSError as e:
                    self.logger.error('Cannot add default route: {0}'.format(str(e)))

            # Default route was changed
            elif rtable.default_route_ipv4 != default_route_ipv4:
                self.logger.info('Changing default route from {0} to {1}'.format(
                    rtable.default_route.gateway,
                    default_route_ipv4.gateway))

                try:
                    rtable.change(default_route_ipv4)
                except OSError as e:
                    self.logger.error('Cannot add default route: {0}'.format(str(e)))

        else:
            self.logger.info('Not configuring default route as using DHCP')

        # Same thing for IPv6
        default_route_ipv6 = default_route(self.config.get('network.gateway.ipv6'))

        if not default_route_ipv6 and rtable.default_route_ipv6:
            # Default route was deleted
            self.logger.info('Removing default route')
            try:
                rtable.delete(rtable.default_route_ipv6)
            except OSError as e:
                self.logger.error('Cannot remove default route: {0}'.format(str(e)))

        elif not rtable.default_route_ipv6 and default_route_ipv6:
            # Default route was added
            self.logger.info('Adding default route via {0}'.format(default_route_ipv6.gateway))
            try:
                rtable.add(default_route_ipv6)
            except OSError as e:
                self.logger.error('Cannot add default route: {0}'.format(str(e)))

        elif rtable.default_route_ipv6 != default_route_ipv6:
            # Default route was changed
            self.logger.info('Changing default route from {0} to {1}'.format(
                rtable.default_route.gateway,
                default_route_ipv6.gateway))

            try:
                rtable.change(default_route_ipv6)
            except OSError as e:
                self.logger.error('Cannot add default route: {0}'.format(str(e)))

        # Now the static routes...
        old_routes = set(static_routes)
        new_routes = set([convert_route(e) for e in self.datastore.query('network.routes')])

        for i in old_routes - new_routes:
            self.logger.info('Removing static route to {0}'.format(describe_route(i)))
            try:
                rtable.delete(i)
            except OSError as e:
                self.logger.error('Cannot remove static route to {0}: {1}'.format(describe_route(i), str(e)))

        for i in new_routes - old_routes:
            self.logger.info('Adding static route to {0}'.format(describe_route(i)))
            try:
                rtable.add(i)
            except OSError as e:
                self.logger.error('Cannot add static route to {0}: {1}'.format(describe_route(i), str(e)))

    def configure_dns(self):
        resolv = io.StringIO()
        proc = subprocess.Popen(
            ['/sbin/resolvconf', '-a', 'lo0'],
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE
        )

        for s in self.context.configstore.get('network.dns.search'):
            print('search {0}'.format(s), file=resolv)

        addrs = self.context.configstore.get('network.dns.addresses')
        for n in addrs:
            print('nameserver {0}'.format(n), file=resolv)

        proc.communicate(resolv.getvalue().encode('utf8'))
        proc.wait()
        resolv.close()
        self.client.emit_event('network.dns.configured', {
            'addresses': addrs,
        })

    def configure_interface(self, name, restart_rtsold=True):
        entity = self.datastore.get_one('network.interfaces', ('id', '=', name))
        if not entity:
            raise RpcException(errno.ENXIO, "Configuration for interface {0} not found".format(name))

        if not entity.get('enabled'):
            self.logger.info('Interface {0} is disabled'.format(name))
            return

        try:
            iface = netif.get_interface(name)
        except KeyError:
            if entity.get('cloned'):
                netif.create_interface(entity['id'])
                iface = netif.get_interface(name)
            else:
                raise RpcException(errno.ENOENT, "Interface {0} not found".format(name))

        # If it's VLAN, configure parent and tag
        if entity.get('type') == 'VLAN':
            vlan = entity.get('vlan')
            if vlan:
                parent = vlan.get('parent')
                tag = vlan.get('tag')

                if parent and tag:
                    try:
                        tag = int(tag)
                        iface.unconfigure()
                        iface.configure(parent, tag)
                    except Exception as e:
                        self.logger.warn('Failed to configure VLAN interface {0}: {1}'.format(name, str(e)))

        # Configure protocol and member ports for a LAGG
        if entity.get('type') == 'LAGG':
            lagg = entity.get('lagg')
            if lagg:
                iface.protocol = getattr(netif.AggregationProtocol, lagg.get('protocol', 'FAILOVER'))
                old_ports = set(iface.ports)
                new_ports = set(lagg['ports'])

                for port in old_ports - new_ports:
                    iface.delete_port(port)

                for port in new_ports - old_ports:
                    iface.add_port(port)

        # Configure member interfaces for a bridge
        if entity.get('type') == 'BRIDGE':
            bridge = entity.get('bridge')
            if bridge:
                old_members = set(iface.members)
                new_members = set(bridge['members'])

                for port in old_members - new_members:
                    iface.delete_member(port)

                for port in new_members - old_members:
                    iface.add_member(port)

        if entity.get('dhcp'):
            if self.context.dhclient_running(name):
                self.logger.info('Interface {0} already configured using DHCP'.format(name))
            else:
                # Remove all existing aliases
                for i in iface.addresses:
                    iface.remove_address(i)

                self.logger.info('Trying to acquire DHCP lease on interface {0}...'.format(name))
                if not self.context.configure_dhcp(name):
                    self.logger.warn('Failed to configure interface {0} using DHCP'.format(name))
        else:
            addresses = set(convert_aliases(entity))
            existing_addresses = set([a for a in iface.addresses if a.af != netif.AddressFamily.LINK])

            # Remove orphaned addresses
            for i in existing_addresses - addresses:
                if i.af == netif.AddressFamily.INET6 and str(i.address).startswith('fe80::'):
                    # skip link-local IPv6 addresses
                    continue

                self.logger.info('Removing address from interface {0}: {1}'.format(name, i))
                iface.remove_address(i)

            # Add new or changed addresses
            for i in addresses - existing_addresses:
                self.logger.info('Adding new address to interface {0}: {1}'.format(name, i))
                iface.add_address(i)

        # nd6 stuff
        if entity.get('rtadv', False):
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}
            if restart_rtsold:
                self.client.call_sync('service.restart', 'rtsold')
        else:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.ACCEPT_RTADV}

        if entity.get('noipv6', False):
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}
        else:
            iface.nd6_flags = iface.nd6_flags - {netif.NeighborDiscoveryFlags.IFDISABLED}
            iface.nd6_flags = iface.nd6_flags | {netif.NeighborDiscoveryFlags.AUTO_LINKLOCAL}

        if entity.get('mtu'):
            iface.mtu = entity['mtu']

        if entity.get('media'):
            iface.media_subtype = entity['media']

        if entity.get('capabilities'):
            caps = iface.capabilities
            for c in entity['capabilities'].get('add'):
                caps.add(getattr(netif.InterfaceCapability, c))

            for c in entity['capabilities'].get('del'):
                caps.remove(getattr(netif.InterfaceCapability, c))

            iface.capabilities = caps

        if netif.InterfaceFlags.UP not in iface.flags:
            self.logger.info('Bringing interface {0} up'.format(name))
            iface.up()

        self.client.emit_event('network.interface.configured', {
            'interface': name,
        })

    def up_interface(self, name):
        self.configure_interface(name)

    def down_interface(self, name):
        try:
            iface = netif.get_interface(name)
        except NameError:
            raise RpcException(errno.ENOENT, "Interface {0} not found".format(name))

        # Remove all IP addresses from interface
        for addr in iface.addresses:
            if addr.af == netif.AddressFamily.LINK:
                continue

            try:
                iface.remove_address(addr)
            except:
                # Continue anyway
                pass

        iface.down()

    def renew_lease(self, name):
        self.logger.info('Renewing IP lease on {0}'.format(name))
        if self.context.dhclient_running(name):
            pid = self.context.dhclient_pid(name)
            os.kill(pid, signal.SIGTERM)
            self.logger.info('Killed dhclient with pid {0}'.format(pid))

        time.sleep(1)
        return self.configure_interface(name)


class Main:
    def __init__(self):
        self.config = None
        self.client = None
        self.datastore = None
        self.configstore = None
        self.rtsock_thread = None
        self.logger = logging.getLogger('networkd')

    def dhclient_pid(self, interface):
        path = os.path.join('/var/run', 'dhclient.{0}.pid'.format(interface))
        if not os.path.exists(path):
            return None

        try:
            with open(path) as f:
                pid = int(f.read().strip())
                return pid
        except (IOError, ValueError):
            return None

    def dhclient_running(self, interface):
        pid = self.dhclient_pid(interface)
        if not pid:
            return False

        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def configure_dhcp(self, interface):
        # Check if dhclient is running
        if self.dhclient_running(interface):
            self.logger.info('Interface {0} already configured by DHCP'.format(interface))
            return True

        def unblock_signals():
            signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM, signal.SIGINT])

        ret = subprocess.call(['/sbin/dhclient', interface], close_fds=True, preexec_fn=unblock_signals)
        return ret == 0

    def interface_detached(self, name):
        self.logger.warn('Interface {0} detached from the system'.format(name))

    def interface_attached(self, name):
        self.logger.warn('Interface {0} attached to the system'.format(name))

    def using_dhcp_for_gateway(self):
        for i in self.datastore.query('network.interfaces'):
            if i.get('dhcp') and self.configstore.get('network.dhcp.assign_gateway'):
                    return True

        return False

    def scan_interfaces(self):
        self.logger.info('Scanning available network interfaces...')
        existing = []

        # Add newly plugged NICs to DB
        for i in list(netif.list_interfaces().values()):
            existing.append(i.name)

            # We want only physical NICs
            if i.cloned:
                continue

            if not self.datastore.exists('network.interfaces', ('id', '=', i.name)):
                self.logger.info('Found new interface {0} ({1})'.format(i.name, i.type.name))
                self.datastore.insert('network.interfaces', {
                    'enabled': False,
                    'id': i.name,
                    'type': i.type.name,
                    'dhcp': False,
                    'noipv6': False,
                    'rtadv': False,
                    'mtu': None,
                    'media': None,
                    'aliases': []
                })

        # Remove unplugged NICs from DB
        for i in self.datastore.query('network.interfaces', ('id', 'nin', existing), ('cloned', '=', False)):
            self.datastore.delete('network.interfaces', i['id'])

    def init_datastore(self):
        try:
            self.datastore = get_datastore(self.config)
        except DatastoreException as err:
            self.logger.error('Cannot initialize datastore: %s', str(err))
            sys.exit(1)

        self.configstore = ConfigStore(self.datastore)

    def connect(self, resume=False):
        while True:
            try:
                self.client.connect('unix:')
                self.client.login_service('networkd')
                self.client.enable_server()
                self.register_schemas()
                self.client.register_service('networkd.configuration', ConfigurationService(self))
                self.client.register_service('networkd.debug', DebugService())
                if resume:
                    self.client.resume_service('networkd.configuration')
                    self.client.resume_service('networkd.debug')

                return
            except (OSError, RpcException) as err:
                self.logger.warning('Cannot connect to dispatcher: {0}, retrying in 1 second'.format(str(err)))
                time.sleep(1)

    def init_dispatcher(self):
        def on_error(reason, **kwargs):
            if reason in (ClientError.CONNECTION_CLOSED, ClientError.LOGOUT):
                self.logger.warning('Connection to dispatcher lost')
                self.connect(resume=True)

        self.client = Client()
        self.client.on_error(on_error)
        self.connect()

    def init_routing_socket(self):
        self.rtsock_thread = RoutingSocketEventSource(self)
        self.rtsock_thread.start()

    def register_schemas(self):
        self.client.register_schema('network-aggregation-protocols', {
            'type': 'string',
            'enum': list(netif.AggregationProtocol.__members__.keys())
        })

        self.client.register_schema('network-interface-flags', {
            'type': 'array',
            'items': {
                'type': 'string',
                'enum': list(netif.InterfaceFlags.__members__.keys())
            }
        })

        self.client.register_schema('network-interface-capabilities', {
            'type': 'array',
            'items': {
                'type': 'string',
                'enum': list(netif.InterfaceCapability.__members__.keys())
            }
        })

        self.client.register_schema('network-interface-mediaopts', {
            'type': 'array',
            'items': {
                'type': 'string',
                'enum': list(netif.InterfaceMediaOption.__members__.keys())
            }
        })

        self.client.register_schema('network-interface-type', {
            'type': 'string',
            'enum': [
                'LOOPBACK',
                'ETHER',
                'VLAN',
                'BRIDGE',
                'LAGG'
            ]
        })

        self.client.register_schema('network-interface-status', {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'link_state': {'type': 'string'},
                'link_address': {'type': 'string'},
                'mtu': {'type': 'integer'},
                'media_type': {'type': 'string'},
                'media_subtype': {'type': 'string'},
                'media_options': {'$ref': 'network-interface-mediaopts'},
                'capabilities': {'$ref': 'network-interface-capabilities'},
                'flags': {'$ref': 'network-interface-flags'},
                'aliases': {
                    'type': 'array',
                    'items': {'$ref': 'network-interface-alias'}
                }
            }
        })

    def main(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('-c', metavar='CONFIG', default=DEFAULT_CONFIGFILE, help='Middleware config file')
        args = parser.parse_args()
        configure_logging('/var/log/networkd.log', 'DEBUG')
        setproctitle.setproctitle('networkd')
        self.config = args.c
        self.init_datastore()
        self.init_dispatcher()
        self.scan_interfaces()
        self.init_routing_socket()
        self.client.resume_service('networkd.configuration')
        self.client.resume_service('networkd.debug')
        self.logger.info('Started')
        self.client.wait_forever()

if __name__ == '__main__':
    m = Main()
    m.main()
