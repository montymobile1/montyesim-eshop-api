from typing import List

from sqlalchemy import text

from app.exceptions import DatabaseException
from app.models.tag import TagModel
from app.models.tag_translation import TagTranslationModel
from app.repo.base_repo import BaseRepository


class TagRepo(BaseRepository):

    def __init__(self):
        super().__init__(TagModel)

    async def get_active_tag_names_and_data_by_group(self, group_id: int, locale: str = "en") -> List[TagModel]:
        """
        Get active tags by group ID, joined with active bundles.

        Args:
            group_id: The tag group ID to filter by

        Returns:
            List of TagModel instances
        """
        async with self.get_session() as session:
            try:
                stmt = text("""
                            SELECT t.id,
                                   t.tag_group_id,
                                   coalesce(tl.name, t.name) as name,
                                   t.icon,
                                   t.data,
                                   t.created_at,
                                   t.updated_at
                            FROM tag t
                                     INNER JOIN bundle_tag bt ON t.id = bt.tag_id
                                     INNER JOIN bundle b ON bt.bundle_id = b.id
                                     LEFT JOIN tag_translation tl on tl.tag_id = t.id AND tl.locale = :locale
                            WHERE t.tag_group_id = :group_id
                              AND b.is_active = true
                            GROUP BY 1, 2, 3, 4, 5, 6, 7
                            ORDER BY t.name
                            """)

                result = await session.execute(stmt, {"group_id": group_id, "locale": locale})
                rows = result.fetchall()

                # Convert rows to TagModel instances
                tags = []
                for row in rows:
                    tag = TagModel(
                        id=row.id,
                        tag_group_id=row.tag_group_id,
                        name=row.name,
                        icon=row.icon,
                        data=row.data,
                        created_at=row.created_at,
                        updated_at=row.updated_at
                    )
                    tags.append(tag)

                return tags
            except Exception as e:
                raise DatabaseException(str(e))


class TagTranslationRepo(BaseRepository):

    def __init__(self):
        super().__init__(TagTranslationModel)
