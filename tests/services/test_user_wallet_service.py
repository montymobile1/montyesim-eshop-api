import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.user_wallet_service import UserWalletService

@pytest.fixture
def user_wallet_service():
    with patch('app.services.user_wallet_service.UserWalletRepo') as MockWalletRepo, \
         patch('app.services.user_wallet_service.UserOrderRepo') as MockOrderRepo, \
         patch('app.services.user_wallet_service.UserWalletTransactionRepo') as MockTransRepo, \
         patch('app.services.user_wallet_service.CurrencyService') as MockCurrencyService:
        service = UserWalletService()
        service._UserWalletService__user_wallet_repo = MockWalletRepo()
        service._UserWalletService__user_order_repo = MockOrderRepo()
        service._UserWalletService__user_wallet_transaction_repo = MockTransRepo()
        service._UserWalletService__currency_service = MockCurrencyService()
        return service

def test_get_user_wallet_by_id_found(user_wallet_service):
    mock_wallet = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        result = asyncio.run(user_wallet_service.get_user_wallet_by_id('id1'))
        assert result == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_id_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    result = asyncio.run(user_wallet_service.get_user_wallet_by_id('id1'))
    assert result is None

def test_create_wallet(user_wallet_service):
    mock_wallet = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_repo.create.return_value = mock_wallet
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        dto = asyncio.run(user_wallet_service.create_wallet(MagicMock(user_id='u', amount=1, currency='USD')))
        assert dto == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_user_id_found(user_wallet_service):
    mock_wallet = MagicMock(currency='USD', amount=10)
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    user_wallet_service._UserWalletService__currency_service.get_currency_rate.return_value = 2
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        dto = asyncio.run(user_wallet_service.get_user_wallet_by_user_id('uid', 'EUR'))
        assert dto == 'dto'
        mock_mapper.assert_called_once()

def test_get_user_wallet_by_user_id_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    result = asyncio.run(user_wallet_service.get_user_wallet_by_user_id('uid', 'EUR'))
    assert result is None

def test_add_wallet_transaction_success(user_wallet_service):
    mock_wallet = MagicMock(amount=10, id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    user_wallet_service._UserWalletService__user_wallet_repo.update_by = MagicMock()
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.create = MagicMock()
    with patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto') as mock_mapper:
        with patch('app.services.user_wallet_service.ResponseHelper.success_data_response', return_value='resp') as mock_resp:
            resp = asyncio.run(user_wallet_service.add_wallet_transaction(5, 'uid', 'voucher'))
            assert resp == 'resp'
            mock_mapper.assert_called_once()
            mock_resp.assert_called_once()

def test_add_wallet_transaction_wallet_not_found(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = None
    with pytest.raises(Exception):
        asyncio.run(user_wallet_service.add_wallet_transaction(5, 'uid', 'voucher'))

def test_send_top_up_admin_email(user_wallet_service):
    wallet = MagicMock(id='wid', amount=40.0, currency='USD')
    user = MagicMock(email='john@doe.com', metadata={})
    tx1 = MagicMock(id='tx-1', wallet_id='wid', amount=10.0, source='TOP-UP-WALLET', status='success',
                    created_at='2026-07-29T08:02:11+00:00')
    tx2 = MagicMock(id='tx-2', wallet_id='wid', amount=25.0, source='TOP-UP-WALLET', status='success',
                    created_at='2026-07-29T10:15:00+00:00')
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.return_value = [tx1, tx2]
    user_wallet_service._UserWalletService__user_wallet_repo.list_in.return_value = [wallet]

    mock_template = MagicMock()
    mock_template.render.return_value = '<html/>'
    with patch('app.services.user_wallet_service.get_email_template', return_value=mock_template), \
         patch('app.services.user_wallet_service.get_config',
               return_value='sara.yaghoubi@montymobile.com,charbel.haddad@montymobile.com') as mock_config, \
         patch('app.services.user_wallet_service.send_email') as mock_send:
        user_wallet_service._UserWalletService__send_top_up_admin_email(user, wallet, tx2, 'pi_123')

    mock_send.assert_called_once()
    recipients = mock_send.call_args.kwargs['recipients']
    assert 'sara.yaghoubi@montymobile.com' in recipients
    assert 'charbel.haddad@montymobile.com' in recipients

    data = mock_template.render.call_args.kwargs['data']
    assert data['user_email'] == 'john@doe.com'
    assert data['transaction_id'] == 'pi_123'
    assert data['top_up_amount'] == '25.00'
    assert data['current_balance'] == '40.00'
    assert data['top_up_count_today'] == 2
    assert data['total_top_up_amount_today'] == '35.00'
    rows = data['today_top_ups']
    assert [r['transaction_id'] for r in rows] == ['tx-1', 'tx-2']
    assert rows[0]['balance_after'] == '15.00'
    assert rows[1]['balance_after'] == '40.00'

def test_send_top_up_admin_email_failure_is_swallowed(user_wallet_service):
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.side_effect = Exception('db down')
    with patch('app.services.user_wallet_service.send_email') as mock_send:
        user_wallet_service._UserWalletService__send_top_up_admin_email(
            MagicMock(metadata={}), MagicMock(amount=1.0, currency='USD'), MagicMock(), None)
    mock_send.assert_not_called()

def test_add_wallet_transaction_succeeds_even_if_email_dispatch_fails(user_wallet_service):
    from app.config.constants import UserWalletTransactionSource
    import os as _os

    user_wallet_service._UserWalletService__user_repo = MagicMock()
    user_wallet_service._UserWalletService__user_repo.get_first_by.return_value = MagicMock(
        metadata={'currency': 'USD'}, email='a@b.c')
    mock_wallet = MagicMock(amount=10, id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = mock_wallet
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.create.return_value = MagicMock(id='tx')

    call_count = {'n': 0}

    def flaky_thread(*args, **kwargs):
        call_count['n'] += 1
        if call_count['n'] >= 2:
            raise RuntimeError('thread creation failed')
        return MagicMock()

    with patch.dict(os.environ, {'SEND_WALLET_TOPUP_NOTIFICATION': 'true'}), \
         patch('app.services.user_wallet_service.threading.Thread', side_effect=flaky_thread), \
         patch('app.services.user_wallet_service.DtoMapper.to_user_wallet_response', return_value='dto'), \
         patch('app.services.user_wallet_service.ResponseHelper.success_data_response', return_value='resp'):
        resp = user_wallet_service.add_wallet_transaction(
            5, 'uid', UserWalletTransactionSource.TOP_UP_WALLET, order_currency='USD')
    assert resp == 'resp'
    assert call_count['n'] == 2

def test_top_up_wallet_blocked_when_daily_total_exceeded(user_wallet_service):
    from app.config.constants import ErrorMessages
    from app.exceptions import CustomException

    wallet = MagicMock(id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = wallet
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.return_value = [
        MagicMock(amount=90)]

    with pytest.raises(CustomException) as exc:
        user_wallet_service.top_up_wallet(MagicMock(amount=11), MagicMock(id='u1', email='a@b.c'),
                                          MagicMock(), 'USD')
    assert exc.value.name == ErrorMessages.TOP_UP_AMOUNT_LIMIT_EXCEEDED
    user_wallet_service._UserWalletService__user_order_repo.create.assert_not_called()

def test_top_up_wallet_allowed_when_daily_total_within_limit(user_wallet_service):
    wallet = MagicMock(id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = wallet
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.return_value = [
        MagicMock(amount=90)]
    order = MagicMock(id='o1')
    order.model_dump.return_value = {}
    user_wallet_service._UserWalletService__user_order_repo.create.return_value = order

    intent = MagicMock(id='pi_1', customer='cus_1', client_secret='sec', amount=1000, livemode=False)
    tax = MagicMock(tax_amount_exclusive=0)
    with patch('app.services.user_wallet_service.create_wallet_top_up_intent', return_value=(intent, tax)), \
         patch('app.services.user_wallet_service.create_payment_ephemeral', return_value=MagicMock(secret='eph')), \
         patch('app.services.user_wallet_service.PaymentIntentResponse', return_value=MagicMock()), \
         patch('app.services.user_wallet_service.ResponseHelper.success_data_response', return_value='resp'):
        resp = user_wallet_service.top_up_wallet(MagicMock(amount=10), MagicMock(id='u1', email='a@b.c'),
                                                 MagicMock(), 'USD')
    assert resp == 'resp'

def test_top_up_wallet_count_limit_from_env(user_wallet_service):
    from app.config.constants import ErrorMessages
    from app.exceptions import CustomException

    wallet = MagicMock(id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = wallet
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.return_value = [
        MagicMock(amount=5)]

    with patch.dict(os.environ, {'MAX_DAILY_TOP_UP_COUNT': '0'}):
        with pytest.raises(CustomException) as exc:
            user_wallet_service.top_up_wallet(MagicMock(amount=10), MagicMock(id='u1', email='a@b.c'),
                                              MagicMock(), 'USD')
    assert exc.value.name == ErrorMessages.TOP_UP_LIMIT_REACHED

def test_top_up_wallet_blocked_at_exactly_max_count(user_wallet_service):
    from app.config.constants import ErrorMessages
    from app.exceptions import CustomException

    wallet = MagicMock(id='wid', currency='USD')
    user_wallet_service._UserWalletService__user_wallet_repo.get_first_by.return_value = wallet
    user_wallet_service._UserWalletService__user_wallet_transaction_repo.list_since.return_value = [
        MagicMock(amount=5), MagicMock(amount=5)]

    with patch.dict(os.environ, {'MAX_DAILY_TOP_UP_COUNT': '2'}):
        with pytest.raises(CustomException) as exc:
            user_wallet_service.top_up_wallet(MagicMock(amount=10), MagicMock(id='u1', email='a@b.c'),
                                              MagicMock(), 'USD')
    assert exc.value.name == ErrorMessages.TOP_UP_LIMIT_REACHED
