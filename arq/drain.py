"""
:mod:`drain`
============

Drain class used by :class:`arq.worker.BaseWorker` and reusable elsewhere.
"""
import asyncio
import logging
from typing import Set  # noqa

from aioredis import RedisPool

from arq.utils import gen_random
from .jobs import ArqError

__all__ = ['Drain']

work_logger = logging.getLogger('arq.work')
jobs_logger = logging.getLogger('arq.jobs')


class TaskError(ArqError, RuntimeError):
    pass


class Drain:
    """
    Drains popping jobs from redis lists and managing a set of tasks with a limited size to execute those jobs.
    """
    def __init__(self, *,
                 redis_pool: RedisPool,
                 max_concurrent_tasks: int=50,
                 shutdown_delay: float=6,
                 timeout_seconds: int=60,
                 burst_mode: bool=True,
                 raise_task_exception: bool=False) -> None:
        """
        :param redis_pool: redis pool to get connection from to pop items from list, also used to optionally
            re-enqueue pending jobs on termination
        :param max_concurrent_tasks: maximum number of jobs which can be execute at the same time by the event loop
        :param shutdown_delay: number of seconds to wait for tasks to finish
        :param timeout_seconds: maximum duration of a job, after that the job will be cancelled by the event loop
        :param burst_mode: break the iter loop as soon as no more jobs are available by adding an sentinel quit queue
        :param raise_task_exception: whether or not to raise an exception which occurs in a processed task
        """
        self.redis_pool = redis_pool
        self.loop = redis_pool._loop
        self.max_concurrent_tasks = max_concurrent_tasks
        self.shutdown_delay = max(shutdown_delay, 0.1)
        self.timeout_seconds = timeout_seconds
        self.burst_mode = burst_mode
        self.raise_task_exception = raise_task_exception
        self.pending_tasks: Set[asyncio.futures.Future] = set()
        self.task_exception: Exception = None

        self.jobs_complete, self.jobs_failed, self.jobs_timed_out = 0, 0, 0
        self.running = False
        self._finish_lock = asyncio.Lock(loop=self.loop)

    async def __aenter__(self):
        self.running = True
        self.redis = await self.redis_pool.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.finish()
        self.redis_pool.release(self.redis)
        self.redis = None
        if self.raise_task_exception and self.task_exception:
            e = self.task_exception
            raise TaskError(f'A processed task failed: {e.__class__.__name__}, {e}') from e

    async def iter(self, *raw_queues: bytes, pop_timeout=1):
        """
        blpop jobs from redis queues and yield them. Waits for the number of tasks to drop below max_concurrent_tasks
        before popping.

        :param raw_queues: tuple of bytes defining queue(s) to pop from.
        :param pop_timeout: how long to wait on each blpop before yielding None.
        :yields: tuple ``(raw_queue_name, raw_data)`` or ``(None, None)`` if all jobs are empty
        """
        work_logger.debug('starting main blpop loop')
        quit_queue = None
        if self.burst_mode:
            quit_queue = b'arq:quit-' + gen_random()
            work_logger.debug('populating quit queue to prompt exit: %s', quit_queue.decode())
            await self.redis.rpush(quit_queue, b'1')
            raw_queues = tuple(raw_queues) + (quit_queue,)
        while self.running:
            msg = await self.redis.blpop(*raw_queues, timeout=pop_timeout)
            if msg is None:
                yield None, None
                continue
            raw_queue, raw_data = msg
            if self.burst_mode and raw_queue == quit_queue:
                work_logger.debug('got job from the quit queue, stopping')
                break
            yield raw_queue, raw_data
            await self.wait()

    def add(self, coro, job, re_enqueue=False):
        """
        Start job and add it to the pending tasks set.
        :param coro: coroutine to execute the job
        :param job: job object, instance of :class:`arq.jobs.Job` or similar
        :param re_enqueue: whether or not to re-enqueue the job on finish if the job won't finish in time.
        """
        task = self.loop.create_task(coro(job))
        task.job = job
        task.re_enqueue = re_enqueue

        task.add_done_callback(self._job_callback)
        self.loop.call_later(self.timeout_seconds, self._cancel_job, task, job)
        self.pending_tasks.add(task)

    async def wait(self):
        """
        Wait for a the number of pending tasks to drop bellow ``max_concurrent_tasks``
        """
        while True:
            pt_cnt = len(self.pending_tasks)
            if pt_cnt < self.max_concurrent_tasks:
                return
            work_logger.info('%d pending tasks, waiting for one to finish', pt_cnt)
            _, self.pending_tasks = await asyncio.wait(self.pending_tasks, loop=self.loop,
                                                       return_when=asyncio.FIRST_COMPLETED)

    async def finish(self, timeout=None):
        """
        Cancel all pending tasks and optionally re-enqueue jobs which haven't finished after the timeout.

        :param timeout: how long to wait for tasks to finish, defaults to ``shutdown_delay``
        """
        timeout = timeout or self.shutdown_delay
        self.running = False
        if self.pending_tasks:
            with await self._finish_lock:
                work_logger.info('drain waiting %0.1fs for %d tasks to finish', timeout, len(self.pending_tasks))
                _, pending = await asyncio.wait(self.pending_tasks, timeout=timeout, loop=self.loop)
                if pending:
                    pipe = self.redis.pipeline()
                    for task in pending:
                        if task.re_enqueue:
                            pipe.rpush(task.job.raw_queue, task.job.raw_data)
                        task.cancel()
                    if pipe._results:
                        await pipe.execute()
                self.pending_tasks = set()

    def _job_callback(self, task):
        self.jobs_complete += 1
        task_exception = task.exception()
        if task_exception:
            self.running = False
            self.task_exception = task_exception
        elif task.result():
            self.jobs_failed += 1
        jobs_logger.debug('task complete, %d jobs done, %d failed', self.jobs_complete, self.jobs_failed)

    def _cancel_job(self, task, job):
        if not task.cancel():
            return
        self.jobs_timed_out += 1
        jobs_logger.error('task timed out %r', job)
