#
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
import subprocess
import base64


def run(context):
    for keytype in ('rsa', 'dsa', 'ecdsa', 'ed25519'):
        config = context.configstore
        private_key = config.get('service.sshd.keys_{0}_private'.format(keytype))
        public_key = config.get('service.sshd.keys_{0}_public'.format(keytype))
        cert_public_key = config.get('service.sshd.keys_{0}_certificate'.format(keytype))
        private_key_file = '/etc/ssh/ssh_host_{0}_key'.format(keytype)

        public_key_file = private_key_file + '.pub'
        cert_public_key_file = private_key_file + '-cert.pub'

        if private_key is None or public_key is None:
            if os.path.exists(private_key_file) and os.path.exists(public_key_file):
                return

            try:
                subprocess.check_call(['/usr/bin/ssh-keygen', '-q', '-t', keytype, '-f', private_key_file, '-N', ''])
                subprocess.check_call(['/usr/bin/ssh-keygen', '-l', '-f', public_key_file])
            except subprocess.CalledProcessError:
                raise

            # Save generated keys back to config db
            with open(private_key_file, 'rb') as fd:
                config.set('service.sshd.keys_{0}_private'.format(keytype), base64.b64encode(fd.read()))

            with open(public_key_file, 'rb') as fd:
                config.set('service.sshd.keys_{0}_public'.format(keytype), base64.b64encode(fd.read()))
        else:
            with open(private_key_file, 'wb') as fd:
                fd.write(base64.b64decode(private_key))
            os.chmod(private_key_file, 0o600)

            with open(public_key_file, 'wb') as fd:
                fd.write(base64.b64decode(public_key))

            if cert_public_key:
                with open(cert_public_key_file, 'wb') as fd:
                    fd.write(base64.b64decode(cert_public_key))
                context.emit_event('etcd.file_generated', {'filename': cert_public_key_file})

        context.emit_event('etcd.file_generated', {
            'filename': private_key_file
        })

        context.emit_event('etcd.file_generated', {
            'filename': public_key_file
        })
