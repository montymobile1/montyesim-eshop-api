import os
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from typing import Callable, Any
from loguru import logger


class TaskExecutor:
    # Class-level (static) shared resources
    _queue: Queue | None = None
    _executor: ThreadPoolExecutor | None = None
    _worker_started: bool = False
    _shutdown: bool = False

    # Locks to protect initialization and worker start
    _init_lock = threading.Lock()
    _worker_lock = threading.Lock()

    # Default sizes read from environment once (can be refreshed on re-init)
    _queue_maxsize: int = int(os.getenv("SYNC_MAX_QUEUE_SIZE", "1000"))
    _max_workers: int = int(os.getenv("TASK_EXECUTOR_MAX_WORKERS", "5"))

    def __init__(self):
        # Lazily initialize the shared queue and executor in a thread-safe way.
        with TaskExecutor._init_lock:
            # If resources haven't been created yet or were previously shutdown, (re)create them
            if TaskExecutor._queue is None or TaskExecutor._executor is None or TaskExecutor._shutdown:
                TaskExecutor._queue = Queue(maxsize=int(os.getenv("SYNC_MAX_QUEUE_SIZE", str(TaskExecutor._queue_maxsize))))
                TaskExecutor._max_workers = int(os.getenv("TASK_EXECUTOR_MAX_WORKERS", str(TaskExecutor._max_workers)))
                TaskExecutor._executor = ThreadPoolExecutor(max_workers=TaskExecutor._max_workers, thread_name_prefix="task-worker")
                TaskExecutor._worker_started = False
                TaskExecutor._shutdown = False

    def _start_worker(self):
        """Start the queue worker thread if not already started"""
        with TaskExecutor._worker_lock:
            if not TaskExecutor._worker_started:
                TaskExecutor._worker_started = True
                worker_thread = threading.Thread(target=self._process_queue, daemon=True, name="task-queue-worker")
                worker_thread.start()
                logger.info(f"Started task executor with {TaskExecutor._max_workers} worker threads")

    def _process_queue(self):
        """Continuously process tasks from the queue using thread pool"""
        logger.info("Task executor queue worker started")
        # Use local references for slightly faster access
        while not TaskExecutor._shutdown:
            try:
                task = TaskExecutor._queue.get(timeout=1)  # type: ignore[attr-defined]
                if task is None:  # Shutdown signal
                    break

                # Submit task to thread pool for execution
                future = TaskExecutor._executor.submit(self._execute_task, task)  # type: ignore[attr-defined]
                logger.debug(f"Submitted task to thread pool: {task}")

                # Wait for the task to complete before processing next task
                try:
                    future.result()  # This blocks until the task is done
                    logger.debug(f"Task completed: {task} remaining queue size: {TaskExecutor._queue.qsize()}")  # type: ignore[attr-defined]
                except Exception as e:
                    logger.error(f"Task failed: {task} - Error: {e}")

                # Mark queue task as done
                TaskExecutor._queue.task_done()  # type: ignore[attr-defined]

            except Empty:
                # Queue is empty, continue waiting for tasks
                continue
            except Exception as e:
                logger.error(f"Error in task queue worker: {str(e)}")
                # Continue processing even if there's an error

    def _execute_task(self, task: Callable[..., Any]):
        """Execute a single task in a worker thread"""
        try:
            logger.info(f"Executing task: {task}")
            if callable(task):
                task()
            else:
                logger.error(f"Task is not callable: {task}")
        except Exception as e:
            logger.error(f"Error executing task {task}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    def add_task(self, task: Callable[..., Any]) -> bool:
        """Add a task to the execution queue"""
        # Start worker if not already running
        self._start_worker()

        if not TaskExecutor._queue.full():  # type: ignore[attr-defined]
            TaskExecutor._queue.put(task)  # type: ignore[attr-defined]
            logger.info(f"Task added: {task} | Queue size: {TaskExecutor._queue.qsize()}")  # type: ignore[attr-defined]
            return True
        else:
            logger.warning("Queue is full, cannot add task.")
            return False

    def queue_size(self) -> int:
        """Get current queue size"""
        return TaskExecutor._queue.qsize()  # type: ignore[attr-defined]

    def shutdown(self, wait: bool = True):
        """Shutdown the task executor"""
        logger.info("Shutting down task executor...")
        TaskExecutor._shutdown = True

        # Add None to queue to signal worker to stop
        if TaskExecutor._queue is not None and not TaskExecutor._queue.full():
            TaskExecutor._queue.put(None)

        if wait and TaskExecutor._queue is not None:
            # Wait for all queued tasks to complete
            TaskExecutor._queue.join()

        # Shutdown the thread pool
        if TaskExecutor._executor is not None:
            TaskExecutor._executor.shutdown(wait=wait)

        # Mark resources as cleared; future TaskExecutor instances will recreate them
        TaskExecutor._executor = None
        TaskExecutor._queue = None
        TaskExecutor._worker_started = False
        logger.info("Task executor shutdown complete")

    def wait_for_completion(self):
        """Wait for all queued tasks to complete"""
        if TaskExecutor._queue is not None:
            TaskExecutor._queue.join()
