from typing import List

from sqlalchemy import text, select
from sqlalchemy.orm import selectinload

from app.exceptions import DatabaseException
from app.models.bundle import BundleModel
from app.repo.base_repo import BaseRepository
from app.schemas.home import BundleDTO


class BundleRepo(BaseRepository):

    def __init__(self):
        super().__init__(BundleModel)

    async def get_bundle_by_id(self, bundle_id: str) -> BundleDTO:
        bundle_model = await super().get_by_id(record_id=bundle_id)
        return BundleDTO.model_validate(bundle_model.data)

    async def get_bundles_by_tag(self, tag_id: int, locale: str = 'en') -> List[BundleDTO]:
        """

        """
        async with self.get_session() as session:
            try:
                stmt = text("""
                            select b.data as bundle_data, coalesce(tl.name, t.name) as tag_name
                            from bundle b
                                     inner join bundle_tag bt on b.id = bt.bundle_id
                                     inner join tag t on bt.tag_id = t.id
                                     left join tag_translation tl on tl.tag_id = t.id and tl.locale = :locale
                            where t.tag_group_id = :tag_id;
                            """)
                result = await session.execute(stmt, {"tag_id": tag_id, "locale": locale})
                rows = result.fetchall()

                # Convert rows to TagModel instances
                bundles = []
                for row in rows:
                    bundle = BundleDTO.model_validate(row.bundle_data)
                    bundles.append(bundle)
                return bundles
            except Exception as e:
                raise DatabaseException(str(e))

    async def get_bundles_with_tags(self, tag_group_id: int = None, locale: str = 'en') -> List[BundleModel]:
        """
        Get bundles with eagerly loaded tag relationships from database joins.
        This returns BundleModel instances with countries/tags loaded from the database.

        Args:
            tag_group_id: Optional tag group ID to filter by
            locale: Locale for tag translations

        Returns:
            List of BundleModel instances with loaded bundle_tags relationships
        """
        async with self.get_session() as session:
            try:
                from app.models.bundle_tag import BundleTagModel
                from app.models.tag import TagModel

                # Build query with eager loading of relationships
                stmt = select(BundleModel).options(
                    selectinload(BundleModel.bundle_tags).selectinload(BundleTagModel.tag)
                ).where(BundleModel.is_active == True)

                # Filter by tag group if specified
                if tag_group_id:
                    stmt = stmt.join(BundleTagModel).join(TagModel).where(
                        TagModel.tag_group_id == tag_group_id,
                        BundleTagModel.is_active == True
                    )

                result = await session.execute(stmt)
                bundles = result.scalars().unique().all()

                return list(bundles)
            except Exception as e:
                raise DatabaseException(str(e))

    async def get_bundles_by_tag_group(self, bundle_code: str = None, tag_group_id: int = None,
                                       tag_ids: List[str] = None, locale: str = 'en') -> List[BundleDTO]:
        """
        Get bundles as BundleDTO with countries and bundle_region populated from database joins.

        Args:
            tag_group_id: Optional tag group ID to filter by (e.g., countries group)
            tag_ids: Optional list of tag IDs to filter by (e.g. Specific Country or Region)
            locale: Locale for tag translations

        Returns:
            List of BundleDTO with countries and bundle_region populated from database
        """
        async with self.get_session() as session:
            try:
                # Build SQL dynamically based on parameters
                where_clause = ""
                params = {"locale": locale}

                if tag_group_id is not None:
                    where_clause += "AND t.tag_group_id = :tag_group_id"
                    params["tag_group_id"] = tag_group_id
                elif tag_ids is not None and len(tag_ids) > 0:
                    # Convert tag_ids to proper format for SQL IN clause
                    placeholders = ",".join([f":tag_id_{i}" for i in range(len(tag_ids))])
                    where_clause += f"AND t.id IN ({placeholders})"
                    for i, tag_id in enumerate(tag_ids):
                        params[f"tag_id_{i}"] = tag_id
                elif bundle_code is not None:
                    where_clause += "AND b.id = :bundle_code"
                    params["bundle_code"] = bundle_code
                stmt = text(f"""
                    SELECT 
                        b.id as bundle_id,
                        b.bundle_name,
                        b.data as bundle_data,
                        b.is_active,
                        b.created_at as bundle_created_at,
                        b.updated_at as bundle_updated_at,
                        t.id as tag_id,
                        t.name as tag_name,
                        t.icon as tag_icon,
                        t.data as tag_data,
                        COALESCE(tl.name, t.name) as translated_tag_name,
                        tg.id as tag_group_id,
                        tg.name as tag_group_name,
                        tg.group_category as tag_group_category
                    FROM bundle b
                    INNER JOIN bundle_tag bt ON b.id = bt.bundle_id AND bt.is_active = true
                    INNER JOIN tag t ON bt.tag_id = t.id
                    INNER JOIN tag_group tg ON t.tag_group_id = tg.id AND tg.is_active = true
                    LEFT JOIN tag_translation tl ON tl.tag_id = t.id AND tl.locale = :locale
                    WHERE b.is_active = true
                    {where_clause}
                    ORDER BY b.bundle_name, t.name
                """)

                result = await session.execute(stmt, params)
                rows = result.fetchall()

                # Group results by bundle and collect all tag IDs from JSONB data
                bundles_dict = {}
                all_tag_ids = set()  # Collect all tag IDs from all bundles' JSONB data

                for row in rows:
                    bundle_id = str(row.bundle_id)
                    if bundle_id not in bundles_dict:
                        # Store the original bundle data
                        bundles_dict[bundle_id] = {
                            "bundle_data": row.bundle_data,
                        }

                        # Extract all tag IDs from the bundle's JSONB data for translation
                        bundle_data = row.bundle_data if row.bundle_data else {}

                        # Collect tag IDs from countries
                        if "countries" in bundle_data and isinstance(bundle_data["countries"], list):
                            for country in bundle_data["countries"]:
                                if isinstance(country, dict) and "id" in country:
                                    all_tag_ids.add(str(country["id"]))

                        # Collect tag IDs from bundle_region
                        if "bundle_region" in bundle_data and isinstance(bundle_data["bundle_region"], list):
                            for region in bundle_data["bundle_region"]:
                                if isinstance(region, dict):
                                    region_id = region.get("id") or region.get("guid")
                                    if region_id:
                                        all_tag_ids.add(str(region_id))

                # Now get ALL translations for all tag IDs found in the bundles' JSONB data
                tag_translations = {}
                if all_tag_ids:
                    # Query translations for ALL tag IDs found in bundles
                    translation_stmt = text("""
                                            SELECT t.id                      as tag_id,
                                                   COALESCE(tl.name, t.name) as translated_name
                                            FROM tag t
                                                     LEFT JOIN tag_translation tl ON tl.tag_id = t.id AND tl.locale = :locale
                                            WHERE t.id = ANY (:tag_ids)
                                            """)

                    translation_result = await session.execute(translation_stmt, {
                        "locale": locale,
                        "tag_ids": list(all_tag_ids)
                    })

                    for trans_row in translation_result:
                        tag_translations[str(trans_row.tag_id)] = trans_row.translated_name

                # Convert to BundleDTO objects with translated data
                bundle_dtos = []
                for bundle_id, bundle_info in bundles_dict.items():
                    bundle_data = dict(bundle_info["bundle_data"]) if bundle_info["bundle_data"] else {}

                    # Translate countries in the JSONB data if they exist
                    if "countries" in bundle_data and isinstance(bundle_data["countries"], list):
                        for country in bundle_data["countries"]:
                            if isinstance(country, dict) and "id" in country:
                                country_id = str(country["id"])
                                if country_id in tag_translations:
                                    # Update the country name with translation
                                    country["country"] = tag_translations[country_id]
                                    # Also update 'name' field if it exists
                                    if "name" in country:
                                        country["name"] = tag_translations[country_id]

                    # Translate bundle_region in the JSONB data if they exist
                    if "bundle_region" in bundle_data and isinstance(bundle_data["bundle_region"], list):
                        for region in bundle_data["bundle_region"]:
                            if isinstance(region, dict):
                                region_id = str(region.get("id") or region.get("guid", ""))
                                if region_id and region_id in tag_translations:
                                    # Update the region name with translation
                                    region["region_name"] = tag_translations[region_id]
                                    # Also update 'name' field if it exists
                                    if "name" in region:
                                        region["name"] = tag_translations[region_id]

                    # Create BundleDTO from the translated data
                    try:
                        bundle_dto = BundleDTO.model_validate(bundle_data)
                        bundle_dtos.append(bundle_dto)
                    except Exception as validation_error:
                        # Log validation error but continue with other bundles
                        print(f"Warning: Could not validate bundle {bundle_id}: {validation_error}")
                        continue

                return bundle_dtos

            except Exception as e:
                raise DatabaseException(str(e))

    async def is_cruise_bundle(self, code: str) -> bool:
        async with self.get_session() as session:
            try:
                stmt = text("""
                            SELECT EXISTS(SELECT 1
                                          FROM bundle_tag bt
                                                   INNER JOIN tag t ON bt.tag_id = t.id
                                          WHERE bt.bundle_id = :bundle_id
                                            AND t.tag_group_id = 3) as is_cruise
                            """)
                result = await session.execute(stmt, {"bundle_id": code})
                return result.scalar()
            except Exception as e:
                raise DatabaseException(str(e))
