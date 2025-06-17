import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from app.services.scheduler_service import SchedulerService
import warnings

@pytest.fixture
def scheduler_service():
    with patch('app.services.scheduler_service.CurrencyRepo') as MockCurrencyRepo, \
         patch('app.services.scheduler_service.esim_hub_service_instance') as mock_esim_hub_instance, \
         patch('app.services.scheduler_service.BackgroundScheduler') as MockScheduler, \
         patch('app.services.scheduler_service.asyncio.get_event_loop', return_value=asyncio.new_event_loop()):
        service = SchedulerService()
        service._SchedulerService__currency_repo = MockCurrencyRepo()
        service._SchedulerService__esim_hub_service = AsyncMock()
        service.scheduler = MockScheduler()
        service.loop = asyncio.get_event_loop()
        return service

@pytest.mark.asyncio
async def test_async_scheduled_task_with_currencies(scheduler_service):
    mock_currency = MagicMock(name='USD')
    scheduler_service._SchedulerService__currency_repo.list.return_value = [mock_currency]
    mock_rate = MagicMock(currency_code='USD', new_rate=2.0)
    scheduler_service._SchedulerService__esim_hub_service.get_exchange_rates.return_value = [mock_rate]
    scheduler_service._SchedulerService__currency_repo.update_by = MagicMock()
    await scheduler_service._async_scheduled_task()
    scheduler_service._SchedulerService__currency_repo.update_by.assert_called_with(
        {'name': 'USD', 'default_currency': 'USD'}, data={'rate': 2.0}
    )

@pytest.mark.asyncio
async def test_async_scheduled_task_no_currencies(scheduler_service):
    scheduler_service._SchedulerService__currency_repo.list.return_value = []
    # Should not raise or call update_by
    scheduler_service._SchedulerService__currency_repo.update_by = MagicMock()
    await scheduler_service._async_scheduled_task()
    scheduler_service._SchedulerService__currency_repo.update_by.assert_not_called()

def test_scheduled_task_runs_coroutine(scheduler_service):
    with patch('asyncio.run_coroutine_threadsafe') as mock_run:
        future = MagicMock()
        future.result.return_value = None
        mock_run.return_value = future
        scheduler_service.scheduled_task()
        mock_run.assert_called()
        future.result.assert_called()

def test_scheduled_task_exception_logged(scheduler_service):
    future = MagicMock()
    future.result.side_effect = Exception('fail')
    with patch('asyncio.run_coroutine_threadsafe', return_value=future) as mock_run, \
         patch('app.services.scheduler_service.logger') as mock_logger, \
         patch('app.services.scheduler_service.SchedulerService._async_scheduled_task', new=lambda *a, **kw: None):
        scheduler_service.scheduled_task()
        mock_logger.error.assert_called()

def test_start_scheduler(scheduler_service):
    with patch('os.getenv', return_value='10'):
        scheduler_service.start_scheduler()
        scheduler_service.scheduler.add_job.assert_called()
        scheduler_service.scheduler.start.assert_called()

def test_shutdown_scheduler(scheduler_service):
    scheduler_service.shutdown_scheduler()
    scheduler_service.scheduler.shutdown.assert_called_with(wait=False)
