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
import errno
import logging
import libzfs
from datetime import datetime
from threading import Event
from cache import EventCacheStore
from task import (Provider, Task, TaskStatus, TaskException,
                  VerifyException, TaskAbortException, query)
from freenas.dispatcher.rpc import RpcException, accepts, returns, description
from freenas.dispatcher.rpc import SchemaHelper as h
from balancer import TaskState
from resources import Resource
from freenas.utils import first_or_default
from freenas.utils.query import QueryDict, wrap


logger = logging.getLogger('ZfsPlugin')
pools = None
datasets = None
snapshots = None


@description("Provides information about ZFS pools")
class ZpoolProvider(Provider):
    @description("Lists ZFS pools")
    @query('zfs-pool')
    def query(self, filter=None, params=None):
        return pools.query(*(filter or []), **(params or {}))

    @accepts()
    @returns(h.array(h.ref('zfs-pool')))
    def find(self):
        zfs = get_zfs()
        return list([p.__getstate__() for p in zfs.find_import()])

    @accepts()
    @returns(h.ref('zfs-pool'))
    def get_boot_pool(self):
        name = self.configstore.get('system.boot_pool_name')
        return pools[name]

    @accepts(str)
    @returns(h.array(str))
    def get_disks(self, name):
        pool = pools.get(name)
        if not pool:
            raise RpcException(errno.ENOENT, 'Pool {0} not found'.format(name))

        return [v['path'] for v in iterate_vdevs(pool['groups'])]

    @returns(h.object())
    def get_capabilities(self):
        return {
            'vdev_types': {
                'disk': {
                    'min_devices': 1,
                    'max_devices': 1
                },
                'mirror': {
                    'min_devices': 2
                },
                'raidz1': {
                    'min_devices': 2
                },
                'raidz2': {
                    'min_devices': 3
                },
                'raidz3': {
                    'min_devices': 4
                },
                'spare': {
                    'min_devices': 1
                }
            },
            'vdev_groups': {
                'data': {
                    'allowed_vdevs': ['disk', 'file', 'mirror', 'raidz1', 'raidz2', 'raidz3', 'spare']
                },
                'log': {
                    'allowed_vdevs': ['disk', 'mirror']
                },
                'cache': {
                    'allowed_vdevs': ['disk']
                }
            }
        }

    @accepts(str, str)
    @returns(h.ref('zfs-vdev'))
    def vdev_by_guid(self, name, guid):
        pool = pools.get(name)
        return first_or_default(lambda v: v['guid'] == guid, iterate_vdevs(pool['groups']))

    @accepts(str, str)
    @returns(h.ref('zfs-vdev'))
    def vdev_by_path(self, name, path):
        pool = pools.get(name)
        return first_or_default(lambda v: v['path'] == path, iterate_vdevs(pool['groups']))

    @accepts(str)
    @returns()
    def ensure_resilvered(self, name):
        try:
            zfs = get_zfs()
            pool = zfs.get(name)

            self.dispatcher.test_or_wait_for_event(
                'fs.zfs.resilver.finished',
                lambda args: args['guid'] == str(pool.guid),
                lambda:
                    pool.scrub.state == libzfs.ScanState.SCANNING and
                    pool.scrub.function == libzfs.ScanFunction.RESILVER
                )

        except libzfs.ZFSException as err:
            raise RpcException(errno.EFAULT, str(err))


class ZfsDatasetProvider(Provider):
    @query('zfs-dataset')
    def query(self, filter=None, params=None):
        return datasets.query(*(filter or []), **(params or {}))

    @accepts(str)
    @returns(h.array(
        h.one_of(
            h.ref('zfs-dataset'),
            h.ref('zfs-snapshot')
        )
    ))
    def get_dependencies(self, dataset_name):
        try:
            zfs = get_zfs()
            ds = zfs.get_dataset(dataset_name)
            deps = list(ds.dependents)
            return deps
        except libzfs.ZFSException as err:
            if err.code == libzfs.Error.NOENT:
                raise RpcException(errno.ENOENT, str(err))

            raise RpcException(errno.EFAULT, str(err))

    @accepts(str)
    @returns(h.array(h.ref('zfs-snapshot')))
    def get_snapshots(self, dataset_name):
        try:
            zfs = get_zfs()
            ds = zfs.get_dataset(dataset_name)
            snaps = list(ds.snapshots)
            snaps.sort(key=lambda s: int(s.properties['creation'].rawvalue))
            return snaps
        except libzfs.ZFSException as err:
            if err.code == libzfs.Error.NOENT:
                raise RpcException(errno.ENOENT, str(err))

            raise RpcException(errno.EFAULT, str(err))

    @returns(int)
    def estimate_send_size(self, dataset_name, snapshot_name, anchor_name=None):
        try:
            zfs = get_zfs()
            ds = zfs.get_object('{0}@{1}'.format(dataset_name, snapshot_name))
            if anchor_name:
                return ds.get_send_space('{0}@{1}'.format(dataset_name, anchor_name))

            return ds.get_send_space()
        except libzfs.ZFSException as err:
            raise RpcException(errno.EFAULT, str(err))


class ZfsSnapshotProvider(Provider):
    @query('zfs-snapshot')
    def query(self, filter=None, params=None):
        return snapshots.query(*(filter or []), **(params or {}))


@description("Scrubs ZFS pool")
@accepts(str, int)
class ZpoolScrubTask(Task):
    def __init__(self, dispatcher, datastore):
        super(ZpoolScrubTask, self).__init__(dispatcher, datastore)
        self.pool = None
        self.started = False
        self.finish_event = Event()
        self.abort_flag = False

    def __scrub_finished(self, args):
        if args["pool"] == self.pool:
            self.state = TaskState.FINISHED
            self.finish_event.set()

    def __scrub_aborted(self, args):
        if args["pool"] == self.pool:
            self.state = TaskState.ABORTED
            self.finish_event.set()

    def describe(self, pool):
        return "Scrubbing pool {0}".format(pool)

    def verify(self, pool, threshold=None):
        zfs = get_zfs()
        pool = zfs.get(pool)
        return get_disk_names(self.dispatcher, pool)

    def run(self, pool, threshold=None):
        self.pool = pool
        self.dispatcher.register_event_handler("fs.zfs.scrub.finish", self.__scrub_finished)
        self.dispatcher.register_event_handler("fs.zfs.scrub.abort", self.__scrub_aborted)
        self.finish_event.clear()

        try:
            zfs = get_zfs()
            pool = zfs.get(self.pool)
            # Skip in case a scrub did already run in the last `threshold` days
            if threshold:
                last_run = pool.scrub.end_time
                if last_run and (datetime.now() - last_run).days < threshold:
                    self.finish_event.set()
                    return
            pool.start_scrub()
            self.started = True
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))

        self.finish_event.wait()
        if self.abort_flag:
            raise TaskAbortException(errno.EINTR, str("User invoked Task.abort()"))

    def abort(self):
        try:
            zfs = get_zfs()
            pool = zfs.get(self.pool)
            pool.stop_scrub()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))

        self.finish_event.set()
        # set the abort flag to True so that run() can raise
        # propoer exception
        self.abort_flag = True
        return True

    def get_status(self):
        if not self.started:
            return TaskStatus(0, "Waiting to start...")

        try:
            zfs = get_zfs()
            pool = zfs.get(self.pool)
            scrub = pool.scrub
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))

        if scrub.state == libzfs.ScanState.SCANNING:
            self.progress = scrub.percentage
            return TaskStatus(self.progress, "In progress...")

        if scrub.state == libzfs.ScanState.CANCELED:
            self.finish_event.set()
            return TaskStatus(self.progress, "Canceled")

        if scrub.state == libzfs.ScanState.FINISHED:
            self.finish_event.set()
            return TaskStatus(100, "Finished")


@description("Creates new ZFS pool")
@accepts(str, h.ref('zfs-topology'), h.object())
class ZpoolCreateTask(Task):
    def __partition_to_disk(self, part):
        result = self.dispatcher.call_sync('disk.get_partition_config', part)
        return os.path.basename(result['disk'])

    def __get_disks(self, topology):
        result = []
        for gname, vdevs in list(topology.items()):
            for vdev in vdevs:
                if vdev['type'] == 'disk':
                    result.append(self.__partition_to_disk(vdev['path']))
                    continue

                if 'children' in vdev:
                    result += [self.__partition_to_disk(i['path']) for i in vdev['children']]

        return ['disk:{0}'.format(d) for d in result]

    def verify(self, name, topology, params=None):
        zfs = get_zfs()
        if name in zfs.pools:
            raise VerifyException(errno.EEXIST, 'Pool with same name already exists')

        return self.__get_disks(topology)

    def run(self, name, topology, params=None):
        params = params or {}
        zfs = get_zfs()
        mountpoint = params.get('mountpoint')

        if not mountpoint:
            raise TaskException(errno.EINVAL, 'Please supply valid "mountpoint" parameter')

        opts = {
            'feature@async_destroy': 'enabled',
            'feature@empty_bpobj': 'enabled',
            'feature@lz4_compress': 'enabled',
            'feature@enabled_txg': 'enabled',
            'feature@extensible_dataset': 'enabled',
            'feature@bookmarks': 'enabled',
            'feature@filesystem_limits': 'enabled',
            'feature@embedded_data': 'enabled',
            'cachefile': '/data/zfs/zpool.cache',
            'failmode': 'continue',
            'autoexpand': 'on',
        }

        fsopts = {
            'compression': 'lz4',
            'aclmode': 'passthrough',
            'aclinherit': 'passthrough',
            'mountpoint': mountpoint
        }

        nvroot = convert_topology(zfs, topology)

        try:
            pool = zfs.create(name, nvroot, opts, fsopts)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


class ZpoolBaseTask(Task):
    def verify(self, *args, **kwargs):
        name = args[0]
        try:
            zfs = get_zfs()
            pool = zfs.get(name)
        except libzfs.ZFSException:
            raise VerifyException(errno.ENOENT, "Pool {0} not found".format(name))

        return get_disk_names(self.dispatcher, pool)


@accepts(str, h.object())
class ZpoolConfigureTask(ZpoolBaseTask):
    def verify(self, pool, updated_props):
        super(ZpoolConfigureTask, self).verify(pool)

    def run(self, pool, updated_props):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            for name, value in updated_props:
                prop = pool.properties[name]
                prop.value = value
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str)
class ZpoolDestroyTask(ZpoolBaseTask):
    def run(self, name):
        try:
            zfs = get_zfs()
            zfs.destroy(name)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(
    str,
    h.any_of(
        h.ref('zfs-topology'),
        None
    ),
    h.any_of(
        h.array(h.ref('zfs-vdev-extension')),
        None
    )
)
class ZpoolExtendTask(ZpoolBaseTask):
    def __init__(self, dispatcher, datastore):
        super(ZpoolExtendTask, self).__init__(dispatcher, datastore)
        self.pool = None
        self.started = False

    def run(self, pool, new_vdevs, updated_vdevs):
        try:
            self.pool = pool
            zfs = get_zfs()
            pool = zfs.get(pool)

            if new_vdevs:
                nvroot = convert_topology(zfs, new_vdevs)
                pool.attach_vdevs(nvroot)

            if updated_vdevs:
                for i in updated_vdevs:
                    vdev = pool.vdev_by_guid(int(i['target_guid']))
                    if not vdev:
                        raise TaskException(errno.ENOENT, 'Vdev with GUID {0} not found'.format(i['target_guid']))

                    new_vdev = libzfs.ZFSVdev(zfs, i['vdev']['type'])
                    new_vdev.path = i['vdev']['path']
                    vdev.attach(new_vdev)

                # Wait for resilvering process to complete
                self.started = True
                self.dispatcher.test_or_wait_for_event(
                    'fs.zfs.resilver.finished',
                    lambda args: args['guid'] == str(pool.guid),
                    lambda:
                        pool.scrub.state == libzfs.ScanState.SCANNING and
                        pool.scrub.function == libzfs.ScanFunction.RESILVER
                )

        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))

    def get_status(self):
        if not self.started:
            return TaskStatus(0, "Waiting to start...")

        try:
            zfs = get_zfs()
            pool = zfs.get(self.pool)
            scrub = pool.scrub
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))

        if scrub.state == libzfs.ScanState.SCANNING:
            self.progress = scrub.percentage
            return TaskStatus(self.progress, "Resilvering in progress...")

        if scrub.state == libzfs.ScanState.FINISHED:
            return TaskStatus(100, "Finished")


@accepts(str, str)
class ZpoolDetachTask(ZpoolBaseTask):
    def run(self, pool, guid):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            vdev = pool.vdev_by_guid(int(guid))
            if not vdev:
                raise TaskException(errno.ENOENT, 'Vdev with GUID {0} not found'.format(guid))

            vdev.detach()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, h.ref('zfs-vdev'))
class ZpoolReplaceTask(ZpoolBaseTask):
    def run(self, pool, guid, vdev):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            ovdev = pool.vdev_by_guid(int(guid))
            if not vdev:
                raise TaskException(errno.ENOENT, 'Vdev with GUID {0} not found'.format(guid))

            new_vdev = libzfs.ZFSVdev(zfs, vdev['type'])
            new_vdev.path = vdev['path']
            ovdev.replace(new_vdev)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, bool)
class ZpoolOfflineDiskTask(ZpoolBaseTask):
    def run(self, pool, guid, temporary=False):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            vdev = pool.vdev_by_guid(int(guid))
            if not vdev:
                raise TaskException(errno.ENOENT, 'Vdev with GUID {0} not found'.format(guid))

            vdev.offline(temporary)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str)
class ZpoolOnlineDiskTask(ZpoolBaseTask):
    def run(self, pool, guid):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            vdev = pool.vdev_by_guid(int(guid))
            if not vdev:
                raise TaskException(errno.ENOENT, 'Vdev with GUID {0} not found'.format(guid))

            vdev.online()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str)
class ZpoolUpgradeTask(ZpoolBaseTask):
    def run(self, pool):
        try:
            zfs = get_zfs()
            pool = zfs.get(pool)
            pool.upgrade()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, h.object())
class ZpoolImportTask(Task):
    def verify(self, guid, name=None, properties=None):
        zfs = get_zfs()
        pool = first_or_default(lambda p: str(p.guid) == guid, zfs.find_import())
        if not pool:
            raise VerifyException(errno.ENOENT, 'Pool with GUID {0} not found'.format(guid))

        return get_disk_names(self.dispatcher, pool)

    def run(self, guid, name=None, properties=None):
        zfs = get_zfs()
        opts = properties or {}
        try:
            pool = first_or_default(lambda p: str(p.guid) == guid, zfs.find_import())
            zfs.import_pool(pool, name, opts)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str)
class ZpoolExportTask(ZpoolBaseTask):
    def verify(self, name):
        super(ZpoolExportTask, self).verify(name)

    def run(self, name):
        zfs = get_zfs()
        try:
            pool = zfs.get(name)
            zfs.export_pool(pool)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


class ZfsBaseTask(Task):
    def verify(self, *args, **kwargs):
        path = args[0]
        try:
            zfs = get_zfs()
            dataset = zfs.get_object(path)
        except libzfs.ZFSException as err:
            raise TaskException(errno.ENOENT, str(err))

        return ['zpool:{0}'.format(dataset.pool.name)]


@accepts(str, bool)
class ZfsDatasetMountTask(ZfsBaseTask):
    def run(self, name, recursive=False):
        try:
            zfs = get_zfs()
            dataset = zfs.get_dataset(name)
            if dataset.mountpoint:
                logger.warning('{0} dataset already mounted at {1}'.format(name, dataset.mountpoint))
                return

            if recursive:
                dataset.mount_recursive()
            else:
                dataset.mount()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str)
class ZfsDatasetUmountTask(ZfsBaseTask):
    def run(self, name):
        try:
            zfs = get_zfs()
            dataset = zfs.get_dataset(name)
            dataset.umount()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, h.ref('dataset-type'), h.object())
class ZfsDatasetCreateTask(Task):
    def check_type(self, type):
        try:
            self.type = getattr(libzfs.DatasetType, type)
        except AttributeError:
            raise VerifyException(errno.EINVAL, 'Invalid dataset type: {0}'.format(type))

    def verify(self, pool_name, path, type, params=None):
        if not pool_exists(pool_name):
            raise VerifyException('Pool {0} not found'.format(pool_name))

        self.check_type(type)
        return ['zpool:{0}'.format(pool_name)]

    def run(self, pool_name, path, type, params=None):
        self.check_type(type)
        try:
            params = params or {}
            sparse = False

            if params.get('sparse'):
                sparse = True
                del params['sparse']

            zfs = get_zfs()
            pool = zfs.get(pool_name)
            pool.create(path, params, fstype=self.type, sparse_vol=sparse)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, str, h.any_of(bool, None), h.any_of(h.object(), None))
class ZfsSnapshotCreateTask(ZfsBaseTask):
    def run(self, pool_name, path, snapshot_name, recursive=False, params=None):
        if params:
            params = {k: v['value'] for k, v in params.items()}

        try:
            zfs = get_zfs()
            ds = zfs.get_dataset(path)
            ds.snapshot('{0}@{1}'.format(path, snapshot_name), recursive=recursive, fsopts=params)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, str, h.any_of(bool, None))
class ZfsSnapshotDeleteTask(ZfsBaseTask):
    def run(self, pool_name, path, snapshot_name, recursive=False):
        try:
            zfs = get_zfs()
            snap = zfs.get_snapshot('{0}@{1}'.format(path, snapshot_name))
            snap.delete(recursive)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str, h.array(str), h.any_of(bool, None))
class ZfsSnapshotDeleteMultipleTask(ZfsBaseTask):
    def run(self, pool_name, path, snapshot_names, recursive=False):
        try:
            zfs = get_zfs()
            for i in snapshot_names:
                snap = zfs.get_snapshot('{0}@{1}'.format(path, i))
                snap.delete(recursive)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


class ZfsConfigureTask(ZfsBaseTask):
    def run(self, pool_name, name, properties):
        try:
            zfs = get_zfs()
            dataset = zfs.get_dataset(name)
            for k, v in list(properties.items()):
                if k in dataset.properties:
                    if v['value'] is None:
                        dataset.properties[k].inherit()
                    else:
                        dataset.properties[k].value = v['value']
                else:
                    prop = libzfs.ZFSUserProperty(v['value'])
                    dataset.properties[k] = prop
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


class ZfsDestroyTask(ZfsBaseTask):
    def run(self, name):
        try:
            zfs = get_zfs()
            dataset = zfs.get_object(name)
            dataset.delete()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str, str)
class ZfsRenameTask(ZfsBaseTask):
    def run(self, name, new_name):
        try:
            zfs = get_zfs()
            dataset = zfs.get_object(name)
            dataset.rename(new_name)
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


@accepts(str)
class ZfsCloneTask(ZfsBaseTask):
    def run(self, path):
        try:
            zfs = get_zfs()
            dataset = zfs.get_dataset(path)
            dataset.delete()
        except libzfs.ZFSException as err:
            raise TaskException(errno.EFAULT, str(err))


def convert_topology(zfs, topology):
    nvroot = {}
    for group, vdevs in topology.items():
        nvroot[group] = []
        for i in vdevs:
            vdev = libzfs.ZFSVdev(zfs, "disk")
            vdev.type = i['type']

            if i['type'] == 'disk':
                vdev.path = i['path']

            if 'children' in i:
                ret = []
                for c in i['children']:
                    cvdev = libzfs.ZFSVdev(zfs, "disk")
                    cvdev.type = c['type']
                    cvdev.path = c['path']
                    ret.append(cvdev)

                vdev.children = ret

            nvroot[group].append(vdev)

    return nvroot


def pool_exists(pool):
    try:
        zfs = get_zfs()
        return zfs.get(pool) is not None
    except libzfs.ZFSException:
        return False


def iterate_vdevs(topology):
    for grp in topology.values():
        for vdev in grp:
            if vdev['type'] == 'disk':
                yield vdev
                continue

            if 'children' in vdev:
                for child in vdev['children']:
                    yield child


def get_disk_names(dispatcher, pool):
    ret = []
    for x in pool.disks:
        try:
            d = dispatcher.call_sync('disk.partition_to_disk', x)
        except RpcException:
            continue

        ret.append('disk:' + d)

    return ret


def sync_zpool_cache(dispatcher, pool, guid=None):
    zfs = get_zfs()
    try:
        zfspool = wrap(zfs.get(pool).__getstate__())
        pools.put(pool, zfspool)
        zpool_sync_resources(dispatcher, pool)
    except libzfs.ZFSException as e:
        if e.code == libzfs.Error.NOENT:
            pools.remove(pool)
            datasets.remove_predicate(lambda i: i['pool'] == pool)
            return

        logger.warning("Cannot read pool status from pool {0}".format(pool))


def sync_dataset_cache(dispatcher, dataset, old_dataset=None):
    zfs = get_zfs()
    pool = dataset.split('/')[0]
    sync_zpool_cache(dispatcher, pool)
    try:
        if old_dataset:
            datasets.rename(old_dataset, dataset)
            return

        if datasets.put(dataset, wrap(zfs.get_dataset(dataset).__getstate__(recursive=False))):
            dispatcher.register_resource(
                Resource('zfs:{0}'.format(dataset)),
                parents=['zpool:{0}'.format(pool)])

    except libzfs.ZFSException as e:
        if e.code == libzfs.Error.NOENT:
            if datasets.remove(dataset):
                datasets.remove_predicate(lambda i: i['name'].startswith(dataset + '/'))
                dispatcher.unregister_resource('zfs:{0}'.format(dataset))

            return

        logger.warning("Cannot read dataset status from dataset {0}".format(dataset))


def sync_snapshot_cache(dispatcher, snapshot, old_snapshot=None):
    zfs = get_zfs()
    try:
        if old_snapshot:
            snapshots.rename(old_snapshot, snapshot)
            return

        snapshots.put(snapshot, wrap(zfs.get_snapshot(snapshot).__getstate__()))
    except libzfs.ZFSException as e:
        if e.code == libzfs.Error.NOENT:
            snapshots.remove(snapshot)
            return

        logger.warning("Cannot read snapshot status from snapshot {0}".format(snapshot))


def zpool_sync_resources(dispatcher, name):
    res_name = 'zpool:{0}'.format(name)

    try:
        zfs = get_zfs()
        pool = zfs.get(name)
    except libzfs.ZFSException:
        dispatcher.unregister_resource(res_name)
        return

    if dispatcher.resource_exists(res_name):
        dispatcher.update_resource(
            res_name,
            new_parents=get_disk_names(dispatcher, pool))
    else:
        dispatcher.register_resource(
            Resource(res_name),
            parents=get_disk_names(dispatcher, pool))


def zpool_try_clear(name, vdev):
    zfs = get_zfs()
    pool = zfs.get(name)
    if pool.clear():
        logger.info('Device {0} reattached successfully to pool {1}'.format(vdev['path'], name))
        return

    logger.warning('Device {0} reattach to pool {1} failed'.format(vdev['path'], name))


def get_zfs():
    return libzfs.ZFS(history=True, history_prefix="[DISPATCHER TASK]")


def _depends():
    return ['DevdPlugin', 'DiskPlugin']


def zfsprop_schema_creator(**kwargs):
    """
    A little helper function to programmatically create zfs property type
    schmeas. It returns a schema dict with top level 'type' being an object.

    Note: If nothing is specified then it defaults to a source='string'
    and value='string'.

    Usage: zfsprop_schema_creator(propety_name=schema_type_as_str)
    Examples:
        Call: zfsprop_schema_creator(value='integer')
        Returns: {
            type: 'object',
            properties: {
                'source': {'type': 'string'},
                'value': {'type': 'integer'},
            }
        }
        zfsprop_schema_creator(source='string', value='integer')
        Returns: {
            type: 'object',
            properties: {
                'source': {'type': 'string'},
                'value': {'type': 'integer'},
            }
        }
    """
    result = {
        'type': 'object',
        'properties': {
            'source': 'string',
            'value': 'string',
        }
    }
    for key, value in kwargs.items():
        result['properties'][key] = {'type': value}
    return result


def _init(dispatcher, plugin):
    def on_pool_create(args):
        logger.info('New pool created: {0} <{1}>'.format(args['pool'], args['guid']))
        with dispatcher.get_lock('zfs-cache'):
            sync_zpool_cache(dispatcher, args['pool'], args['guid'])

    def on_pool_destroy(args):
        logger.info('Pool {0} <{1}> destroyed'.format(args['pool'], args['guid']))
        with dispatcher.get_lock('zfs-cache'):
            sync_zpool_cache(dispatcher, args['pool'], args['guid'])

    def on_pool_changed(args):
        with dispatcher.get_lock('zfs-cache'):
            sync_zpool_cache(dispatcher, args['pool'], args['guid'])

    def on_pool_reguid(args):
        logger.info('Pool {0} changed guid to <{1}>'.format(args['pool'], args['guid']))

    def on_dataset_create(args):
        with dispatcher.get_lock('zfs-cache'):
            if '@' in args['ds']:
                logger.info('New snapshot created: {0}'.format(args['ds']))
                sync_snapshot_cache(dispatcher, args['ds'])
            else:
                logger.info('New dataset created: {0}'.format(args['ds']))
                sync_dataset_cache(dispatcher, args['ds'])

    def on_dataset_delete(args):
        with dispatcher.get_lock('zfs-cache'):
            if '@' in args['ds']:
                logger.info('Snapshot removed: {0}'.format(args['ds']))
                sync_snapshot_cache(dispatcher, args['ds'])
            else:
                logger.info('Dataset removed: {0}'.format(args['ds']))
                sync_dataset_cache(dispatcher, args['ds'])

    def on_dataset_rename(args):
        with dispatcher.get_lock('zfs-cache'):
            if '@' in args['ds']:
                logger.info('Snapshot {0} renamed to: {1}'.format(args['ds'], args['new_ds']))
                sync_snapshot_cache(dispatcher, args['new_ds'], args['old_ds'])
            else:
                logger.info('Dataset {0} renamed to: {1}'.format(args['ds'], args['new_ds']))
                sync_dataset_cache(dispatcher, args['new_ds'])

    def on_dataset_setprop(args):
        with dispatcher.get_lock('zfs-cache'):
            if args['action'] == 'set':
                logger.info('{0} {1} property {2} set to: {3}'.format(
                    'Snapshot' if '@' in args['ds'] else 'Dataset',
                    args['ds'],
                    args['prop_name'],
                    args['prop_value']
                ))

            if args['action'] == 'inherit':
                logger.info('{0} {1} property {2} inherited from parent'.format(
                    'Snapshot' if '@' in args['ds'] else 'Dataset',
                    args['ds'],
                    args['prop_name'],
                ))

            if '@' in args['ds']:
                sync_snapshot_cache(dispatcher, args['ds'])
            else:
                sync_dataset_cache(dispatcher, args['ds'])

    def on_device_attached(args):
        for p in pools.validvalues():
            if p['status'] not in ('DEGRADED', 'UNAVAIL'):
                continue

            for vd in iterate_vdevs(p['groups']):
                if args['path'] == vd['path']:
                    logger.info('Device {0} that was part of the pool {1} got reconnected'.format(
                        args['path'],
                        p['name'])
                    )

                    # Try to clear errors
                    zpool_try_clear(p['name'], vd)

    plugin.register_schema_definition('zfs-vdev', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'path': {'type': ['string', 'null']},
            'guid': {'type': 'string'},
            'status': {
                'type': 'string',
                'readOnly': True
            },
            'stats': {
                'type': 'object',
                'readOnly': True
            },
            'type': {
                'type': 'string',
                'enum': ['disk', 'file', 'mirror', 'raidz1', 'raidz2', 'raidz3']
            },
            'children': {
                'type': 'array',
                'items': {'$ref': 'zfs-vdev'}
            }
        }
    })

    # Plugin Schema definitions

    plugin.register_schema_definition('zfs-topology', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'data': {
                'type': 'array',
                'items': {'$ref': 'zfs-vdev'},
            },
            'log': {
                'type': 'array',
                'items': {'$ref': 'zfs-vdev'},
            },
            'cache': {
                'type': 'array',
                'items': {'$ref': 'zfs-vdev'},
            },
            'spare': {
                'type': 'array',
                'items': {'$ref': 'zfs-vdev'},
            },
        }
    })

    plugin.register_schema_definition('zfs-vdev-extension', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'target_guid': {'type': 'string'},
            'vdev': {'$ref': 'zfs-vdev'}
        }
    })

    # TODO: Add ENUM to the 'state' property below
    plugin.register_schema_definition('zfs-scan', {
        'type': 'object',
        'properties': {
            'errors': {'type': 'integer'},
            'start_time': {'type': 'string'},
            'bytes_to_process': {'type': 'integer'},
            'state': {'type': 'string'},
            'end_time': {'type': 'string'},
            'func': {'type': 'integer'},
            'bytes_processed': {'type': 'integer'},
            'percentage': {'type': 'float'},
        }
    })

    # A dict containing the zfs property name: dict of their properties
    # (optional if empty the zfsprop_schema_creator defaults kickin)
    # for example 'comment': [{'source': string, 'value': 'boolean'}]
    zfsprops_dict = {
        'comment': {},
        'freeing': {},
        'listsnapshots': {},
        'leaked': {},
        'version': {},
        'free': {},
        'delegation': {},
        'dedupditto': {},
        'failmode': {},
        'autoexpand': {},
        'allocated': {},
        'guid': {},
        'altroot': {},
        'size': {},
        'fragmentation': {},
        'capacity': {},
        'name': {},
        'maxblocksize': {},
        'cachefile': {},
        'bootfs': {},
        'autoreplace': {},
        'readonly': {},
        'dedupratio': {},
        'health': {},
        'expandsize': {},
    }

    zfsproperty_schema = {'type': 'object', 'properties': {}}

    for key, value in zfsprops_dict.items():
        zfsproperty_schema['properties'][key] = zfsprop_schema_creator(**value)

    plugin.register_schema_definition('zfs-properties', zfsproperty_schema)

    # A dict containing the zfs dataset property name: dict of their properties
    # (optional if empty the zfsprop_schema_creator defaults kickin)
    # for example 'comment': [{'source': string, 'value': 'boolean'}]
    zfs_datasetprops_dict = {
        'origin': {},
        'referenced': {},
        'numclones': {},
        'primarycache': {},
        'logbias': {},
        'inconsistent': {},
        'reservation': {},
        'casesensitivity': {},
        'guid': {},
        'usedbysnapshots': {},
        'stmf_sbd_lu': {},
        'mounted': {},
        'compression': {},
        'snapdir': {},
        'copies': {},
        'aclinherit': {},
        'compressratio': {},
        'recordsize': {},
        'mlslabel': {},
        'jailed': {},
        'snapshot_count': {},
        'volsize': {},
        'clones': {},
        'atime': {},
        'usedbychildren': {},
        'volblocksize': {},
        'objsetid': {},
        'name': {},
        'defer_destroy': {},
        'type': {},
        'devices': {},
        'useraccounting': {},
        'iscsioptions': {},
        'setuid': {},
        'usedbyrefreservation': {},
        'logicalused': {},
        'userrefs': {},
        'creation': {},
        'sync': {},
        'volmode': {},
        'sharenfs': {},
        'sharesmb': {},
        'createtxg': {},
        'mountpoint': {},
        'xattr': {},
        'utf8only': {},
        'aclmode': {},
        'exec': {},
        'dedup': {},
        'snapshot_limit': {},
        'readonly': {},
        'version': {},
        'filesystem_limit': {},
        'secondarycache': {},
        'prevsnap': {},
        'available': {},
        'used': {},
        'written': {},
        'refquota': {},
        'refcompressratio': {},
        'quota': {},
        'vscan': {},
        'canmount': {},
        'normalization': {},
        'usedbydataset': {},
        'unique': {},
        'checksum': {},
        'redundant_metadata': {},
        'filesystem_count': {},
        'refreservation': {},
        'logicalreferenced': {},
        'nbmand': {},
    }

    zfs_datasetproperty_schema = {'type': 'object', 'properties': {}}

    for key, value in zfs_datasetprops_dict.items():
        zfs_datasetproperty_schema['properties'][key] = zfsprop_schema_creator(**value)

    plugin.register_schema_definition('zfs-datasetproperties', zfs_datasetproperty_schema)

    plugin.register_schema_definition('zfs-dataset', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'name': {'type': 'string'},
            'pool': {'type': 'string'},
            'type': {'$ref': 'dataset-type'},
            'properties': {'$ref': 'zfs-datasetproperties'},
            'children': {
                'type': 'array',
                'items': {'$ref': 'zfs-dataset'},
            },
        }
    })

    plugin.register_schema_definition('dataset-type', {
        'type': 'string',
        'enum': list(libzfs.DatasetType.__members__.keys())
    })

    plugin.register_schema_definition('zfs-snapshot', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'pool': {'type': 'string'},
            'dataset': {'type': 'string'},
            'name': {'type': 'string'},
            'holds': {'type': 'object'},
            'properties': {'type': 'object'}
        }
    })

    plugin.register_schema_definition('zfs-property', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'source': {
                'type': 'string',
                'enum': ['NONE', 'DEFAULT', 'LOCAL', 'INHERITED', 'RECEIVED']
            },
            'value': {'type': 'string'},
            'rawvalue': {'type': 'string'}
        }
    })

    plugin.register_schema_definition('zfs-pool', {
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'status': {
                'type': 'string',
                'enum': ['ONLINE', 'OFFLINE', 'DEGRADED', 'FAULTED',
                         'REMOVED', 'UNAVAIL']
            },
            'name': {'type': 'string'},
            'scan': {'$ref': 'zfs-scan'},
            'hostname': {'type': 'string'},
            'root_dataset': {'$ref': 'zfs-dataset'},
            'groups': {'$ref': 'zfs-topology'},
            'guid': {'type': 'integer'},
            'properties': {'$ref': 'zfs-properties'},
        }
    })

    plugin.register_event_handler('fs.zfs.pool.created', on_pool_create)
    plugin.register_event_handler('fs.zfs.pool.imported', on_pool_create)
    plugin.register_event_handler('fs.zfs.pool.destroyed', on_pool_destroy)
    plugin.register_event_handler('fs.zfs.pool.setprop', on_pool_changed)
    plugin.register_event_handler('fs.zfs.pool.reguid', on_pool_reguid)
    plugin.register_event_handler('fs.zfs.pool.config_sync', on_pool_changed)
    plugin.register_event_handler('fs.zfs.vdev.state_changed', on_pool_changed)
    plugin.register_event_handler('fs.zfs.dataset.created', on_dataset_create)
    plugin.register_event_handler('fs.zfs.dataset.deleted', on_dataset_delete)
    plugin.register_event_handler('fs.zfs.dataset.renamed', on_dataset_rename)
    plugin.register_event_handler('fs.zfs.dataset.setprop', on_dataset_setprop)
    plugin.register_event_handler('system.device.attached', on_device_attached)

    # Register Providers
    plugin.register_provider('zfs.pool', ZpoolProvider)
    plugin.register_provider('zfs.dataset', ZfsDatasetProvider)
    plugin.register_provider('zfs.snapshot', ZfsSnapshotProvider)

    # Register Event Types
    plugin.register_event_type('zfs.pool.changed')
    plugin.register_event_type('zfs.dataset.changed')
    plugin.register_event_type('zfs.snapshot.changed')

    # Register Task Handlers
    plugin.register_task_handler('zfs.pool.create', ZpoolCreateTask)
    plugin.register_task_handler('zfs.pool.configure', ZpoolConfigureTask)
    plugin.register_task_handler('zfs.pool.extend', ZpoolExtendTask)
    plugin.register_task_handler('zfs.pool.detach', ZpoolDetachTask)
    plugin.register_task_handler('zfs.pool.replace', ZpoolReplaceTask)
    plugin.register_task_handler('zfs.pool.offline_disk', ZpoolOfflineDiskTask)
    plugin.register_task_handler('zfs.pool.online_disk', ZpoolOnlineDiskTask)
    plugin.register_task_handler('zfs.pool.upgrade', ZpoolUpgradeTask)

    plugin.register_task_handler('zfs.pool.import', ZpoolImportTask)
    plugin.register_task_handler('zfs.pool.export', ZpoolExportTask)
    plugin.register_task_handler('zfs.pool.destroy', ZpoolDestroyTask)
    plugin.register_task_handler('zfs.pool.scrub', ZpoolScrubTask)

    plugin.register_task_handler('zfs.mount', ZfsDatasetMountTask)
    plugin.register_task_handler('zfs.umount', ZfsDatasetUmountTask)
    plugin.register_task_handler('zfs.create_dataset', ZfsDatasetCreateTask)
    plugin.register_task_handler('zfs.create_snapshot', ZfsSnapshotCreateTask)
    plugin.register_task_handler('zfs.delete_snapshot', ZfsSnapshotDeleteTask)
    plugin.register_task_handler('zfs.delete_multiple_snapshots', ZfsSnapshotDeleteMultipleTask)
    plugin.register_task_handler('zfs.configure', ZfsConfigureTask)
    plugin.register_task_handler('zfs.destroy', ZfsDestroyTask)
    plugin.register_task_handler('zfs.rename', ZfsRenameTask)
    plugin.register_task_handler('zfs.clone', ZfsCloneTask)

    if not os.path.isdir('/data/zfs'):
        os.mkdir('/data/zfs')

    # Do initial caches sync
    try:
        global pools
        global datasets
        global snapshots

        zfs = get_zfs()
        logger.info("Syncing ZFS pools...")
        pools = EventCacheStore(dispatcher, 'zfs.pool')
        for i in zfs.pools:
            sync_zpool_cache(dispatcher, i.name)

        logger.info("Syncing ZFS datasets...")
        datasets = EventCacheStore(dispatcher, 'zfs.dataset')
        for i in zfs.datasets:
            sync_dataset_cache(dispatcher, i.name)

        logger.info("Syncing ZFS snapshots...")
        snapshots = EventCacheStore(dispatcher, 'zfs.snapshot')
        for i in zfs.snapshots:
            sync_snapshot_cache(dispatcher, i.name)

        pools.ready = True
        datasets.ready = True
        snapshots.ready = True
    except libzfs.ZFSException as err:
        logger.error("Cannot sync ZFS caches: {0}".format(str(err)))

    try:
        zfs = get_zfs()
        # Try to reimport Pools into the system after upgrade, this checks
        # for any non-imported pools in the system via the python binding
        # analogous of `zpool import` and then tries to verify its guid with
        # the pool's in the database. In the event two pools with the same guid
        # are found (Very rare and only happens in special broken cases) it
        # logs said guid with pool name and skips that import.
        unimported_unique_pools = {}
        unimported_duplicate_pools = []

        for pool in zfs.find_import():
            if pool.guid in unimported_unique_pools:
                # This means that the pool is prolly a duplicate
                # Thus remove it from this dict of pools
                # and put it in the duplicate dict
                del unimported_unique_pools[pool.guid]
                unimported_duplicate_pools.append(pool)
            else:
                # Since there can be more than two duplicate copies
                # of a pool might exist, we still need to check for
                # it in the unimported pool list
                duplicate_guids = [x.guid for x in unimported_duplicate_pools]
                if pool.guid in duplicate_guids:
                    continue
                else:
                    unimported_unique_pools[pool.guid] = pool

        # Logging the duplicate pool names and guids, if any
        if unimported_duplicate_pools:
            dispatcher.logger.warning(
                'The following pools were unimported because of duplicates' +
                'being found: ')
            for duplicate_pool in unimported_duplicate_pools:
                dispatcher.logger.warning(
                    'Unimported pool name: {0}, guid: {1}'.format(
                        duplicate_pool.name, duplicate_pool.guid))

        # Finally, Importing the unique unimported pools that are present in
        # the database
        for vol in dispatcher.datastore.query('volumes'):
            if int(vol['id']) in unimported_unique_pools:
                pool_to_import = unimported_unique_pools[int(vol['id'])]
                # Check if the volume name is also the same
                if vol['name'] == pool_to_import.name:
                    opts = {}
                    try:
                        zfs.import_pool(pool_to_import, pool_to_import.name, opts)
                    except libzfs.ZFSException as err:
                        logger.error('Cannot import pool {0} <{1}>: {2}'.format(
                            pool_to_import.name,
                            vol['id'],
                            str(err))
                        )
                else:
                    # What to do now??
                    # When in doubt log it!
                    dispatcher.logger.error(
                        'Cannot Import pool with guid: {0}'.format(vol['id']) +
                        ' because it is named as: {0} in'.format(vol['name']) +
                        ' the database but the actual system found it named' +
                        ' as {0}'.format(pool_to_import.name))

    except libzfs.ZFSException as err:
        # Log what happened
        logger.error('ZfsPlugin init error: {0}'.format(str(err)))
