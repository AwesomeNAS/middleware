#i+
# Copyright 2012 iXsystems, Inc.
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

import datetime
import os
import sys
import json


from django.conf import settings
from django.core import serializers
from django.contrib import auth
from django.utils.importlib import import_module

from freenasUI import account, network, plugins, services, sharing, storage, system
from freenasUI.middleware.notifier import notifier

from jsonrpc import jsonrpc_method
from subprocess import Popen, PIPE

from syslog import syslog, LOG_DEBUG


PLUGINS_API_VERSION = "0.1"


#
#    API utility functions
#
def __popen(cmd):
    return Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True, close_fds=True)


def __get_plugins_jail_info():
    jail_info = services.models.Plugins.objects.order_by("-pk")
    return jail_info[0] if jail_info else None


def __get_plugins_jail_path():
    jail_path = None
    jail_info = __get_plugins_jail_info()
    if jail_info:
        jail_path = jail_info.jail_path
    return jail_path

    
def __get_plugins_jail_name():
    jail_name = None
    jail_info = __get_plugins_jail_info()
    if jail_info:
        jail_name = jail_info.jail_name 
    return jail_name    


def __get_plugins_jail_full_path():
    jail_name = None
    jail_name = __get_plugins_jail_name()
    if not jail_name:
        return None

    jail_path = None
    jail_path = __get_plugins_jail_path()
    if not jail_path:
        return None

    jail_full_path = os.path.join(jail_path, jail_name)
    return jail_full_path


def __serialize(objects):
    return serializers.serialize("json", objects)


def __create(meth, model, prefix, keys, args, func=None, func_args=None):
    syslog(LOG_DEBUG, "%s: enter" % meth)

    kwargs = {}
    for k in keys:
        if args.has_key(k) and args[k]:
             kwargs[prefix + k] = args[k]

    obj = None
    if kwargs:
        try:
            obj = model.objects.create(**kwargs)
            obj.save()
            if func and func_args:
                func(func_args)
            elif func:
                func()

        except Exception, e:
            syslog(LOG_DEBUG, "%s: error = %s" % (meth, e))
            obj = None

    syslog(LOG_DEBUG, "%s: leave" % meth)
    return __serialize([obj])


def __destroy(meth, model, pk, func=None, func_args=None):
    syslog(LOG_DEBUG, "%s: enter" % meth)

    res = False

    if not pk:
        syslog(LOG_DEBUG, "%s: pk is null" % meth)
        return json.dumps(res)

    obj = model.objects.filter(pk=pk)
    if not obj:
        syslog(LOG_DEBUG, "%s: unable to find pk" % meth)
        return json.dumps(res)

    obj = obj[0]
    try: 
        obj.delete()
        if func and func_args:
            func(func_args)
        elif func:
            func()
        res = True

    except Exception, e:
        syslog(LOG_DEBUG, "%s: error = %s" % (meth, e))
        res = False
      
    syslog(LOG_DEBUG, "%s: leave" % meth)
    return json.dumps(res)


def __get(meth, model, prefix, keys, pk, args, related=False):
    syslog(LOG_DEBUG, "%s: enter" % meth)

    kwargs = {}
    if pk:
        kwargs["pk"] = pk
    for k in keys:
        if args.has_key(k) and args[k]:
             kwargs[prefix + k] = args[k]

    syslog(LOG_DEBUG, "%s: leave" % meth)
    if kwargs:
        return __serialize(model.objects.filter(**kwargs))

    else:
        return __serialize(model.objects.order_by("-pk"))


def __set(meth, model, prefix, keys, pk, args, func=None, func_args=None):
    syslog(LOG_DEBUG, "%s: enter" % meth)

    res = False

    if not pk:
        return json.dumps(res)

    obj = model.objects.filter(pk=pk)
    if not obj:
        syslog(LOG_DEBUG, "%s: unable to find pk" % meth)
        return json.dumps(res)

    obj = obj[0]

    kwargs = {} 
    if pk:
        kwargs["pk"] = pk 
    for k in keys:
        if args.has_key(k) and args[k]:
             kwargs[prefix + k] = args[k]

    try:
        obj.__dict__.update(kwargs)
        obj.save()
        if func and func_args: 
            func(func_args)
        elif func:
            func()
        res = True

    except Exception, e:
        syslog(LOG_DEBUG, "%s: error = %s" % (meth, e))
        res = False

    syslog(LOG_DEBUG, "%s: leave" % meth)
    return json.dumps(res)


def __api_call_not_implemented(request):
    return "not implemented"



#
#    API information methods
#
@jsonrpc_method("api.methods")
def __api_call_api_methods(request):
    api_methods = __plugins_api_call_table.keys()
    return sorted(api_methods)

@jsonrpc_method("api.version")
def __api_call_api_version(request):
    return PLUGINS_API_VERSION



#
#    Account methods
#
account_bsdgroups_keys = [ "gid", "group", "builtin" ]

@jsonrpc_method("account.bsdgroups.get")
def __account_bsdgroups_get(request, pk=None, gid=None, group=None, builtin=None):
    return __get("account.bsdgroups.get", account.models.bsdGroups,
        "bsdgrp_", account_bsdgroups_keys, pk, locals())

@jsonrpc_method("account.bsdgroups.set")
def __account_bsdgroups_set(request, pk=None, gid=None, group=None, builtin=None):
    return __set("account.bsdgroups.set", account.models.bsdGroups,
        "bsdgrp_", account_bsdgroups_keys, pk, locals(), lambda: notifier().reload("user"))

@jsonrpc_method("account.bsdgroups.create")
def __account_bsdgroups_create(request, gid=None, group=None, builtin=False):
    l = locals()
    if not gid:
        l["gid"] = notifier().user_getnextgid()

    return __create("account.bsdgroups.create", account.models.bsdGroups,
        "bsdgrp_", account_bsdgroups_keys, l, lambda: notifier().reload("user"))

@jsonrpc_method("account.bsdgroups.destroy")
def __account_bsdgroups_destroy(request, pk=None):
    return __destroy("account.bsdgroups.destroy",
        account.models.bsdGroups, pk, lambda: notifier().reload("user"))


account_bsdusers_keys = [ "uid", "username", "group", "home",
        "shell", "full_name", "builtin", "email" ]

@jsonrpc_method("account.bsdusers.get")
def __account_bsdusers_get(request, pk=None, uid=None, username=None, group=None,
        home=None, shell=None, full_name=None, builtin=None, email=None):
    return __get("account.bsdusers.get", account.models.bsdUsers,
        "bsdusr_", account_bsdusers_keys, pk, locals())

@jsonrpc_method("account.bsdusers.set")
def __account_bsdusers_set(request, pk=None, uid=None, username=None, group=None,
    unixhash=None, smbhash=None, home=None, shell=None, full_name=None, builtin=None, email=None):
    l = locals()

    res = False
    if not pk:
        return json.dumps(res)

    obj = account.models.bsdUsers.objects.filter(pk=pk)
    if not obj:
        return json.dumps(res)

    obj = obj[0]

    kwargs = {}
    keys = ["uid", "username", "group", "unixhash",
       "smbhash", "home", "shell", "full_name", "builtin", "email"]
    if pk:
        kwargs["pk"] = pk 
    for k in keys:
        if l.has_key(k) and l[k]:
             kwargs["bsdusr_" + k] = l[k]

    try:
        obj.__dict__.update(kwargs)
        obj.save()
        notifier().reload("user")
        res = True

    except Exception, e:
        syslog(LOG_DEBUG, "account.bsdusers.set: error = %s" % e)
        res = False

    return json.dumps(res)

@jsonrpc_method("account.bsdusers.create")
def __account_bsdusers_create(request, uid=-1, username=None, password=None, gid=-1,
    home="/nonexistent", shell="/usr/sbin/nologin", full_name=None, builtin=False, email=None):
    l = locals()

    if not (username or password or full_name):
        return json.dumps([None])

    kwargs = {}
    keys = ["uid", "username", "home", "shell",
        "full_name", "builtin", "email"]
    for k in keys:
        if l.has_key(k) and l[k]:
             kwargs["bsdusr_" + k] = l[k]

    uid, gid, unixhash, smbhash = notifier().user_create(
        username=username,
        fullname=full_name,
        password=password,
        uid=uid,
        gid=gid,
        shell=shell,
        homedir=home
    )

    try:
        grp = account.models.bsdGroups.objects.get(bsdgrp_gid=gid)

    except Exception, e:
        grp = account.models.bsdGroups(bsdgrp_gid=gid,
            bsdgrp_group=username, bsdgrp_builtin=False)
        grp.save()

    kwargs["bsdusr_uid"] = uid
    kwargs["bsdusr_group"] = grp
    kwargs["bsdusr_unixhash"] = unixhash
    kwargs["bsdusr_smbhash"] = smbhash
    
    try:
        obj = account.models.bsdUsers.objects.create(**kwargs)
        obj.save()
        notifier().reload("user")

    except Exception, e:
        syslog(LOG_DEBUG, "account.bsdusers.create: error = %s" % e)
        obj = None

    obj = account.models.bsdUsers.objects.select_related().filter(pk=obj.pk) if obj else [None]
    return __serialize(obj)

@jsonrpc_method("account.bsdusers.destroy")
def __account_bsdusers_destroy(request, pk=None):
    res = False

    if not pk:
        return json.dumps(res)

    obj = account.models.bsdUsers.objects.filter(pk=pk)
    if not obj:
        return json.dumps(res)

    obj = obj[0]

    grp_pk = obj.bsdusr_group.pk
    grp_obj = account.models.bsdGroups.objects.filter(pk=grp_pk)
    if grp_obj:
        grp_obj = grp_obj[0]

        try:
            grp_obj.delete()

        except Exception, e:
            syslog(LOG_DEBUG, "account.bsdusers.destroy: error = %s" % e)

    try: 
        obj.delete()
        notifier().reload("user")
        res = True

    except Exception, e:
        syslog(LOG_DEBUG, "account.bsdusers.destroy: error = %s" % e)
        res = False
      
    return json.dumps(res)


# XXX
@jsonrpc_method("account.bsdgroupmembership.get")
def __account_bsdgroupmembership_get(request):
    return __serialize(account.models.bsdGroupMembership.objects.order_by("-pk"))
@jsonrpc_method("account.bsdgroupmembership.set")
def __account_bsdgroupmembership_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("account.bsdgroupmembership.create")
def __account_bsdgroupmembership_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("account.bsdgroupmembership.destroy")
def __account_bsdgroupmembership_destroy(request):
    return __api_call_not_implemented(request)




#
#    Network methods
#
network_globalconfiguration_keys = [ "hostname", "domain", "ipv4gateway",
    "ipv6gateway", "nameserver1", "nameserver2", "nameserver3" ]

@jsonrpc_method("network.globalconfiguration.get")
def __network_globalconfiguration_get(request, pk=None, hostname=None, domain=None,
    ipv4gateway=None, ipv6gateway=None, nameserver1=None, nameserver2=None, nameserver3=None):
    return __get("network.globalconfiguration.get", network.models.GlobalConfiguration,
        "gc_", network_globalconfiguration_keys, pk, locals())

@jsonrpc_method("network.globalconfiguration.set")
def __network_globalconfiguration_set(request, pk=None, hostname=None, domain=None,
    ipv4gateway=None, ipv6gateway=None, nameserver1=None, nameserver2=None, nameserver3=None):
    return __set("network.globalconfiguration.set", network.models.GlobalConfiguration,
        "gc_", network_globalconfiguration_keys, pk, locals(), lambda: notifier().reload("networkgeneral"))

@jsonrpc_method("network.globalconfiguration.create")
def __network_globalconfiguration_create(request, hostname=None, domain=None,
    ipv4gateway=None, ipv6gateway=None, nameserver1=None, nameserver2=None, nameserver3=None):
    return __create("network.globalconfiguration.create", network.models.GlobalConfiguration,
        "gc_", network_globalconfiguration_keys, locals(), lambda: notifier().reload("networkgeneral"))

@jsonrpc_method("network.globalconfiguration.destroy")
def __network_globalconfiguration_destroy(request, pk=None):
    return __destroy("network.globalconfiguration.destroy",
        network.models.GlobalConfiguration, pk, lambda: notifier().reload("networkgeneral"))


network_interfaces_keys = [ "interface", "name", "dhcp", "ipv4address",
        "v4netmaskbit", "ipv6auto", "ipv6address", "v6netmaskbit", "options" ]

@jsonrpc_method("network.interfaces.get")
def __network_interfaces_get(request, pk=None, interface=None, name=None,
    dhcp=None, ipv4address=None, v4netmaskbit=None, ipv6auto=None,
    ipv6address=None, v6netmaskbit=None, options=None):
    return __get("network.interfaces.get", network.models.Interfaces,
        "int_", network_interfaces_keys, pk, locals())

@jsonrpc_method("network.interfaces.set")
def __network_interfaces_set(request, pk=None, interface=None, name=None,
    dhcp=None, ipv4address=None, v4netmaskbit=None, ipv6auto=None,
    ipv6address=None, v6netmaskbit=None, options=None):
    return __set("network.interfaces.set", network.models.Interfaces,
        "int_", network_interfaces_keys, pk, locals(), lambda: notifier().start("network"))

@jsonrpc_method("network.interfaces.create")
def __network_interfaces_create(request, interface=None, name=None,
    dhcp=None, ipv4address=None, v4netmaskbit=None, ipv6auto=None,
    ipv6address=None, v6netmaskbit=None, options=None):
    return __create("network.interfaces.create", network.models.Interfaces,
        "int_", network_interfaces_keys, locals(), lambda: mnotifier().start("network"))

@jsonrpc_method("network.interfaces.destroy")
def __network_interfaces_destroy(request, pk=None):
    return __destroy("network.interfaces.destroy",
        network.models.Interfaces, pk, lambda: notifier().start("network"))


# This section currently needs work
network_alias_keys = [ "interface", "v4address",
    "v4netmaskbit", "v6address", "v6netmaskbit" ]

@jsonrpc_method("network.alias.get")
def __network_alias_get(request, pk=None, interface=None, v4address=None, 
    v4netmaskbit=None, v6address=None, v6netmaskbit=None):
    return __get("network.alias.get", network.models.Alias,
        "alias_", network_alias_keys, pk, locals())

# XXX
@jsonrpc_method("network.alias.set")
def __network_alias_set(request, pk=None, interface=None, v4address=None,
    v4netmaskbit=None, v6address=None, v6netmaskbit=None):
    return __set("network.alias.set", network.models.Alias,
        "alias_", network_alias_keys, pk, locals(),
        lambda: notifier().start("network"))

# XXX
@jsonrpc_method("network.alias.create")
def __network_alias_create(request, interface=None, v4address=None,
    v4netmaskbit=None, v6address=None, v6netmaskbit=None):
    return __create("network.alias.create", network.models.Alias,
        "alias_", network_alias_keys, locals(),
        lambda: notifier().start("network"))

@jsonrpc_method("network.alias.destroy")
def __network_alias_destroy(request, pk=None):
    func = lambda: notifier().stop("netif");notifier().start("network")
    return __destroy("network.alias.destroy", network.models.Alias, pk, func)



@jsonrpc_method("network.vlan.get")
def __network_vlan_get(request):
    return __serialize(network.models.VLAN.objects.order_by("-pk"))
@jsonrpc_method("network.vlan.set")
def __network_vlan_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.vlan.create")
def __network_vlan_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.vlan.destroy")
def __network_vlan_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("network.lagginterface.get")
def __network_lagginterface_get(request):
    return __serialize(network.models.LAGGInterface.objects.order_by("-pk"))
@jsonrpc_method("network.lagginterface.set")
def __network_lagginterface_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.lagginterface.create")
def __network_lagginterface_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.lagginterface.destroy")
def __network_lagginterface_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("network.lagginterfacemembers.get")
def __network_lagginterfacemembers_get(request):
    return __serialize(network.models.LAGGInterfaceMembers.objects.order_by("-pk"))
@jsonrpc_method("network.lagginterfacemembers.set")
def __network_lagginterfacemembers_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.lagginterfacemembers.create")
def __network_lagginterfacemembers_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.lagginterfacemembers.destroy")
def __network_lagginterfacemembers_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("network.staticroute.get")
def __network_staticroute_get(request):
    return __serialize(network.models.StaticRoute.objects.order_by("-pk"))
@jsonrpc_method("network.staticroute.set")
def __network_staticroute_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.staticroute.create")
def __network_staticroute_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("network.staticroute.destroy")
def __network_staticroute_destroy(request):
    return __api_call_not_implemented(request)


#
#    Plugins methods
#
@jsonrpc_method("plugins.plugins.get")
def __plugins_plugins_get(request, plugin_name=None):
    if plugin_name:
        return __serialize(plugins.models.Plugins.objects.filter(plugin_name=plugin_name))
    else:
        return __serialize(plugins.models.Plugins.objects.order_by("-pk"))
@jsonrpc_method("plugins.plugins.set")
def __plugins_plugins_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("plugins.plugins.create")
def __plugins_plugins_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("plugins.plugins.destroy")
def __plugins_plugins_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("plugins.jail.info")
def __plugins_jail_info(request):
    return __serialize([__get_plugins_jail_info()])

@jsonrpc_method("plugins.is_authenticated")
def __plugins_is_authenticated(request, sessionid):
    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore(sessionid)

    try:
        user_id = session[auth.SESSION_KEY]
        backend_path = session[auth.BACKEND_SESSION_KEY]
        backend = auth.load_backend(backend_path)
        user = backend.get_user(user_id)
    except KeyError, e:
        return False
    if user and user.is_authenticated():
        return True
    return False


#
#    Services methods
#
services_services_keys = [ "service", "enable" ]

@jsonrpc_method("services.services.get")
def __services_services_get(request, pk=None, service=None, enable=None):
    return __get("services.services.get", services.models.services,
        "srv_", services_services_keys, pk, locals())

@jsonrpc_method("services.services.set")
def __services_services_set(request, pk=None, service=None, enable=None):
    return __set("services.services.set", services.models.services,
        "srv_", services_services_keys, pk, locals())

@jsonrpc_method("services.services.create")
def __services_services_create(request, service=None, enable=None):
    return __create("services.services.create", services.models.services,
        "srv_", services_services_keys, locals())

@jsonrpc_method("services.services.destroy")
def __services_services_destroy(request, pk=None):
    return __destroy("services.services.destroy",
        services.models.services, pk)



services_cifs_keys = [ "authmodel", "netbiosname", "workgroup", "description",
    "doscharset", "unixcharset", "loglevel", "localmaster", "timeserver",
    "guest", "guestok", "guestonly", "filemask", "dirmask", "largerw",
    "sendfile", "easupport", "dosattr", "nullpw", "smb_options",
    "homedir_enable", "homedir_browseable_enable", "homedir", "homedir_aux",
    "unixext", "aio_enable", "aio_rs", "aio_ws", "zeroconf" ]

@jsonrpc_method("services.cifs.get")
def __services_cifs_get(request, pk=None, authmodel=None, netbiosname=None,
    workgroup=None, description=None, doscharset=None, unixcharset=None,
    loglevel=None, localmaster=None, timeserver=None, guest=None, guestok=None,
    guestonly=None, filemask=None, dirmask=None, largerw=None, sendfile=None,
    easupport=None, dosattr=None, nullpw=None, smb_options=None, homedir_enable=None,
    homedir_browseable_enable=None, homedir=None, homedir_aux=None, unixext=None,
    aio_enable=None, aio_rs=None, aio_ws=None, zeroconf=None):
    return __get("services.cifs.get", services.models.CIFS,
        "cifs_srv_", services_cifs_keys, pk, locals())

@jsonrpc_method("services.cifs.set")
def __services_cifs_set(request, pk=None, authmodel=None, netbiosname=None,
    workgroup=None, description=None, doscharset=None, unixcharset=None,
    loglevel=None, localmaster=None, timeserver=None, guest=None, guestok=None,
    guestonly=None, filemask=None, dirmask=None, largerw=None, sendfile=None,
    easupport=None, dosattr=None, nullpw=None, smb_options=None, homedir_enable=None,
    homedir_browseable_enable=None, homedir=None, homedir_aux=None, unixext=None,
    aio_enable=None, aio_rs=None, aio_ws=None, zeroconf=None):
    return __set("services.cifs.set", services.models.CIFS, "cifs_srv_",
        services_cifs_keys, pk, locals(), lambda: notifier().reload("cifs"))

@jsonrpc_method("services.cifs.create")
def __services_cifs_create(request, authmodel=None, netbiosname=None,
    workgroup=None, description=None, doscharset=None, unixcharset=None,
    loglevel=None, localmaster=None, timeserver=None, guest=None, guestok=None,
    guestonly=None, filemask=None, dirmask=None, largerw=None, sendfile=None,
    easupport=None, dosattr=None, nullpw=None, smb_options=None, homedir_enable=None,
    homedir_browseable_enable=None, homedir=None, homedir_aux=None, unixext=None,
    aio_enable=None, aio_rs=None, aio_ws=None, zeroconf=None):
    return __create("services.cifs.create", services.models.CIFS, "cifs_srv_",
        services_cifs_keys, locals(), lambda: notifier().reload("cifs"))

@jsonrpc_method("services.cifs.destroy")
def __services_cifs_destroy(request, pk=None):
    return __destroy("services.cifs.destroy", services.models.CIFS,
        pk, lambda: notifier().reload("cifs"))



services_afp_keys = [ "name", "guest", "guest_user", "connections_limit" ]

@jsonrpc_method("services.afp.get")
def __services_afp_get(request, pk=None, name=None, guest_usser=None, connections_limit=None):
    return __get("services.afp.get", services.models.AFP,
        "afp_srv_", services_afp_keys, pk, locals())

@jsonrpc_method("services.afp.set")
def __services_afp_set(request, pk=None, name=None, guest_usser=None, connections_limit=None):
    return __set("services.afp.set", services.models.AFP, "afp_srv_",
        services_afp_keys, pk, locals(), lambda: notifier().reload("afp"))

@jsonrpc_method("services.afp.create")
def __services_afp_create(request, name=None, guest_usser=None, connections_limit=None):
    return __create("services.afp.create", services.models.AFP, "afp_srv_",
        services_afp_keys, locals(), lambda: notifier().reload("afp"))

@jsonrpc_method("services.afp.destroy")
def __services_afp_destroy(request, pk=None):
    return __destroy("services.afp.destroy", services.models.AFP,
        pk, lambda: notifier().reload("afp"))


services_nfs_keys = [ "servers", "async" ]

@jsonrpc_method("services.nfs.get")
def __services_nfs_get(request, pk=None, servers=None, async=None):
    return __get("services.nfs.get", services.models.NFS,
        "nfs_srv_", services_nfs_keys, pk, locals())

@jsonrpc_method("services.nfs.set")
def __services_nfs_set(request, pk=None, servers=None, async=None):
    return __set("services.nfs.set", services.models.NFS, "nfs_srv_",
        services_nfs_keys, pk, locals(), lambda: notifier().reload("nfs"))

@jsonrpc_method("services.nfs.create")
def __services_nfs_create(request, servers=None, async=None):
    return __create("services.nfs.create", services.models.NFS, "nfs_srv_",
        services_nfs_keys, locals(), lambda: notifier().reload("nfs"))

@jsonrpc_method("services.nfs.destroy")
def __services_nfs_destroy(request, pk=None):
    return __destroy("services.nfs.destroy", services.models.NFS,
        pk, lambda: notifier().reload("nfs"))



@jsonrpc_method("services.iscsitargetglobalconfiguration.get")
def __services_iscsitargetglobalconfiguration_get(request):
    return __serialize(sevices.models.iSCSITargetGlobalConfiguration.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargetglobalconfiguration.set")
def __services_iscsitargetglobalconfiguration_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetglobalconfiguration.create")
def __services_iscsitargetglobalconfiguration_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetglobalconfiguration.destroy")
def __services_iscsitargetglobalconfiguration_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitargetextent.get")
def __services_iscsitargetextent_get(request):
    return __serialize(services.iSCSITargetExtent.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargetextent.set")
def __services_iscsitargetextent_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetextent.create")
def __services_iscsitargetextent_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetextent.destroy")
def __services_iscsitargetextent_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitargetportal.get")
def __services_iscsitargetportal_get(request):
    return __serialize(services.models.iSCSITargetPortal.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargetportal.set")
def __services_iscsitargetportal_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetportal.create")
def __services_iscsitargetportal_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetportal.destroy")
def __services_iscsitargetportal_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitargetauthorizedinitiator.get")
def __services_iscsitargetauthorizedinitiator_get(request):
    return __serialize(services.models.iSCSITargetAuthorizedInitiator.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargetauthorizedinitiator.set")
def __services_iscsitargetauthorizedinitiator_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetauthorizedinitiator.create")
def __services_iscsitargetauthorizedinitiator_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetauthorizedinitiator.destroy")
def __services_iscsitargetauthorizedinitiator_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitargetauthcredential.get")
def __services_iscsitargetauthcredential_get(request):
    return __serialize(services.models.iSCSITargetAuthCredential.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargetauthcredential.set")
def __services_iscsitargetauthcredential_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetauthcredential.create")
def __services_iscsitargetauthcredential_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargetauthcredential.destroy")
def __services_iscsitargetauthcredential_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitarget.get")
def __services_iscsitarget_get(request):
    return __serialize(services.models.iSCSITarget.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitarget.set")
def __services_iscsitarget_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitarget.create")
def __services_iscsitarget_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitarget.destroy")
def __services_iscsitarget_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.iscsitargettoextent.get")
def __services_iscsitargettoextent_get(request):
    return __serialize(services.iSCSITargetToExtent.objects.order_by("-pk"))
@jsonrpc_method("services.iscsitargettoextent.set")
def __services_iscsitargettoextent_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargettoextent.create")
def __services_iscsitargettoextent_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.iscsitargettoextent.destroy")
def __services_iscsitargettoextent_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.dynamicdns.get")
def __services_dynamicdns_get(request):
    return __serialize(services.DynamicDNS.objects.order_by("-pk"))
@jsonrpc_method("services.dynamicdns.set")
def __services_dynamicdns_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.dynamicdns.create")
def __services_dynamicdns_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.dynamicdns.destroy")
def __services_dynamicdns_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.plugins.get")
def __services_plugins_get(request):
    return __serialize(services.models.Plugins.objects.order_by("-pk"))
@jsonrpc_method("services.plugins.set")
def __services_plugins_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.plugins.create")
def __services_plugins_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.plugins.destroy")
def __services_plugins_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.snmp.get")
def __services_snmp_get(request):
    return __serialize(services.models.SNMP.objects.order_by("-pk"))
@jsonrpc_method("services.snmp.set")
def __services_snmp_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.snmp.create")
def __services_snmp_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.snmp.destroy")
def __services_snmp_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.ups.get")
def __services_ups_get(request):
    return __serialize(services.models.UPS.objects.order_by("-pk"))
@jsonrpc_method("services.ups.set")
def __services_ups_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ups.create")
def __services_ups_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ups.destroy")
def __services_ups_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.ftp.get")
def __services_ftp_get(request):
    return __serialize(services.models.FTP.objects.order_by("-pk"))
@jsonrpc_method("services.ftp.set")
def __services_ftp_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ftp.create")
def __services_ftp_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ftp.destroy")
def __services_ftp_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.tftp.get")
def __services_tftp_get(request):
    return __serialize(services.models.TFTP.objects.order_by("-pk"))
@jsonrpc_method("services.tftp.set")
def __services_tftp_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.tftp.create")
def __services_tftp_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.tftp.destroy")
def __services_tftp_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.ssh.get")
def __services_ssh_get(request):
    return __serialize(services.models.SSH.objects.order_by("-pk"))
@jsonrpc_method("services.ssh.set")
def __services_ssh_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ssh.create")
def __services_ssh_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ssh.destroy")
def __services_ssh_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.activedirectory.get")
def __services_activedirectory_get(request):
    return __serialize(services.models.ActiveDirectory.objects.order_by("-pk"))
@jsonrpc_method("services.activedirectory.set")
def __services_activedirectory_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.activedirectory.create")
def __services_activedirectory_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.activedirectory.destroy")
def __services_activedirectory_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.ldap.get")
def __services_ldap_get(request):
    return __serialize(services.models.LDAP.objects.order_by("-pk"))
@jsonrpc_method("services.ldap.set")
def __services_ldap_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ldap.create")
def __services_ldap_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.ldap.destroy")
def __services_ldap_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.rsyncd.get")
def __services_rsyncd_get(request):
    return __serialize(services.models.Rsyncd.objects.order_by("-pk"))
@jsonrpc_method("services.rsyncd.set")
def __services_rsyncd_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.rsyncd.create")
def __services_rsyncd_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.rsyncd.destroy")
def __services_rsyncd_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.rsyncmod.get")
def __services_rsyncmod_get(request):
    return __serialize(services.models.RsyncMod.objects.order_by("-pk"))
@jsonrpc_method("services.rsyncmod.set")
def __services_rsyncmod_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.rsyncmod.create")
def __services_rsyncmod_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.rsyncmod.destroy")
def __services_rsyncmod_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("services.smart.get")
def __services_smart_get(request):
    return __serialize(services.models.SMART.objects.order_by("-pk"))
@jsonrpc_method("services.smart.set")
def __services_smart_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.smart.create")
def __services_smart_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("services.smart.destroy")
def __services_smart_destroy(request):
    return __api_call_not_implemented(request)



#
#    Sharing methods
#
sharing_cifs_keywords = [ "name", "comment", "path", "ro", "browsable", 
    "inheritowner", "inheritperms", "recyclebin", "showhiddenfiles",
    "guestok", "guestonly", "hostsallow", "hostsdeny", "auxsmbconf" ]

@jsonrpc_method("sharing.cifs_share.get")
def __sharing_cifs_share_get(request, pk=None, name=None, comment=None,
    path=None, ro=None, browsable=None, inheritowner=None, inheritperms=None,
    recyclebin=None, showhiddenfiles=None, guestok=None, guestonly=None,
    hostsallow=None, hostsdeny=None, auxsmbconf=None):
    return __get("sharing.cifs_share.get", sharing.models.CIFS_Share,
        "cifs_", sharing_cifs_keys, pk, locals())

@jsonrpc_method("sharing.cifs_share.set")
def __sharing_cifs_share_set(request, pk=None, name=None, comment=None,
    path=None, ro=None, browsable=None, inheritowner=None, inheritperms=None,
    recyclebin=None, showhiddenfiles=None, guestok=None, guestonly=None,
    hostsallow=None, hostsdeny=None, auxsmbconf=None):
    return __set("sharing.cifs_share.set", sharing.models.CIFS_Share, "cifs_",
        sharing_cifs_keys, pk, locals(), lambda: notifier().reload("cifs"))

@jsonrpc_method("sharing.cifs_share.create")
def __sharing_cifs_share_create(request, name=None, comment=None,
    path=None, ro=None, browsable=None, inheritowner=None, inheritperms=None,
    recyclebin=None, showhiddenfiles=None, guestok=None, guestonly=None,
    hostsallow=None, hostsdeny=None, auxsmbconf=None):
    return __create("sharing.cifs_share.create", sharing.models.CIFS_Share, "cifs_",
        sharing_cifs_keys, locals(), lambda: notifier().reload("cifs"))

@jsonrpc_method("sharing.cifs_share.destroy")
def __sharing_cifs_share_destroy(request, pk=None):
    return __destroy("sharing.cifs_share.destroy", sharing.models.CIFS_Share,
        pk, lambda: notifier().reload("cifs"))




@jsonrpc_method("sharing.afp_share.get")
def __sharing_afp_share_get(request):
    return __serialize(sharing.models.AFP_Share.objects.order_by("-pk"))
@jsonrpc_method("sharing.afp_share.set")
def __sharing_afp_share_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("sharing.afp_share.create")
def __sharing_afp_share_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("sharing.afp_share.destroy")
def __sharing_afp_share_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("sharing.nfs_share.get")
def __sharing_nfs_share_get(request):
    return __serialize(sharing.models.NFS_Share.objects.order_by("-pk"))
@jsonrpc_method("sharing.nfs_share.set")
def __sharing_nfs_share_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("sharing.nfs_share.create")
def __sharing_nfs_share_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("sharing.nfs_share.destroy")
def __sharing_nfs_share_destroy(request):
    return __api_call_not_implemented(request)



#
#    Storage methods
#
@jsonrpc_method("storage.volume.get")
def __storage_volume_get(request):
    return __serialize(storage.models.Volume.objects.order_by("-pk"))
@jsonrpc_method("storage.volume.set")
def __storage_volume_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.volume.create")
def __storage_volume_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.volume.destroy")
def __storage_volume_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("storage.disk.get")
def __storage_disk_get(request):
    return __serialize(storage.models.Disk.objects.order_by("-pk"))
@jsonrpc_method("storage.disk.set")
def __storage_disk_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.disk.create")
def __storage_disk_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.disk.destroy")
def __storage_disk_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("storage.mountpoint.get")
def __storage_mountpoint_get(request):
    return __serialize(storage.models.MountPoint.objects.order_by("-pk"))
@jsonrpc_method("storage.mountpoint.set")
def __storage_mountpoint_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.mountpoint.create")
def __storage_mountpoint_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.mountpoint.destroy")
def __storage_mountpoint_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("storage.replremote.get")
def __storage_replremote_get(request):
    return __serialize(storage.models.ReplRemote.objects.order_by("-pk"))
@jsonrpc_method("storage.replremote.set")
def __storage_replremote_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.replremote.create")
def __storage_replremote_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.replremote.destroy")
def __storage_replremote_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("storage.replication.get")
def __storage_replication_get(request):
    return __serialize(storage.models.Replication.objects.order_by("-pk"))
@jsonrpc_method("storage.replication.set")
def __storage_replication_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.replication.create")
def __storage_replication_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.replication.destroy")
def __storage_replication_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("storage.task.get")
def __storage_task_get(request):
    return __serialize(storage.models.Task.objects.order_by("-pk"))
@jsonrpc_method("storage.task.set")
def __storage_task_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.task.create")
def __storage_task_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("storage.task.destroy")
def __storage_task_destroy(request):
    return __api_call_not_implemented(request)



#
#    System methods
#
@jsonrpc_method("system.settings.get")
def __system_settings_get(request):
    return __serialize(system.models.Settings.objects.order_by("-pk"))
@jsonrpc_method("system.settings.set")
def __system_settings_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.settings.create")
def __system_settings_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.settings.destroy")
def __system_settings_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.ntpserver.get")
def __system_ntpserver_get(request):
    return __serialize(system.models.NTPServer.objects.order_by("-pk"))
@jsonrpc_method("system.ntpserver.set")
def __system_ntpserver_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.ntpserver.create")
def __system_ntpserver_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.ntpserver.destroy")
def __system_ntpserver_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.advanced.get")
def __system_advanced_get(request):
    return __serialize(system.models.Advanced.objects.order_by("-pk"))
@jsonrpc_method("system.advanced.set")
def __system_advanced_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.advanced.create")
def __system_advanced_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.advanced.destroy")
def __system_advanced_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.email.get")
def __system_email_get(request):
    return __serialize(system.models.Email.objects.order_by("-pk"))
@jsonrpc_method("system.email.set")
def __system_email_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.email.create")
def __system_email_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.email.destroy")
def __system_email_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.ssl.get")
def __system_ssl_get(request):
    return __serialize(system.models.SSL.objects.order_by("-pk"))
@jsonrpc_method("system.ssl.set")
def __system_ssl_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.ssl.create")
def __system_ssl_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.ssl.destroy")
def __system_ssl_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.cronjob.get")
def __system_cronjob_get(request):
    return __serialize(system.models.CronJob.objects.order_by("-pk"))
@jsonrpc_method("system.cronjob.set")
def __system_cronjob_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.cronjob.create")
def __system_cronjob_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.cronjob.destroy")
def __system_cronjob_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.rsync.get")
def __system_rsync_get(request):
    return __serialize(system.models.Rsync.objects.order_by("-pk"))
@jsonrpc_method("system.rsync.set")
def __system_rsync_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.rsync.create")
def __system_rsync_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.rsync.destroy")
def __system_rsync_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.smarttest.get")
def __system_smarttest_get(request):
    return __serialize(system.models.SMARTTest.objects.order_by("-pk"))
@jsonrpc_method("system.smarttest.set")
def __system_smarttest_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.smarttest.create")
def __system_smarttest_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.smarttest.destroy")
def __system_smarttest_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.sysctl.get")
def __system_sysctl_get(request):
    return __serialize(system.models.Sysctl.objects.order_by("-pk"))
@jsonrpc_method("system.sysctl.set")
def __system_sysctl_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.sysctl.create")
def __system_sysctl_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.sysctl.destroy")
def __system_sysctl_destroy(request):
    return __api_call_not_implemented(request)

@jsonrpc_method("system.tunable.get")
def __system_tunable_get(request):
    return __serialize(system.models.Tunable.objects.order_by("-pk"))
@jsonrpc_method("system.tunable.set")
def __system_tunable_set(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.tunable.create")
def __system_tunable_create(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("system.tunable.destroy")
def __system_tunable_destroy(request):
    return __api_call_not_implemented(request)



#
#    Database methods
#
@jsonrpc_method("db.query")
def __api_call_db_query_database(request):
    return __api_call_not_implemented(request)



#
#    Filesystem methods
#
@jsonrpc_method("fs.mountpoints.get")
def __fs_mountpoints_get(request):
    path_list = []
    mp_list = storage.models.MountPoint.objects.exclude(
        mp_volume__vol_fstype__exact='iscsi').select_related().all()

    for mp in mp_list: 
        path_list.append(mp.mp_path)
        datasets = mp.mp_volume.get_datasets()

        if datasets:
            for name, dataset in datasets.items():
                path_list.append(dataset.mountpoint)

    return path_list

@jsonrpc_method("fs.mounted.get")
def __fs_mounted_get(request, path=None):
    path_list = []

    cmd = "/sbin/mount -p"
    if path:
        cmd += " | /usr/bin/awk '/%s/ { print $0; }'" % path.replace("/", "\/")

    p = __popen(cmd)
    lines = p.communicate()[0].strip().split('\n')
    for line in lines:
        if not line:
            continue
        parts = line.split()
        if path and parts:
            dst = parts[1]
            i = dst.find(path)
            dst = dst[i:]
            parts[1] = dst
        path_list.append(parts)

    if p.returncode != 0:
        return None

    return path_list

@jsonrpc_method("fs.mount")
def __fs_mount_filesystem(request, src, dst):
    jail_path = __get_plugins_jail_full_path()
    if not jail_path:
        data = { "error": True, "message": "source or destination not specified" }
        return data

    if not src or not dst:
        data = { "error": True, "message": "source or destination not specified" }
        return data

    full_dst = "%s/%s" % (jail_path, dst)
    p = __popen("/sbin/mount_nullfs %s %s" % (src, full_dst))
    stdout, stderr = p.communicate()

    return {
        'error': False if p.returncode == 0 else True,
        'message': stderr,
        }


@jsonrpc_method("fs.umount")
def __fs_umount_filesystem(request, dst):
    jail_path = __get_plugins_jail_full_path()
    if not jail_path:
        data = { "error": True, "message": "plugins jail is not configured" }
        return data

    if not dst:
        data = { "error": True, "message": "destination not specified" }
        return data

    fs = "%s/%s" % (jail_path, dst)
    p = __popen("/sbin/umount %s" % fs)
    p.wait()

    return False if p.returncode != 0 else True


@jsonrpc_method("fs.directory.get")
def __fs_get_directory(request, path=None):
    files = None 
    if path:
        files = os.listdir(path) 

    return json.dumps(files)


@jsonrpc_method("fs.file.get")
def __fs_get_file(request, path=None):
    file = None
    if not path: 
        return file

    try:
        f = open(path, "r")

    except:
        return file

    file = f.readlines()  
    f.close()

    return json.dumps(file)


@jsonrpc_method("fs.file.set")
def __fs_set_file(request, path=None, data=None):
    return __api_call_not_implemented(request)

@jsonrpc_method("fs.file.create")
def __fs_set_file(request, path=None, data=None):
    return __api_call_not_implemented(request)

@jsonrpc_method("fs.filesystems.get")
def __fs_get_filesystems(request):
    return __api_call_not_implemented(request)


#
#    OS methods
#
@jsonrpc_method("os.query")
def  __os_query_system(request):
    return __api_call_not_implemented(request)
@jsonrpc_method("os.arch")
def __os_arch(request):
    pipe = Popen("/usr/bin/uname -m", stdin=PIPE, stdout=PIPE, stderr=PIPE,
        shell=True, close_fds=True)
    arch = pipe.stdout.read().strip()
    pipe.wait()
    return arch



#
#    Debug/Test/Null methods
#
@jsonrpc_method("api.test")
def __api_test(request):
    return True

@jsonrpc_method("api.debug")
def __api_debug(request):
    return True
