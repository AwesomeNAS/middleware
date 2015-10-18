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

import gevent
import logging
import traceback
import errno
import copy
import uuid
import inspect
import subprocess
import bsd
from datetime import datetime
from dispatcher import validator
from dispatcher.rpc import RpcException
from gevent.queue import Queue
from gevent.lock import RLock
from gevent.event import Event, AsyncResult
from gevent.subprocess import Popen
from fnutils import first_or_default
from resources import ResourceGraph, Resource
from task import TaskException, TaskAbortException, VerifyException, TaskStatus, TaskState


TASKPROXY_PATH = '/usr/local/libexec/taskproxy'


class WorkerState(object):
    IDLE = 'IDLE'
    EXECUTING = 'EXECUTING'
    WAITING = 'WAITING'


class TaskExecutor(object):
    def __init__(self, balancer, task):
        self.balancer = balancer
        self.task = task
        self.proc = None
        self.pid = None
        self.conn = None
        self.checked_in = Event()
        self.result = AsyncResult()

    def checkin(self, conn):
        self.balancer.logger.debug('Check-in of task #{0} (key {1})'.format(self.task.id, self.task.key))
        self.conn = conn
        self.task.set_state(TaskState.EXECUTING)
        return {
            'id': self.task.id,
            'class': self.task.clazz.__name__,
            'filename': inspect.getsourcefile(self.task.clazz),
            'args': self.task.args,
            'debugger': self.task.debugger
        }

    def get_status(self):
        if not self.conn:
            return None

        try:
            st = TaskStatus(0)
            st.__setstate__(self.conn.call_client_sync('taskproxy.get_status'))
            return st
        except RpcException, err:
            self.balancer.logger.error("Cannot obtain status from task #{0}: {1}".format(self.task.id, str(err)))
            self.proc.terminate()

    def put_status(self, status):
        # Try to collect rusage at this point, when process is still alive
        try:
            kinfo = bsd.kinfo_getproc(self.pid)
            self.task.rusage = kinfo.rusage
        except LookupError:
            pass

        if status['status'] == 'FINISHED':
            self.result.set(status['result'])

        if status['status'] == 'FAILED':
            error = status['error']
            self.result.set_exception(TaskException(
                code=error['code'],
                message=error['message'],
                stacktrace=error['stacktrace'],
                extra=error.get('extra')
            ))

    def run(self):
        gevent.spawn(self.executor)
        try:
            self.result.get()
        except BaseException, e:
            if not isinstance(e, TaskException):
                self.balancer.dispatcher.report_error(
                    'Task {0} raised exception other than TaskException'.format(self.task.name),
                    e
                )

            self.task.error = serialize_error(e)
            self.task.set_state(TaskState.FAILED, TaskStatus(0, str(e), extra={
                "stacktrace": traceback.format_exc()
            }))
            self.task.ended.set()
            self.balancer.task_exited(self.task)
            return

        self.task.result = self.result.value
        self.task.set_state(TaskState.FINISHED, TaskStatus(100, ''))
        self.task.ended.set()
        self.balancer.task_exited(self.task)

    def abort(self):
        self.balancer.logger.info("Trying to abort task #{0}".format(self.task.id))
        # Try to abort via RPC. If this fails, kill process
        try:
            self.conn.call_client_sync('taskproxy.abort')
        except RpcException, err:
            self.balancer.logger.warning("Failed to abort task #{0} gracefully: {1}".format(self.task.id, str(err)))
            self.balancer.logger.warning("Killing process {0}".format(self.pid))
            self.proc.terminate()

    def executor(self):
        try:
            self.proc = Popen(
                [TASKPROXY_PATH, self.task.key],
                close_fds=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)

            self.pid = self.proc.pid
            self.balancer.logger.debug('Started task #{0} as PID {1}'.format(self.task.id, self.pid))
        except OSError:
            self.result.set_exception(TaskException(errno.EFAULT, 'Cannot spawn task executor'))
            return

        out, _ = self.proc.communicate()
        self.task.set_output(out)
        if not self.result.ready():
            self.balancer.logger.error('Executor process with PID {0} died abruptly with exit code {1}'.format(
                self.proc.pid,
                self.proc.returncode))
            self.result.set_exception(TaskException(errno.EFAULT, 'Task executor died'))


class Task(object):
    def __init__(self, dispatcher, name=None):
        self.dispatcher = dispatcher
        self.created_at = None
        self.started_at = None
        self.finished_at = None
        self.id = None
        self.key = str(uuid.uuid4())
        self.name = name
        self.clazz = None
        self.args = None
        self.user = None
        self.session_id = None
        self.error = None
        self.state = TaskState.CREATED
        self.progress = None
        self.resources = []
        self.thread = None
        self.instance = None
        self.parent = None
        self.result = None
        self.output = None
        self.rusage = None
        self.executor = TaskExecutor(self.dispatcher.balancer, self)
        self.ended = Event()
        self.debugger = None

    def __getstate__(self):
        return {
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "user": self.user,
            "resources": self.resources,
            "session": self.session_id,
            "name": self.name,
            "parent": self.parent.id if self.parent else None,
            "args": remove_dots(self.args),
            "result": self.result,
            "state": self.state,
            "output": self.output,
            "rusage": self.rusage,
            "error": self.error,
            "debugger": self.debugger
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
            "abortable": True if (hasattr(self.instance, 'abort') and callable(self.instance.abort)) else False
        })

    def run(self):
        self.set_state(TaskState.EXECUTING)
        try:
            result = self.instance.run(*(copy.deepcopy(self.args)))
        except TaskAbortException, e:
            self.error = serialize_error(e)

            self.progress = self.instance.get_status()
            self.set_state(TaskState.ABORTED, TaskStatus(self.progress.percentage, "Aborted"))
            self.ended.set()
            self.dispatcher.balancer.task_exited(self)
            self.dispatcher.balancer.logger.debug("Task ID: %d, Name: %s aborted by user", self.id, self.name)
            return
        except BaseException, e:
            self.error = serialize_error(e)

            self.set_state(TaskState.FAILED, TaskStatus(0, str(e), extra={
                "stacktrace": traceback.format_exc()
            }))
            self.ended.set()

            self.dispatcher.balancer.task_exited(self)
            return

        self.result = result
        self.set_state(TaskState.FINISHED, TaskStatus(100, ''))
        self.ended.set()
        self.dispatcher.balancer.task_exited(self)

    def start(self):
        # Start actual task
        gevent.spawn(self.executor.run)

        # Start progress watcher
        gevent.spawn(self.progress_watcher)

        return self.thread

    def join(self, timeout=None):
        self.ended.wait(timeout)

    def set_state(self, state, progress=None, error=None):
        event = {'id': self.id, 'name': self.name, 'state': state}

        if error:
            self.error = error

        if state == TaskState.EXECUTING:
            self.started_at = datetime.now()
            event['started_at'] = self.started_at

        if state == TaskState.FINISHED:
            self.finished_at = datetime.now()
            event['finished_at'] = self.finished_at
            event['result'] = self.result

        self.state = state
        self.dispatcher.dispatch_event('task.updated', event)
        self.dispatcher.datastore.update('tasks', self.id, self)

        if progress:
            self.progress = progress
            self.__emit_progress()

    def set_output(self, output):
        self.output = output
        self.dispatcher.datastore.update('tasks', self.id, self)

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
        self.logger = logging.getLogger('Balancer')
        self.dispatcher.require_collection('tasks', 'serial', type='log')
        self.create_initial_queues()
        self.distribution_lock = RLock()
        self.debugger = None

        # Lets try to get `EXECUTING|WAITING|CREATED` state tasks
        # from the previous dispatcher instance and set their
        # states to 'FAILED' since they are no longer running
        # in this instance of the dispatcher
        for stale_task in dispatcher.datastore.query('tasks', ('state', 'in', ['EXECUTING', 'WAITING', 'CREATED'])):
            self.logger.info('Stale Task ID: {0} Name: {1} being set to FAILED'.format(
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

    def verify_schema(self, clazz, args):
        if not hasattr(clazz, 'params_schema'):
            return []

        schema = self.schema_to_list(clazz.params_schema)
        val = validator.DefaultDraft4Validator(schema, resolver=self.dispatcher.rpc.get_schema_resolver(schema))
        return list(val.iter_errors(args))

    def submit(self, name, args, sender):
        if name not in self.dispatcher.tasks:
            self.logger.warning("Cannot submit task: unknown task type %s", name)
            raise RpcException(errno.EINVAL, "Unknown task type {0}".format(name))

        errors = self.verify_schema(self.dispatcher.tasks[name], args)
        if len(errors) > 0:
            errors = list(validator.serialize_errors(errors))
            self.logger.warning("Cannot submit task %s: schema verification failed", name)
            raise RpcException(errno.EINVAL, "Schema verification failed", extra=errors)

        task = Task(self.dispatcher, name)
        task.user = sender.user.name
        task.session_id = sender.session_id
        task.created_at = datetime.now()
        task.clazz = self.dispatcher.tasks[name]
        task.args = copy.deepcopy(args)
        task.state = TaskState.CREATED
        task.debugger = self.debugger
        task.id = self.dispatcher.datastore.insert("tasks", task)
        self.task_queue.put(task)
        self.dispatcher.dispatch_event('task.created', {'id': task.id, 'name': name, 'state': task.state})
        self.logger.info("Task %d submitted (type: %s, class: %s)", task.id, name, task.clazz)
        return task.id

    def verify_subtask(self, parent, name, args):
        clazz = self.dispatcher.tasks[name]
        instance = clazz(self.dispatcher, self.dispatcher.datastore)
        return instance.verify(*args)

    def run_subtask(self, parent, name, args):
        task = Task(self.dispatcher, name)
        task.created_at = datetime.now()
        task.clazz = self.dispatcher.tasks[name]
        task.args = args
        task.state = TaskState.CREATED
        task.instance = task.clazz(self.dispatcher, self.dispatcher.datastore)
        task.instance.verify(*task.args)
        task.id = self.dispatcher.datastore.insert("tasks", task)
        task.parent = parent
        self.task_list.append(task)
        task.start()
        return task

    def join_subtasks(self, *tasks):
        for i in tasks:
            i.join()

    def abort(self, id):
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
            except:
                pass
        if success:
            task.ended.set()
            task.set_state(TaskState.ABORTED, TaskStatus(0, "Aborted"))
            self.logger.debug("Task ID: %d, Name: %s aborted by user", task.id, task.name)

    def task_exited(self, task):
        self.resource_graph.release(*task.resources)
        self.schedule_tasks()

    def schedule_tasks(self):
        """
        This function is called when:
        1) any new task is submitted to any of the queues
        2) any task exists

        :return:
        """
        for task in filter(lambda t: t.state == TaskState.WAITING, self.task_list):
            if not self.resource_graph.can_acquire(*task.resources):
                continue

            self.resource_graph.acquire(*task.resources)
            self.threads.append(task.start())

    def distribution_thread(self):
        while True:
            self.task_queue.peek()
            self.distribution_lock.acquire()
            task = self.task_queue.get()

            try:
                self.logger.debug("Picked up task %d: %s with args %s", task.id, task.name, task.args)
                task.instance = task.clazz(self.dispatcher, self.dispatcher.datastore)
                task.resources = task.instance.verify(*task.args)

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

    def get_active_tasks(self):
        return filter(lambda x: x.state in (
            TaskState.CREATED,
            TaskState.WAITING,
            TaskState.EXECUTING),
            self.task_list)

    def get_tasks(self, type=None):
        if type is None:
            return self.task_list

        return filter(lambda x: x.state == type, self.task_list)

    def get_task(self, id):
        self.distribution_lock.acquire()
        t = first_or_default(lambda x: x.id == id, self.task_list)
        if not t:
            t = first_or_default(lambda x: x.id == id, self.task_queue.queue)

        self.distribution_lock.release()
        return t

    def get_task_by_key(self, key):
        return first_or_default(lambda t: t.key == key, self.task_list)

    def get_task_by_sender(self, sender):
        return first_or_default(lambda t: t.executor.conn == sender, self.task_list)


def serialize_error(err):
    ret = {
        'type': type(err).__name__,
        'message': err.message,
        'stacktrace': err.stacktrace if hasattr(err, 'stacktrace') else traceback.format_exc()
    }

    if isinstance(err, RpcException):
        ret['code'] = err.code
        if err.extra:
            ret['extra'] = err.extra
    else:
        ret['code'] = errno.EFAULT

    return ret


def remove_dots(obj):
    if isinstance(obj, dict):
        return {k.replace('.', '+'): v for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [remove_dots(x) for x in obj]

    return obj
