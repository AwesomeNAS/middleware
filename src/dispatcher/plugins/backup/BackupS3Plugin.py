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

import os
import errno
import requests
import boto3
from task import Task, ProgressTask, TaskException, TaskDescription
from freenas.dispatcher.rpc import description
from freenas.utils import normalize


CHUNK_SIZE = 5 * 1024 * 1024


@description('Lists information about a specific S3 backup')
class BackupS3ListTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Listing information about S3 backup'

    def describe(self, backup):
        return TaskDescription('Listing information about S3 backup')

    def verify(self, backup):
        return []

    def run(self, backup):
        client = open_client(self.dispatcher, backup)
        result = []
        marker = None

        while True:
            ret = client.list_objects_v2(
                Bucket=backup['bucket'],
                Prefix='{0}/'.format(backup['folder']) if backup['folder'] else '',
                **({'ContinuationToken': marker} if marker else {})
            )

            for i in ret.get('Contents', []):
                result.append({
                    'name': i['Key'],
                    'size': i['Size'],
                    'content_type': None
                })

            if ret['IsTruncated']:
                marker = ret['NextContinuationToken']
                continue

            break

        return result


@description('Initializes a S3 backup')
class BackupS3InitTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Initializing S3 backup'

    def describe(self, backup):
        return TaskDescription('Initializing S3 backup')

    def verify(self, backup):
        return []

    def run(self, backup):
        normalize(backup['properties'], {
            'peer': None,
            'bucket': None,
            'folder': None
        })

        return backup['properties']


@description('Puts new data onto S3 backup')
class BackupS3PutTask(ProgressTask):
    @classmethod
    def early_describe(cls):
        return 'Putting new data onto S3 backup'

    def describe(self, backup, name, fd):
        return TaskDescription('Putting new data onto S3 backup {name}', name=name)

    def verify(self, backup, name, fd):
        return []

    def run(self, backup, name, fd):
        client = open_client(self.dispatcher, backup)
        folder = backup['folder'] or ''
        key = os.path.join(folder, name)
        parts = []
        idx = 1

        try:
            with os.fdopen(fd.fd, 'rb') as f:
                mp = client.create_multipart_upload(
                    Bucket=backup['bucket'],
                    Key=key
                )

                while True:
                    chunk = f.read(CHUNK_SIZE)
                    if chunk == b'':
                        break

                    resp = client.upload_part(
                        Bucket=backup['bucket'],
                        Key=key,
                        PartNumber=idx,
                        UploadId=mp['UploadId'],
                        ContentLength=CHUNK_SIZE,
                        Body=chunk
                    )

                    parts.append({
                        'ETag': resp['ETag'],
                        'PartNumber': idx
                    })

                    idx += 1

                client.complete_multipart_upload(
                    Bucket=backup['bucket'],
                    Key=key,
                    UploadId=mp['UploadId'],
                    MultipartUpload={
                        'Parts': parts
                    }
                )

        except Exception as err:
            raise TaskException(errno.EFAULT, 'Cannot put object: {0}'.format(str(err)))
        finally:
            pass


@description('Gets data from S3 backup')
class BackupS3GetTask(Task):
    @classmethod
    def early_describe(cls):
        return 'Getting data from S3 backup'

    def describe(self, backup, name, fd):
        return TaskDescription('Getting data from S3 backup {name}', name=name)

    def verify(self, backup, name, fd):
        return []

    def run(self, backup, name, fd):
        client = open_client(self.dispatcher, backup)
        folder = backup['folder'] or ''
        key = os.path.join(folder, name)
        url = client.generate_presigned_url(
            ClientMethod='get_object',
            Params={
                'Bucket': backup['bucket'],
                'Key': key
            }
        )

        req = requests.get(url)
        if req.status_code == 404:
            raise TaskException(errno.ENOENT, '{0} not found'.format(name))

        if req.status_code == 403:
            raise TaskException(errno.EPERM, 'Permission to {0} denied'.format(name))

        if req.status_code != 200:
            raise TaskException(errno.EINVAL, 'HTTP error code {0} while trying to access {1}'.format(
                req.status_code,
                name
            ))

        with os.fdopen(fd.fd, 'wb') as f:
            for chunk in req.iter_content(CHUNK_SIZE):
                f.write(chunk)


def open_client(dispatcher, backup):
    peer = dispatcher.call_sync('peer.query', [('id', '=', backup['peer'])], {'single': True})
    if not peer:
        raise TaskException(errno.ENOENT, 'Cannot find peer {0}'.format(backup['peer']))

    if peer['type'] != 'amazon-s3':
        raise TaskException(errno.EINVAL, 'Invalid peer type: {0}'.format(peer['type']))

    creds = peer['credentials']
    return boto3.client(
        's3',
        aws_access_key_id=creds['access_key'],
        aws_secret_access_key=creds['secret_key'],
        region_name=creds.get('region')
    )


def _depends():
    return ['BackupPlugin']


def _metadata():
    return {
        'type': 'backup',
        'method': 's3'
    }


def _init(dispatcher, plugin):
    plugin.register_schema_definition('backup-s3', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'type': {'enum': ['backup-s3']},
            'peer': {'type': 'string'},
            'bucket': {'type': ['string', 'null']},
            'folder': {'type': ['string', 'null']}
        }
    })

    plugin.register_task_handler('backup.s3.init', BackupS3InitTask)
    plugin.register_task_handler('backup.s3.list', BackupS3ListTask)
    plugin.register_task_handler('backup.s3.get', BackupS3GetTask)
    plugin.register_task_handler('backup.s3.put', BackupS3PutTask)
