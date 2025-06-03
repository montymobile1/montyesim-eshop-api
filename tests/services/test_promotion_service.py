# import pytest
# from unittest.mock import Mock, patch
# from app.services.promotion_service import PromotionService
# from app.exceptions import CustomException
# from datetime import datetime, timedelta
#
# @pytest.fixture
# def mock_promotion_repo():
#     with patch('app.services.promotion_service.PromotionRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_promotion_rule_repo():
#     with patch('app.services.promotion_service.PromotionRuleRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_promotion_usage_repo():
#     with patch('app.services.promotion_service.PromotionUsageRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_user_repo():
#     with patch('app.services.promotion_service.UserRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_esim_hub_service():
#     with patch('app.services.promotion_service.esim_hub_service_instance') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_wallet_service():
#     with patch('app.services.promotion_service.UserWalletService') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_bundle_repo():
#     with patch('app.services.promotion_service.BundleRepo') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_bundle_service():
#     with patch('app.services.promotion_service.BundleService') as mock:
#         yield mock
#
# @pytest.fixture
# def mock_fcm_service():
#     with patch('app.services.promotion_service.fcm_service') as mock:
#         yield mock
#
# @pytest.fixture
# def promotion_service(mock_promotion_repo, mock_promotion_rule_repo, mock_promotion_usage_repo,
#                      mock_user_repo, mock_esim_hub_service, mock_wallet_service,
#                      mock_bundle_repo, mock_bundle_service, mock_fcm_service):
#     return PromotionService()
#
# class TestPromotionService:
#     @pytest.mark.asyncio
#     async def test_get_active_promotions_success(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         mock_promotion_repo.return_value.select.return_value = [
#             {
#                 "id": "promo_1",
#                 "code": "SUMMER2024",
#                 "discount_percentage": 20,
#                 "start_date": (datetime.now() - timedelta(days=1)).isoformat(),
#                 "end_date": (datetime.now() + timedelta(days=30)).isoformat(),
#                 "is_active": True
#             },
#             {
#                 "id": "promo_2",
#                 "code": "WINTER2024",
#                 "discount_percentage": 15,
#                 "start_date": (datetime.now() - timedelta(days=1)).isoformat(),
#                 "end_date": (datetime.now() + timedelta(days=15)).isoformat(),
#                 "is_active": True
#             }
#         ]
#
#         # Act
#         result = await promotion_service.get_active_promotions()
#
#         # Assert
#         assert len(result) == 2
#         assert result[0]["code"] == "SUMMER2024"
#         assert result[1]["code"] == "WINTER2024"
#         mock_promotion_repo.return_value.select.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_get_active_promotions_empty(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         mock_promotion_repo.return_value.select.return_value = []
#
#         # Act
#         result = await promotion_service.get_active_promotions()
#
#         # Assert
#         assert len(result) == 0
#         mock_promotion_repo.return_value.select.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_apply_promotion_success(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_code = "SUMMER2024"
#         original_price = 100.00
#         mock_promotion_repo.return_value.get_first_by.return_value = {
#             "id": "promo_1",
#             "code": promotion_code,
#             "discount_percentage": 20,
#             "start_date": (datetime.now() - timedelta(days=1)).isoformat(),
#             "end_date": (datetime.now() + timedelta(days=30)).isoformat(),
#             "is_active": True
#         }
#
#         # Act
#         result = await promotion_service.apply_promotion(promotion_code, original_price)
#
#         # Assert
#         assert result["discounted_price"] == 80.00
#         assert result["discount_amount"] == 20.00
#         assert result["discount_percentage"] == 20
#         mock_promotion_repo.return_value.get_first_by.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_apply_promotion_not_found(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_code = "INVALID"
#         original_price = 100.00
#         mock_promotion_repo.return_value.get_first_by.return_value = None
#
#         # Act & Assert
#         with pytest.raises(CustomException) as exc:
#             await promotion_service.apply_promotion(promotion_code, original_price)
#         assert exc.value.code == 404
#         assert "Promotion not found" in str(exc.value)
#
#     @pytest.mark.asyncio
#     async def test_apply_promotion_expired(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_code = "EXPIRED"
#         original_price = 100.00
#         mock_promotion_repo.return_value.get_first_by.return_value = {
#             "id": "promo_1",
#             "code": promotion_code,
#             "discount_percentage": 20,
#             "start_date": (datetime.now() - timedelta(days=31)).isoformat(),
#             "end_date": (datetime.now() - timedelta(days=1)).isoformat(),
#             "is_active": True
#         }
#
#         # Act & Assert
#         with pytest.raises(CustomException) as exc:
#             await promotion_service.apply_promotion(promotion_code, original_price)
#         assert exc.value.code == 400
#         assert "Promotion expired" in str(exc.value)
#
#     @pytest.mark.asyncio
#     async def test_create_promotion_success(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_data = {
#             "code": "NEWPROMO",
#             "discount_percentage": 25,
#             "start_date": datetime.now().isoformat(),
#             "end_date": (datetime.now() + timedelta(days=30)).isoformat(),
#             "is_active": True
#         }
#         mock_promotion_repo.return_value.create.return_value = {
#             "id": "promo_123",
#             **promotion_data
#         }
#
#         # Act
#         result = await promotion_service.create_promotion(promotion_data)
#
#         # Assert
#         assert result["code"] == promotion_data["code"]
#         assert result["discount_percentage"] == promotion_data["discount_percentage"]
#         mock_promotion_repo.return_value.create.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_update_promotion_success(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_id = "promo_123"
#         update_data = {
#             "discount_percentage": 30,
#             "is_active": False
#         }
#         mock_promotion_repo.return_value.update_by.return_value = {
#             "id": promotion_id,
#             "code": "NEWPROMO",
#             "discount_percentage": 30,
#             "is_active": False
#         }
#
#         # Act
#         result = await promotion_service.update_promotion(promotion_id, update_data)
#
#         # Assert
#         assert result["discount_percentage"] == update_data["discount_percentage"]
#         assert result["is_active"] == update_data["is_active"]
#         mock_promotion_repo.return_value.update_by.assert_called_once()
#
#     @pytest.mark.asyncio
#     async def test_delete_promotion_success(self, promotion_service, mock_promotion_repo):
#         # Arrange
#         promotion_id = "promo_123"
#         mock_promotion_repo.return_value.delete_by.return_value = {"id": promotion_id}
#
#         # Act
#         result = await promotion_service.delete_promotion(promotion_id)
#
#         # Assert
#         assert result["message"] == "Promotion deleted successfully"
#         mock_promotion_repo.return_value.delete_by.assert_called_once()