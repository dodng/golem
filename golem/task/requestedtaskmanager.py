import asyncio
from datetime import timedelta
import logging
from pathlib import Path
from typing import Any, Dict, List

from dataclasses import dataclass
from golem_messages import idgenerator
from golem_task_api.client import RequestorAppClient
from peewee import fn
from twisted.internet.defer import Deferred, succeed

from golem.core.deferred import deferred_from_future
from golem.model import (
    ComputingNode,
    default_now,
    RequestedTask,
    RequestedSubtask,
)
from golem.resource.dirmanager import DirManager
from golem.task.envmanager import EnvironmentManager, EnvId
from golem.task.taskstate import TaskStatus, SubtaskStatus
from golem.task.task_api import EnvironmentTaskApiService
from golem.task.timer import ProviderComputeTimers


logger = logging.getLogger(__name__)

TaskId = str
SubtaskId = str


@dataclass
class CreateTaskParams:
    app_id: str
    name: str
    environment: str
    task_timeout: int
    subtask_timeout: int
    output_directory: Path
    resources: List[Path]
    max_subtasks: int
    max_price_per_hour: int
    concent_enabled: bool


@dataclass
class SubtaskDefinition:
    subtask_id: SubtaskId
    resources: List[str]
    params: Dict[str, Any]
    deadline: int


class RequestedTaskManager:
    def __init__(self, env_manager: EnvironmentManager, public_key, root_path):
        logger.debug('RequestedTaskManager(public_key=%r, root_path=%r)',
                     public_key, root_path)
        self._dir_manager = DirManager(root_path)
        self._env_manager = env_manager
        self._public_key: bytes = public_key
        self._app_clients: Dict[EnvId, RequestorAppClient] = {}

    def create_task(
            self,
            golem_params: CreateTaskParams,
            app_params: Dict[str, Any],
    ) -> TaskId:
        """ Creates an entry in the storage about the new task and assigns
        the task_id to it. The task then has to be initialized and started. """
        logger.debug('create_task(golem_params=%r, app_params=%r)',
                     golem_params, app_params)

        task = RequestedTask.create(
            task_id=idgenerator.generate_id(self._public_key),
            app_id=golem_params.app_id,
            name=golem_params.name,
            status=TaskStatus.creating,
            environment=golem_params.environment,
            # prerequisites='{}',
            task_timeout=golem_params.task_timeout,
            subtask_timeout=golem_params.subtask_timeout,
            start_time=default_now(),
            max_price_per_hour=golem_params.max_price_per_hour,
            max_subtasks=golem_params.max_subtasks,
            # concent_enabled = BooleanField(null=False, default=False),
            # mask = BlobField(null=False, default=masking.Mask().to_bytes()),
            output_directory=golem_params.output_directory,
            # FIXME: Where to move resources?
            resources=golem_params.resources,
            # FIXME: add app_params?
            app_params=app_params,
        )

        logger.info(
            "Creating task. id=%s, app=%r, env=%r",
            task.task_id,
            golem_params.app_id,
            golem_params.environment,
        )
        logger.debug('raw_task=%r', task)
        return task.task_id

    async def init_task(self, task_id: TaskId) -> None:
        """ Initialize the task by calling create_task on the Task API.
        The application performs validation of the params which may result in
        an error marking the task as failed. """
        logger.debug('init_task(task_id=%r)', task_id)

        task = RequestedTask.get(RequestedTask.task_id == task_id)

        if task.status != TaskStatus.creating:
            raise RuntimeError(f"Task {task_id} has already been initialized")

        # FIXME: Blender creates preview files here

        self._dir_manager.clear_temporary(task_id)
        work_dir = self._dir_manager.get_task_temporary_dir(task_id)

        # FIXME: Is RTM responsible for managing test tasks?

        app_client = await self._get_app_client(task.app_id, task.environment)
        logger.debug('init_task(task_id=%r) before creating task', task_id)
        await app_client.create_task(
            task.task_id,
            task.max_subtasks,
            task.app_params,
        )
        logger.debug('init_task(task_id=%r) after', task_id)

    def start_task(self, task_id: TaskId) -> None:
        """ Marks an already initialized task as ready for computation. """
        logger.debug('start_task(task_id=%r)', task_id)

        task = RequestedTask.get(RequestedTask.task_id == task_id)

        if not task.status.is_preparing():
            raise RuntimeError(f"Task {task_id} has already been started")

        task.status = TaskStatus.waiting
        task.save()
        # FIXME: add self.notice_task_updated(task_id, op=TaskOp.STARTED)
        logger.info("Task %s started", task_id)

    @staticmethod
    def task_exists(task_id: TaskId) -> bool:
        """ Return whether task of a given task_id exists. """
        logger.debug('task_exists(task_id=%r)', task_id)
        result = RequestedTask.select(RequestedTask.task_id) \
            .where(RequestedTask.task_id == task_id).exists()
        return result

    @staticmethod
    def is_task_finished(task_id: TaskId) -> bool:
        """ Return True if there is no more computation needed for this
        task because the task has finished, e.g. completed successfully, timed
        out, aborted, etc. """
        logger.debug('is_task_finished(task_id=%r)', task_id)
        task = RequestedTask.get(task_id)
        return task.status.is_completed()

    def get_task_network_resources_dir(self, task_id: TaskId) -> Path:
        """ Return a path to the directory of the task network resources. """
        return Path(self._dir_manager.get_task_resource_dir(task_id))

    def get_subtasks_outputs_dir(self, task_id: TaskId) -> Path:
        """ Return a path to the directory where subtasks outputs should be
        placed. """
        return Path(self._dir_manager.get_task_output_dir(task_id))

    async def has_pending_subtasks(self, task_id: TaskId) -> bool:
        """ Return True is there are pending subtasks waiting for
        computation at the given moment. If there are the next call to
        get_next_subtask will return properly defined subtask. It may happen
        that after not having any pending subtasks some will become available
        again, e.g. in case of failed verification a subtask may be marked
        as pending again. """
        logger.debug('has_pending_subtasks(task_id=%r)', task_id)
        task = RequestedTask.get(RequestedTask.task_id == task_id)
        app_client = await self._get_app_client(task.app_id, task.environment)
        return await app_client.has_pending_subtasks(task.task_id)

    async def get_next_subtask(
            self,
            task_id: TaskId,
            computing_node: ComputingNode
    ) -> SubtaskDefinition:
        """ Return a set of data required for subtask computation. """
        logger.debug(
            'get_next_subtask(task_id=%r, computing_node=%r)',
            task_id,
            computing_node
        )
        # Check is my requested task
        task = RequestedTask.get(RequestedTask.task_id == task_id)

        # Check not providing for own task
        if computing_node.node_id == self._public_key:
            raise RuntimeError(f"No subtasks for self. task_id={task_id}")

        if not task.status.is_active():
            raise RuntimeError(
                f"Task not active, no next subtask. task_id={task_id}")

        # Check should accept provider, raises when waiting on results or banned
        if self._get_unfinished_subtasks(task_id, computing_node) > 0:
            raise RuntimeError(
                "Provider has unfinished subtasks, no next subtask. "
                f"task_id={task_id}")

        if not await self.has_pending_subtasks(task_id):
            raise RuntimeError(
                f"Task not pending, no next subtask. task_id={task_id}")

        app_client = await self._get_app_client(task.app_id, task.environment)
        result = await app_client.next_subtask(task.task_id)
        subtask = RequestedSubtask.create(
            task=task,
            subtask_id=result.subtask_id,
            status=SubtaskStatus.starting,
            payload=result.params,
            inputs=result.resources,
            start_time=default_now(),
            price=task.max_price_per_hour,
            computing_node=computing_node,
        )
        deadline = subtask.start_time \
            + timedelta(milliseconds=task.subtask_timeout)

        ProviderComputeTimers.start(subtask.subtask_id)
        return SubtaskDefinition(
            subtask_id=subtask.subtask_id,
            resources=subtask.inputs,
            params=subtask.payload,
            deadline=deadline,
        )

    async def verify(self, task_id: TaskId, subtask_id: SubtaskId) -> bool:
        """ Return whether a subtask has been computed corectly. """
        logger.debug('verify(task_id=%r, subtask_id=%r)', task_id, subtask_id)
        task = RequestedTask.get(RequestedTask.task_id == task_id)
        if not task.status.is_active():
            raise RuntimeError(
                f"Task not active, can not verify. task_id={task_id}")
        subtask = RequestedSubtask.get(
            RequestedSubtask.subtask_id == subtask_id)
        # FIXME, check if subtask_id belongs to task
        assert subtask.task == task
        app_client = await self._get_app_client(task.app_id, task.environment)
        subtask.status = SubtaskStatus.verifying
        subtask.save()
        result = await app_client.verify(task.task_id, subtask_id)

        ProviderComputeTimers.finish(subtask_id)
        if result:
            subtask.status = SubtaskStatus.finished
        else:
            subtask.status = SubtaskStatus.failure
        subtask.save()

        if result:
            # Check if task completed
            finished_subtasks = RequestedSubtask.select(
                fn.Count(RequestedSubtask.subtask_id)
            ).where(
                RequestedSubtask.task == task,
                RequestedSubtask.status == SubtaskStatus.finished
            )
            if finished_subtasks >= task.max_subtasks:
                task.status = TaskStatus.finished
                task.save()
                await self._shutdown_app_client(task.app_id)

        return result

    async def abort_task(self, task_id):
        task = RequestedTask.get(RequestedTask.task_id == task_id)
        if not task.status.is_active():
            raise RuntimeError(
                f"Task not active, can not abort. task_id={task_id}")
        task.status = TaskStatus.aborted
        task.save()
        subtasks = RequestedSubtask.select().where(
            RequestedSubtask.task == task,
            # FIXME: duplicate list with SubtaskStatus.is_active()
            RequestedSubtask.status.in_([
                SubtaskStatus.starting,
                SubtaskStatus.downloading,
                SubtaskStatus.verifying,
            ])
        )
        for subtask in subtasks:
            ProviderComputeTimers.finish(subtask.subtask_id)
            subtask.status = SubtaskStatus.cancelled
            subtask.save()

        # self.notice_task_updated(task_id, op=TaskOp.ABORTED)

        await self._shutdown_app_client(task.app_id)

    def quit(self) -> Deferred:
        # FIXME: make async not Deferred?
        logger.debug('quit() clients=%r', self._app_clients)
        if not self._app_clients:
            logger.debug('No clients to clean up')
            return succeed(None)
        shutdown_futures = [
            app.shutdown() for app in self._app_clients.values()
        ]
        logger.debug('quit() futures=%r', shutdown_futures)
        # FIXME: error when running in another thread.
        # this fixes it, but is it the right way?
        try:
            asyncio.get_event_loop()
        except Exception:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        future = asyncio.ensure_future(asyncio.wait(shutdown_futures))
        deferred = deferred_from_future(future)
        logger.debug('quit() deferred=%r', deferred)
        return deferred

    async def _get_app_client(
            self,
            app_id: str,
            env_id: EnvId
    ) -> RequestorAppClient:
        if app_id not in self._app_clients:
            logger.info('Creating app_client for app_id=%r', app_id)
            service = self._get_task_api_service(app_id, env_id)
            logger.info('Got service for env=%r, service=%r', app_id, service)
            self._app_clients[app_id] = await RequestorAppClient.create(service)
            logger.info(
                'app_client created for app_id=%r, clients=%r',
                app_id, self._app_clients[app_id])
        return self._app_clients[app_id]

    def _get_task_api_service(
            self,
            app_id: str,
            env_id: EnvId
    ) -> EnvironmentTaskApiService:
        # FIXME: Stolen from golem/task/taskcomputer.py:_get_task_api_service()
        logger.info(
            'Creating task_api service for env=%r, app=%r',
            env_id,
            app_id
        )
        if not self._env_manager.enabled(env_id):
            raise RuntimeError(
                f"Error connecting to app: {env_id}. environment not enabled")
        env = self._env_manager.environment(env_id)
        payload_builder = self._env_manager.payload_builder(env_id)
        prereq = env.parse_prerequisites(
            {"image": "blenderapp", "tag": "latest"}  # FIXME: hardcoded :(
        )
        shared_dir = self._dir_manager.root_path

        return EnvironmentTaskApiService(
            env=env,
            payload_builder=payload_builder,
            prereq=prereq,
            shared_dir=shared_dir
        )

    @staticmethod
    def _get_unfinished_subtasks(
            task_id: TaskId,
            computing_node: ComputingNode
    ) -> None:
        unfinished_subtask_count = RequestedSubtask.select(
            fn.Count(RequestedSubtask.subtask_id)
        ).where(
            RequestedSubtask.computing_node == computing_node,
            RequestedSubtask.task_id == task_id,
            RequestedSubtask.status != SubtaskStatus.finished,
        ).scalar()
        logger.debug('unfinished subtasks: %r', unfinished_subtask_count)
        return unfinished_subtask_count

    async def _shutdown_app_client(self, app_id) -> None:
        # Check if app completed all tasks
        unfinished_tasks = RequestedTask.select(
            fn.Count(RequestedTask.task_id)
        ).where(
            RequestedTask.app_id == app_id,
            # FIXME: duplicate list with TaskStatus.is_active()
            RequestedTask.status.in_([
                TaskStatus.sending,
                TaskStatus.waiting,
                TaskStatus.starting,
                TaskStatus.computing,
            ])
        ).scalar()
        logger.debug('unfinished tasks: %r', unfinished_tasks)
        if unfinished_tasks == 0:
            await self._app_clients[app_id].shutdown()
            del self._app_clients[app_id]