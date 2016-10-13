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

import logging
import errno
import netif
from binascii import crc32
from utils import uuid2, parse_uuid2
from bsd.nis import NIS
from freenas.utils.query import query
from plugin import DirectoryServicePlugin, DirectoryState

logger = logging.getLogger(__name__)


class NISPlugin(DirectoryServicePlugin):
    def __init__(self, context):
        self.context = context
        self.server = None
        self.domain_name = None
        self.server_name = None

    def _convert_user(self, entry):
        tmp = (self.domain_name or "").encode('utf-8')
        
        return {
            'id': str(uuid2(crc32(tmp), entry.pw_uid)),
            'uid': entry.pw_uid,
            'gid': entry.pw_gid,
            'builtin': False,
            'username': entry.pw_name,
            'full_name': entry.pw_gecos,
            'shell': entry.pw_shell,
            'home': entry.pw_dir,
            'sshpubkey': None,
            'group': str(uuid2(crc32(tmp), entry.pw_gid)),
            'groups': None,
            'sudo': False,
            }
    
    def _convert_group(self, entry):
        tmp = (self.domain_name or "").encode('utf-8')
        return {
            'id': str(uuid2(crc32(tmp), int(entry.gr_gid))),
            'gid': entry.gr_gid,
            'name': entry.gr_name,
            'parents': None,
            'builtin': False,
            'sudo': False,
            }
    
    def getpwent(self, filter=None, params=None):
        filter = filter or []
        filter.append(('uid', '!=', 0))
        return query((self._convert_user(pw) for pw in self.server.getpwent()),
                     *filter, **(params or {}))

    def getpwnam(self, name):
        if name == "root":
            return None
        return self._convert_user(self.server.getpwnam(name))

    def getpwuid(self, uid):
        if uid == 0:
            return None
        return self._convert_user(self.server.getpwuid(uid))

    def getpwuuid(self, uuid):
        tmp = (self.domain_name or "").encode('utf-8')
        (checksum, uid) = parse_uuid2(uuid)
        if crc32(tmp) != checksum:
            return None
        return self._convert_user(self.server.getpwuid(uid))

    def getgrent(self, filter=None, params=None):
        filter = filter or []
        filter.append(('gid', '!=', 0))
        return query((self._convert_group(gr) for gr in self.server.getgrent()),
                     *filter, **(params or {}))

    def getgrnam(self, name):
        return self._convert_group(self.server.getgrnam(name))

    def getgrgid(self, gid):
        if gid == 0:
            return None
        return self._convert_group(self.server.getgrgid(gid))

    def getgruuid(self, uuid):
        tmp = (self.domain_name or "").encode('utf-8')
        (checksum, gid) = parse_uuid2(uuid)
        if crc32(tmp) != checksum:
            return None
        tmp = self.server.getgrgid(gid)
        retval = self._convert_group(tmp)
        return retval

    def change_password(self, username, password):
        # Not currently implemented, or at least not implemented well
        raise OSError(errno.EPERM)

    def configure(self, enable, directory):
        directory.put_state(DirectoryState.JOINING)
        try:
            self.domain_name = directory.parameters["domain"]
        except KeyError:
            self.domain_name = None
        try:
            self.server_name = directory.parameters["server"]
        except KeyError:
            self.server_name = None

        netif.set_domainname(self.domain_name)
        self.server = NIS(self.domain_name, self.server_name)
        if self.server:
            directory.put_state(DirectoryState.BOUND)


def _init(context):
    context.register_plugin('nis', NISPlugin)

    context.register_schema('nis-directory-params', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            '%type': {'enum': ['nis-directory-params']},
            'server': {'type': 'string'},
            'domain': {'type': 'string'}
        }
    })
    
