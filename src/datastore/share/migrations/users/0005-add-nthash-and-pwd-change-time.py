#+
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

from datetime import datetime

# old smbhash format:
# "jakub:1000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX:EB0EFEB0EEB0EB0EB0EAB0EB0E0EEB0E:[U          ]:LCT-574F23E8:\n"


def probe(obj, ds):
    return 'smbhash' in obj


def apply(obj, ds):
    smbhash = obj.pop('smbhash', None)
    if not smbhash:
        obj.update({
            'nthash': None,
            'lmhash': None,
            'password_changed_at': None
        })
        return obj

    try:
        pieces = smbhash.strip().split(':')
        lmhash = pieces[2]
        nthash = pieces[3]
        lct = int(pieces[5].split('-')[1], 16)
    except:
        lmhash = None
        nthash = None
        lct = None

    obj.update({
        'lmhash': lmhash,
        'nthash': nthash,
        'password_changed_at': datetime.fromtimestamp(lct)
    })
    return obj
