import os
import threading
from concurrent.futures import ThreadPoolExecutor
from queue import Queue, Empty
from typing import Callable, Any
from loguru import logger


class TaskExecutor:

    def __init__(self):
        self.__queue = Queue(maxsize=int(os.getenv("SYNC_MAX_QUEUE_SIZE", "1000")))
        self.__max_workers = int(os.getenv("TASK_EXECUTOR_MAX_WORKERS", "5"))
        self.__executor = ThreadPoolExecutor(max_workers=self.__max_workers, thread_name_prefix="task-worker")
        self.__worker_started = False
        self.__worker_lock = threading.Lock()
        self.__shutdown = False

    def _start_worker(self):
        """Start the queue worker thread if not already started"""
        with self.__worker_lock:
            if not self.__worker_started:
                self.__worker_started = True
                worker_thread = threading.Thread(target=self._process_queue, daemon=True, name="task-queue-worker")
                worker_thread.start()
                logger.info(f"Started task executor with {self.__max_workers} worker threads")

    def _process_queue(self):
        """Continuously process tasks from the queue using thread pool"""
        logger.info("Task executor queue worker started")
        while not self.__shutdown:
            try:
                # Get task from queue (blocks until available or timeout)
                task = self.__queue.get(timeout=1)
                if task is None:  # Shutdown signal
                    break

                # Submit task to thread pool for execution and wait for completion
                future = self.__executor.submit(self._execute_task, task)
                logger.debug(f"Submitted task to thread pool: {task}")

                # Wait for the task to complete before processing next task
                try:
                    future.result()  # This blocks until the task is done
                    logger.debug(f"Task completed: {task} remaining queue size: {self.__queue.qsize()}")
                except Exception as e:
                    logger.error(f"Task failed: {task} - Error: {e}")

                # Mark queue task as done
                self.__queue.task_done()

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
            # Execute the task (assuming it's a callable with no arguments)
            # If your tasks need arguments, you can modify this to handle them
            if callable(task):
                task()
            else:
                logger.error(f"Task is not callable: {task}")
        except Exception as e:
            logger.error(f"Error executing task {task}: {e}")

    def add_task(self, task: Callable[..., Any]) -> bool:
        """Add a task to the execution queue"""
        # Start worker if not already running
        self._start_worker()

        if not self.__queue.full():
            self.__queue.put(task)
            logger.info(f"Task added: {task} | Queue size: {self.__queue.qsize()}")
            return True
        else:
            logger.warning("Queue is full, cannot add task.")
            return False

    def queue_size(self) -> int:
        """Get current queue size"""
        return self.__queue.qsize()

    def shutdown(self, wait: bool = True):
        """Shutdown the task executor"""
        logger.info("Shutting down task executor...")
        self.__shutdown = True

        # Add None to queue to signal worker to stop
        if not self.__queue.full():
            self.__queue.put(None)

        if wait:
            # Wait for all queued tasks to complete
            self.__queue.join()

        # Shutdown the thread pool
        self.__executor.shutdown(wait=wait)
        logger.info("Task executor shutdown complete")

    def wait_for_completion(self):
        """Wait for all queued tasks to complete"""
        self.__queue.join()
