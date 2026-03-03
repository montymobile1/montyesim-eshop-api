import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.services.scheduler_service import SchedulerService


@pytest.fixture
def scheduler_setup():
    with patch('app.services.scheduler_service.CurrencyRepo') as MockCurrencyRepo, \
         patch('app.services.scheduler_service.esim_hub_service_instance') as MockEsimHubInstance, \
         patch('app.services.scheduler_service.BackgroundScheduler') as MockScheduler:
        currency_repo_mock = MagicMock()
        MockCurrencyRepo.return_value = currency_repo_mock

        esim_hub_mock = AsyncMock()
        MockEsimHubInstance.return_value = esim_hub_mock

        scheduler_mock = MockScheduler.return_value

        service = SchedulerService()
        return service, currency_repo_mock, esim_hub_mock, scheduler_mock


@pytest.mark.asyncio
async def test_async_scheduled_task_with_currencies(scheduler_setup):
    service, currency_repo_mock, esim_hub_mock, _ = scheduler_setup

    mock_currency = MagicMock()
    mock_currency.name = 'USD'
    currency_repo_mock.list.return_value = [mock_currency]

    mock_rate = MagicMock()
    mock_rate.currency_code = 'USD'
    mock_rate.new_rate = 2.0
    esim_hub_mock.get_exchange_rates.return_value = [mock_rate]

    currency_repo_mock.update_by = MagicMock()

    await service._async_scheduled_task()

    currency_repo_mock.update_by.assert_called_with(
        {'name': 'USD', 'default_currency': 'USD'}, data={'rate': 2.0}
    )


@pytest.mark.asyncio
async def test_async_scheduled_task_no_currencies(scheduler_setup):
    service, currency_repo_mock, _, _ = scheduler_setup

    currency_repo_mock.list.return_value = []
    currency_repo_mock.update_by = MagicMock()

    await service._async_scheduled_task()

    currency_repo_mock.update_by.assert_not_called()


def test_scheduled_task_runs_coroutine(scheduler_setup):
    service, *_ = scheduler_setup

    with patch('asyncio.run') as mock_run:
        mock_run.return_value = None
        service.scheduled_task()
        mock_run.assert_called()


def test_scheduled_task_exception_logged(scheduler_setup):
    service, *_ = scheduler_setup

    with patch('asyncio.run', side_effect=Exception('fail')), \
         patch('app.services.scheduler_service.logger') as mock_logger:
        service.scheduled_task()
        mock_logger.error.assert_called()


def test_start_scheduler(scheduler_setup):
    service, _, _, scheduler_mock = scheduler_setup

    with patch('os.getenv', side_effect=lambda k, d=None: '10' if k == 'SCHEDULER_INTERVAL_SECONDS' else d):
        service.start_scheduler()
        scheduler_mock.add_job.assert_called()
        scheduler_mock.start.assert_called()
        # Validate job options
        _, kwargs = scheduler_mock.add_job.call_args
        assert kwargs.get('max_instances') == 1
        assert kwargs.get('coalesce') is True
        assert kwargs.get('misfire_grace_time') == 60


def test_shutdown_scheduler(scheduler_setup):
    service, _, _, scheduler_mock = scheduler_setup

    service.shutdown_scheduler()
    scheduler_mock.shutdown.assert_called_with(wait=False)
