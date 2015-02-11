# -*- coding: utf-8 -*-
import datetime
import logging
from requests.exceptions import ConnectionError

from django.conf import settings

from ripple_api.models import Transaction
from ripple_api.ripple_api import account_tx, RippleApiError


PROCESS_TRANSACTIONS_TIMEOUT = 270

logger = logging.getLogger('ripple')
logger.setLevel(logging.ERROR)


def _get_min_ledger_index(account):
    """
    Gets min leger index for transactions of `account` stored
    in database.
    """
    transactions = Transaction.objects.filter(
        account=account,
        status__in=[
            Transaction.RECEIVED, Transaction.PROCESSED,
            Transaction.MUST_BE_RETURN, Transaction.RETURNING,
            Transaction.RETURNED
        ]
    ).order_by('-pk')
    return transactions[0].ledger_index if transactions else -1


def _store_transaction(account, transaction):
    """
    Stores transaction for `account` into database.
    """
    tr_tx = transaction['tx']
    meta = transaction.get('meta', {})

    if meta.get('TransactionResult') != 'tesSUCCESS':
        return

    amount = meta.get('delivered_amount') or tr_tx.get('Amount', {})

    is_unprocessed = (
        tr_tx['TransactionType'] == 'Payment' and
        tr_tx['Destination'] == account and
        isinstance(amount, dict) and
        not Transaction.objects.filter(hash=tr_tx['hash'])
    )
    if is_unprocessed:
        logger.info(
            format_log_message(
                'Saving transaction: %s', transaction
            )
        )

        transaction_object = Transaction.objects.create(
            account=tr_tx['Account'],
            hash=tr_tx['hash'],
            destination=account,
            ledger_index=tr_tx['ledger_index'],
            destination_tag=tr_tx.get('DestinationTag'),
            source_tag=tr_tx.get('SourceTag'),
            status=Transaction.RECEIVED,
            currency=amount['currency'],
            issuer=amount['issuer'],
            value=amount['value']
        )

        logger.info(
            format_log_message(
                "Transaction saved: %s", transaction_object
            )
        )


def format_log_message(message, transaction=None, *args):
    """
    Message log formatter for processors.
    """
    if transaction or args:
        format_args = [transaction]
        format_args.extend(args)
        return message % tuple(format_args)
    else:
        return message


def monitor_transactions(account):
    """
    Gets new transactions for `account` and store them in DB.
    """
    start_time = datetime.datetime.now()
    logger.info(
        format_log_message(
            'Looking for new ripple transactions since last run'
        )
    )
    ledger_min_index = _get_min_ledger_index(account)
    marker = None
    has_results = True

    try:
        timeout = settings.RIPPLE_TIMEOUT
    except AttributeError:
        timeout = 5

    while has_results:
        try:
            response = account_tx(account,
                                  ledger_min_index,
                                  limit=200,
                                  marker=marker,
                                  timeout=timeout)
        except (RippleApiError, ConnectionError), e:
            logger.error(format_log_message(e))
            break

        transactions = response['transactions']
        marker = response.get('marker')
        has_results = bool(marker)

        for transaction in transactions:
            _store_transaction(account, transaction)

        transactions_timeout_reached = (
            datetime.datetime.now() - start_time >= datetime.timedelta(
                seconds=PROCESS_TRANSACTIONS_TIMEOUT
            )
        )

        if transactions_timeout_reached and has_results:
            has_results = False
            logger.error(
                'Process_transactions command terminated because '
                '(%s seconds) timeout: %s',
                PROCESS_TRANSACTIONS_TIMEOUT, unicode(marker)
            )
