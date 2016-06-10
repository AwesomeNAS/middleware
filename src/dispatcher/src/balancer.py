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
import gevent
import logging
import traceback
import errno
import copy
import uuid
import fnmatch
import inspect
import subprocess
import bsd
import signal
from datetime import datetime
from freenas.dispatcher import validator
from freenas.dispatcher.fd import FileDescriptor
from freenas.dispatcher.rpc import RpcException
from gevent.queue import Queue
from gevent.lock import RLock
from gevent.event import Event, AsyncResult
from gevent.subprocess import Popen
from freenas.utils import first_or_default
from resources import Resource
from task import (
    TaskException, TaskAbortException, VerifyException, ValidationException,
    TaskStatus, TaskState, TaskDescription
)
import collections


TASKWORKER_PATH = '/usr/local/libexec/taskworker'


class WorkerState(object):
    IDLE = 'IDLE'
    EXECUTING = 'EXECUTING'
    STARTING = 'STARTING'


class TaskExecutor(object):
    def __init__(self, balancer, index):
        self.balancer = balancer
        self.index = index
        self.task = None
        self.proc = None
        self.pid = None
        self.conn = None
        self.state = None
        self.key = str(uuid.uuid4())
        self.checked_in = Event()
        self.result = AsyncResult()
        self.exiting = False
        self.task_started = Event()
        self.thread = gevent.spawn(self.executor)
        self.status_lock = RLock()

    def checkin(self, conn):
        self.balancer.logger.debug('Check-in of worker #{0} (key {1})'.format(self.index, self.key))
        self.conn = conn
        self.state = WorkerState.IDLE
        self.checked_in.set()

    def get_status(self):
        self.task_started.wait()
        if not self.conn:
            return None

        with self.status_lock:
            try:
                st = TaskStatus(0)
                st.__setstate__(self.conn.call_sync('taskproxy.get_status'))
                return st
            except RpcException as err:
                self.balancer.logger.error(
                    "Cannot obtain status from task #{0}: {1}".format(self.task.id, str(err))
                )
                self.terminate()

    def put_status(self, status):
        with self.status_lock:
            # Try to collect rusage at this point, when process is still alive
            try:
                kinfo = bsd.kinfo_getproc(self.pid)
                self.task.rusage = kinfo.rusage
            except LookupError:
                pass

            if status['status'] == 'ROLLBACK':
                self.task.set_state(TaskState.ROLLBACK)

            if status['status'] == 'FINISHED':
                self.result.set(status['result'])

            if status['status'] == 'FAILED':
                error = status['error']
                cls = TaskException

                if error['type'] == 'task.TaskAbortException':
                    cls = TaskAbortException

                if error['type'] == 'ValidationException':
                    cls = ValidationException

                self.result.set_exception(cls(
                    code=error['code'],
                    message=error['message'],
                    stacktrace=error['stacktrace'],
                    extra=error.get('extra')
                ))

    def put_warning(self, warning):
        self.task.add_warning(warning)

    def run(self, task):
        self.result = AsyncResult()
        self.task = task
        self.task.set_state(TaskState.EXECUTING)
        self.task_started.set()

        filename = None
        module_name = inspect.getmodule(task.clazz).__name__
        for dir in self.balancer.dispatcher.plugin_dirs:
            found = False
            try:
                for root, _, files in os.walk(dir):
                    file = first_or_default(lambda f: module_name in f, files)
                    if file:
                        filename = os.path.join(root, file)
                        found = True
                        break

                if found:
                    break
            except FileNotFoundError:
                continue

        try:
            self.conn.call_sync('taskproxy.run', {
                'id': task.id,
                'user': task.user,
                'class': task.clazz.__name__,
                'filename': filename,
                'args': task.args,
                'debugger': task.debugger,
                'environment': task.environment
            })
        except RpcException as e:
            self.balancer.logger.warning('Cannot start task {0} on executor #{1}: {2}'.format(
                task.id,
                self.index,
                str(e)
            ))

            self.balancer.logger.warning('Killing unresponsive task executor #{0} (pid {1})'.format(
                self.index,
                self.proc.pid
            ))

            self.terminate()

        try:
            self.result.get()
        except BaseException as e:
            if not isinstance(e, TaskException):
                self.balancer.dispatcher.report_error(
                    'Task {0} raised exception other than TaskException'.format(self.task.name),
                    e
                )

            if isinstance(e, TaskAbortException):
                self.task.set_state(TaskState.ABORTED, TaskStatus(0, 'aborted'))
            else:
                self.task.error = serialize_error(e)
                self.task.set_state(TaskState.FAILED, TaskStatus(0, str(e), extra={
                    "stacktrace": traceback.format_exc()
                }))

            self.task.ended.set()
            self.balancer.task_exited(self.task)
            self.task_started.clear()
            self.state = WorkerState.IDLE
            return

        self.task.result = self.result.value
        self.task.set_state(TaskState.FINISHED, TaskStatus(100, ''))
        self.task.ended.set()
        self.balancer.task_exited(self.task)
        self.task_started.clear()
        self.state = WorkerState.IDLE

    def abort(self):
        self.balancer.logger.info("Trying to abort task #{0}".format(self.task.id))
        # Try to abort via RPC. If this fails, kill process
        try:
            self.conn.call_sync('taskproxy.abort')
        except RpcException as err:
            self.balancer.logger.warning("Failed to abort task #{0} gracefully: {1}".format(self.task.id, str(err)))
            self.balancer.logger.warning("Killing process {0}".format(self.pid))
            self.terminate()

    def terminate(self):
        try:
            self.proc.terminate()
        except ProcessLookupError:
            self.balancer.logger.warning('Executor process with PID {0} already dead'.format(self.proc.pid))

    def executor(self):
        while not self.exiting:
            try:
                self.proc = Popen(
                    [TASKWORKER_PATH, self.key],
                    close_fds=True,
                    preexec_fn=os.setpgrp,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT)

                self.pid = self.proc.pid
                self.balancer.logger.debug('Started executor #{0} as PID {1}'.format(self.index, self.pid))
            except OSError:
                self.result.set_exception(TaskException(errno.EFAULT, 'Cannot spawn task executor'))
                self.balancer.logger.error('Cannot spawn task executor #{0}'.format(self.index))
                return

            for line in self.proc.stdout:
                line = line.decode('utf8')
                self.balancer.logger.debug('Executor #{0}: {1}'.format(self.index, line.strip()))
                if self.task:
                    self.task.output += line

            self.proc.wait()
            self.checked_in.clear()

            if self.proc.returncode == -signal.SIGTERM:
                self.balancer.logger.info(
                    'Executor process with PID {0} was terminated gracefully'.format(
                        self.proc.pid
                    )
                )
            else:
                self.balancer.logger.error('Executor process with PID {0} died abruptly with exit code {1}'.format(
                    self.proc.pid,
                    self.proc.returncode)
                )

            self.result.set_exception(TaskException(errno.EFAULT, 'Task executor died'))
            gevent.sleep(1)

    def die(self):
        self.exiting = True
        if self.proc:
            self.terminate()


class Task(object):
    def __init__(self, dispatcher, name=None):
        self.dispatcher = dispatcher
        self.created_at = None
        self.started_at = None
        self.finished_at = None
        self.id = None
        self.name = name
        self.clazz = None
        self.args = None
        self.user = None
        self.session_id = None
        self.error = None
        self.state = TaskState.CREATED
        self.description = None
        self.progress = None
        self.resources = []
        self.warnings = []
        self.environment = {}
        self.thread = None
        self.instance = None
        self.parent = None
        self.subtask_ids = []
        self.result = None
        self.output = ''
        self.rusage = None
        self.ended = Event()
        self.debugger = None
        self.executor = None
        self.strict_verify = None

    def __getstate__(self):
        return {
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "user": self.user,
            "resources": self.resources,
            "description": self.get_description().__getstate__(),
            "session": self.session_id,
            "name": self.name,
            "parent": self.parent.id if self.parent else None,
            "args": remove_dots(self.args),
            "result": self.result,
            "state": self.state,
            "output": self.output,
            "rusage": self.rusage,
            "error": self.error,
            "warnings": self.warnings,
            "debugger": self.debugger,
            "environment": self.environment
        }

    def __emit_progress(self):
        self.dispatcher.dispatch_event("task.progress", {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "nolog": True,
            "percentage": self.progress.percentage,
            "message": self.progress.message,
            "extra": self.progress.extra,
            "abortable": True if (hasattr(self.instance, 'abort') and isinstance(self.instance.abort, collections.Callable)) else False
        })

    def start(self):
        try:
            self.dispatcher.balancer.assign_executor(self)
        except OverflowError:
            self.set_state(TaskState.FAILED, error='Out of executors')
            self.ended.set()
            self.dispatcher.balancer.task_exited(self)

        # Start actual task
        gevent.spawn(self.executor.run, self)

        # Start progress watcher
        gevent.spawn(self.progress_watcher)

        return self.thread

    def join(self, timeout=None):
        self.ended.wait(timeout)

    def set_state(self, state, progress=None, error=None):
        event = {'id': self.id, 'name': self.name, 'state': state}
        self.state = state

        if error:
            self.error = error

        if state == TaskState.EXECUTING:
            self.started_at = datetime.utcnow()
            event['started_at'] = self.started_at

        if state == TaskState.FINISHED:
            self.finished_at = datetime.utcnow()
            self.progress = TaskStatus(100)
            event['finished_at'] = self.finished_at
            event['result'] = self.result

        if state in (TaskState.FAILED, TaskState.ABORTED):
            self.progress = TaskStatus(0)

        self.dispatcher.dispatch_event('task.created' if state == TaskState.CREATED else 'task.updated', event)
        self.dispatcher.datastore.update('tasks', self.id, self)
        self.dispatcher.dispatch_event('task.changed', {
            'operation': 'create' if state == TaskState.CREATED else 'update',
            'ids': [self.id]
        })

        if progress and state not in (TaskState.FINISHED, TaskState.FAILED, TaskState.ABORTED):
            self.progress = progress
            self.__emit_progress()

    def set_output(self, output):
        self.output = output
        self.dispatcher.datastore.update('tasks', self.id, self)

    def add_warning(self, warning):
        self.warnings.append(warning)
        self.dispatcher.datastore.update('tasks', self.id, self)
        self.dispatcher.dispatch_event('task.changed', {
            'operation': 'update',
            'ids': [self.id]
        })

    def get_description(self):
        if not self.description:
            if not self.clazz:
                return TaskDescription(self.name)

            return TaskDescription(self.clazz.early_describe())

        if isinstance(self.description, TaskDescription):
            return self.description

        return TaskDescription(str(self.description))

    def progress_watcher(self):
        while True:
            if self.ended.wait(1):
                return
            #elif (hasattr(self.instance, 'suggested_timeout') and
            #      time.time() - self.started_at > self.instance.suggested_timeout):
            #    self.set_state(TaskState.FAILED, TaskStatus(0, "FAILED"))
            #    self.ended.set()
            #    self.error = {
            #       'type': "ETIMEDOUT",
            #       'message': "The task was killed due to a timeout",
            #    }
            #    self.progress = self.instance.get_status()
            #    self.set_state(TaskState.FAILED, TaskStatus(self.progress.percentage, "TIMEDOUT"))
            #    self.ended.set()
            #    self.dispatcher.balancer.task_exited(self)
            #    self.dispatcher.balancer.logger.debug("Task ID: %d, Name: %s was TIMEDOUT", self.id, self.name)
            else:
                progress = self.executor.get_status()
                if progress:
                    self.progress = progress
                    self.__emit_progress()


class Balancer(object):
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.task_list = []
        self.task_queue = Queue()
        self.resource_graph = dispatcher.resource_graph
        self.queues = {}
        self.threads = []
        self.executors = []
        self.logger = logging.getLogger('Balancer')
        self.dispatcher.require_collection('tasks', 'serial', type='log')
        self.create_initial_queues()
        self.start_executors()
        self.distribution_lock = RLock()
        self.debugger = None
        self.debugged_tasks = None
        self.dispatcher.register_event_type('task.changed')

        # Lets try to get `EXECUTING|WAITING|CREATED` state tasks
        # from the previous dispatcher instance and set their
        # states to 'FAILED' since they are no longer running
        # in this instance of the dispatcher
        for stale_task in dispatcher.datastore.query('tasks', ('state', 'in', ['EXECUTING', 'WAITING', 'CREATED'])):
            self.logger.info('Stale task ID: {0}, name: {1} being set to FAILED'.format(
                stale_task['id'],
                stale_task['name']
            ))

            stale_task.update({
                'state': 'FAILED',
                'error': {
                    'message': 'dispatcher process died',
                    'code': errno.EINTR,
                }
            })

            dispatcher.datastore.update('tasks', stale_task['id'], stale_task)

    def create_initial_queues(self):
        self.resource_graph.add_resource(Resource('system'))

    def start_executors(self):
        for i in range(0, self.dispatcher.configstore.get('middleware.executors_count')):
            self.logger.info('Starting task executor #{0}...'.format(i))
            self.executors.append(TaskExecutor(self, i))

    def start(self):
        self.threads.append(gevent.spawn(self.distribution_thread))
        self.logger.info("Started")

    def schema_to_list(self, schema):
        return {
            'type': 'array',
            'items': schema,
            'minItems': sum([1 for x in schema if 'mandatory' in x and x['mandatory']]),
            'maxItems': len(schema)
        }

    def verify_schema(self, clazz, args, strict=False):
        if not hasattr(clazz, 'params_schema'):
            return []

        schema = self.schema_to_list(clazz.params_schema)
        val = validator.DefaultDraft4Validator(schema, resolver=self.dispatcher.rpc.get_schema_resolver(schema))
        if strict:
            val.fail_read_only = True
        else:
            val.remove_read_only = True

        return list(val.iter_errors(args))

    def submit(self, name, args, sender, env=None):
        if name not in self.dispatcher.tasks:
            self.logger.warning("Cannot submit task: unknown task type %s", name)
            raise RpcException(errno.EINVAL, "Unknown task type {0}".format(name))

        task = Task(self.dispatcher, name)
        task.user = sender.user.name
        task.session_id = sender.session_id
        task.created_at = datetime.utcnow()
        task.clazz = self.dispatcher.tasks[name]
        task.args = copy.deepcopy(args)
        task.strict_verify = 'strict_validation' in sender.enabled_features

        if env:
            if not isinstance(env, dict):
                raise ValueError('env must be a dict')

            task.environment = copy.deepcopy(env)

        if self.debugger:
            for m in self.debugged_tasks:
                if fnmatch.fnmatch(name, m):
                    task.debugger = self.debugger

        task.id = self.dispatcher.datastore.insert("tasks", task)
        task.set_state(TaskState.CREATED)
        self.task_queue.put(task)
        self.logger.info("Task %d submitted (type: %s, class: %s)", task.id, name, task.clazz)
        return task.id

    def verify_subtask(self, parent, name, args):
        clazz = self.dispatcher.tasks[name]
        instance = clazz(self.dispatcher, self.dispatcher.datastore)
        return instance.verify(*args)

    def run_subtask(self, parent, name, args):
        args = list(args)
        task = Task(self.dispatcher, name)
        task.created_at = datetime.utcnow()
        task.clazz = self.dispatcher.tasks[name]
        task.args = args
        task.instance = task.clazz(self.dispatcher, self.dispatcher.datastore)
        task.instance.verify(*task.args)
        task.description = task.instance.describe(*task.args)
        task.id = self.dispatcher.datastore.insert("tasks", task)
        task.parent = parent

        if self.debugger:
            for m in self.debugged_tasks:
                if fnmatch.fnmatch(name, m):
                    task.debugger = self.debugger

        task.set_state(TaskState.CREATED)
        self.task_list.append(task)
        # If we actually have a non `None` parent task then, add
        # the current subtask to the parent task's subtasks list too
        if parent is not None:
            parent.subtask_ids.append(task.id)
        task.start()
        return task

    def join_subtasks(self, *tasks):
        for i in tasks:
            i.join()

    def abort(self, id, error=None):
        task = self.get_task(id)
        if not task:
            self.logger.warning("Cannot abort task: unknown task id %d", id)
            return

        success = False
        if task.started_at is None:
            success = True
        else:
            try:
                task.executor.abort()
                # Also try to abort any subtasks that might have been running
                for st in task.subtask_ids:
                    self.abort(st)
            except:
                pass
        if success:
            task.ended.set()
            if error:
                task.set_state(TaskState.FAILED, TaskStatus(0), serialize_error(error))
                self.logger.debug("Task ID: %d, name: %s aborted with error", task.id, task.name)
            else:
                task.set_state(TaskState.ABORTED, TaskStatus(0, "Aborted"))
                self.logger.debug("Task ID: %d, name: %s aborted by user", task.id, task.name)

    def task_exited(self, task):
        self.resource_graph.release(*task.resources)
        self.schedule_tasks(True)

    def schedule_tasks(self, exit=False):
        """
        This function is called when:
        1) any new task is submitted to any of the queues
        2) any task exists
        """
        started = 0
        executing_tasks = [t for t in self.task_list if t.state == TaskState.EXECUTING]
        waiting_tasks = [t for t in self.task_list if t.state == TaskState.WAITING]

        for task in waiting_tasks:
            if not self.resource_graph.can_acquire(*task.resources):
                continue

            self.resource_graph.acquire(*task.resources)
            self.threads.append(task.start())
            started += 1

        if not started and not executing_tasks and (exit or len(waiting_tasks) == 1):
            for task in waiting_tasks:
                # Check whether or not task waits on nonexistent resources. If it does,
                # abort it 'cause there's no chance anymore that missing resources will appear.
                if any(self.resource_graph.get_resource(res) is None for res in task.resources):
                    self.logger.warning('Aborting task {0}: deadlock'.format(task.id))
                    self.abort(task.id, VerifyException(errno.EBUSY, 'Resource deadlock avoided'))

    def distribution_thread(self):
        while True:
            self.task_queue.peek()
            self.distribution_lock.acquire()
            task = self.task_queue.get()

            try:
                self.logger.debug("Picked up task %d: %s with args %s", task.id, task.name, task.args)

                errors = self.verify_schema(self.dispatcher.tasks[task.name], task.args, task.strict_verify)
                if len(errors) > 0:
                    errors = list(validator.serialize_errors(errors))
                    self.logger.warning("Cannot submit task {0}: schema verification failed with errors {1}".format(
                        task.name,
                        errors
                    ))
                    raise ValidationException(extra=errors)

                task.instance = task.clazz(self.dispatcher, self.dispatcher.datastore)
                task.resources = task.instance.verify(*task.args)
                task.description = task.instance.describe(*task.args)

                if type(task.resources) is not list:
                    raise ValueError("verify() returned something else than resource list")

            except Exception as err:
                self.logger.warning("Cannot verify task %d: %s", task.id, err)
                task.set_state(TaskState.FAILED, TaskStatus(0), serialize_error(err))
                self.task_list.append(task)
                task.ended.set()
                self.distribution_lock.release()

                if not isinstance(Exception, VerifyException):
                    self.dispatcher.report_error('Task {0} verify() method raised invalid exception', err)

                continue

            task.set_state(TaskState.WAITING)
            self.task_list.append(task)
            self.distribution_lock.release()
            self.schedule_tasks()
            self.logger.debug("Task %d assigned to resources %s", task.id, ','.join(task.resources))

    def assign_executor(self, task):
        for i in self.executors:
            if i.state == WorkerState.IDLE:
                i.checked_in.wait()
                self.logger.info("Task %d assigned to executor #%d", task.id, i.index)
                task.executor = i
                i.state = WorkerState.EXECUTING
                return

        # Out of executors! Need to spawn new one
        executor = TaskExecutor(self, len(self.executors))
        self.executors.append(executor)
        executor.checked_in.wait()
        executor.state = WorkerState.EXECUTING
        task.executor = executor
        self.logger.info("Task %d assigned to executor #%d", task.id, executor.index)

    def dispose_executors(self):
        for i in self.executors:
            i.die()

    def get_active_tasks(self):
        return [x for x in self.task_list if x.state in (
            TaskState.CREATED,
            TaskState.WAITING,
            TaskState.EXECUTING
        )]

    def get_tasks(self, type=None):
        if type is None:
            return self.task_list

        return [x for x in self.task_list if x.state == type]

    def get_task(self, id):
        self.distribution_lock.acquire()
        t = first_or_default(lambda x: x.id == id, self.task_list)
        if not t:
            t = first_or_default(lambda x: x.id == id, self.task_queue.queue)

        self.distribution_lock.release()
        return t

    def get_executor_by_key(self, key):
        return first_or_default(lambda t: t.key == key, self.executors)

    def get_executor_by_sender(self, sender):
        return first_or_default(lambda t: t.conn == sender, self.executors)


def serialize_error(err):
    ret = {
        'type': type(err).__name__,
        'message': str(err),
        'stacktrace': err.stacktrace if hasattr(err, 'stacktrace') else traceback.format_exc()
    }

    if isinstance(err, RpcException):
        ret['code'] = err.code
        ret['message'] = err.message
        if err.extra:
            ret['extra'] = err.extra
    else:
        ret['code'] = errno.EFAULT

    return ret


def remove_dots(obj):
    if isinstance(obj, FileDescriptor):
        return {'fd': obj.fd}

    if isinstance(obj, dict):
        return {k.replace('.', '+'): remove_dots(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [remove_dots(x) for x in obj]

    if isinstance(obj, int) and obj > 2**32:
        return str(obj)

    return obj
