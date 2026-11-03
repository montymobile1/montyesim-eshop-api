"""Files that could not be deleted from storage after a successful save, kept for a later retry."""
from typing import Optional

from app.config.db import DatabaseTables
from app.repo.admin.base import AdminBaseRepository


class StorageCleanupQueueRepo(AdminBaseRepository):
    def __init__(self):
        super().__init__(DatabaseTables.TABLE_STORAGE_CLEANUP_QUEUE)

    def enqueue(self, bucket: str, path: str, reason: Optional[str] = None) -> None:
        self._run(lambda: self.table.insert({"bucket": bucket, "path": path, "reason": reason}).execute())
