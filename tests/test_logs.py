import asyncio
import logging
from copy import deepcopy
from logging.config import dictConfig

import boto3
import pytest
from moto import mock_s3

from exception_reports.logs import async_exception_handler, DEFAULT_LOGGING_CONFIG
from exception_reports.storages import LocalErrorStorage, S3ErrorStorage


class SpecialException(Exception):
    pass


@mock_s3
def test_s3_error_handler():
    bucket = 'my-bucket'
    prefix = 'all-exceptions/'
    region = 'us-west-1'

    s3 = boto3.client('s3', region_name=region)
    s3.create_bucket(Bucket=bucket)

    def list_keys():
        return [o['Key'] for o in s3.list_objects(Bucket=bucket, Delimiter='/', Prefix=prefix).get('Contents', [])]

    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)
    logging_config['filters']['add_exception_report']['storage_backend'] = S3ErrorStorage(
        access_key='access_key',
        secret_key='secret_key',
        bucket=bucket,
        prefix=prefix,
        region=region,
    )

    dictConfig(logging_config)

    logger = logging.getLogger(__name__)

    logger.info('this is information')
    assert list_keys() == []

    logger.error('this is a problem')
    keys = list_keys()
    assert len(keys) == 1
    assert keys[0].endswith('.html')


def test_error_handler_reports_z(tmpdir):
    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)

    logging_config['filters']['add_exception_report']['storage_backend'] = LocalErrorStorage(output_path=tmpdir)
    dictConfig(logging_config)

    logger = logging.getLogger(__name__)

    assert not tmpdir.listdir()

    logger.error('this is a problem')

    assert len(tmpdir.listdir()) == 1


def test_error_handler_reports(tmpdir):
    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)

    logging_config['filters']['add_exception_report']['storage_backend'] = LocalErrorStorage(output_path=tmpdir)
    logging_config['filters']['add_exception_report']['output_format'] = 'html'
    dictConfig(logging_config)

    logger = logging.getLogger(__name__)

    assert not tmpdir.listdir()

    logger.error('this is a problem')

    assert len(tmpdir.listdir()) == 1


def test_error_handler_json(tmpdir):
    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)

    logging_config['filters']['add_exception_report']['storage_backend'] = LocalErrorStorage(output_path=tmpdir)
    logging_config['filters']['add_exception_report']['output_format'] = 'json'
    dictConfig(logging_config)

    logger = logging.getLogger(__name__)

    assert not tmpdir.listdir()

    logger.error('this is a problem')

    assert len(tmpdir.listdir()) == 1


def test_error_handler_reports_multiple_exceptions(tmpdir):
    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)
    logging_config['filters']['add_exception_report']['storage_backend'] = LocalErrorStorage(output_path=tmpdir)
    dictConfig(logging_config)

    logger = logging.getLogger(__name__)

    def a(foo):
        try:
            b(foo)
        except Exception:
            raise SpecialException('second problem')

    def b(foo):
        c(foo)

    def c(foo):
        raise SpecialException('original problem')

    try:
        a('bar')
    except Exception:
        logger.exception("There were multiple problems")


@pytest.mark.xfail(raises=(RuntimeError,))
@pytest.mark.asyncio
async def test_async_handler(event_loop):
    """
    Demonstrate adding an exception handler that stops all work

    You shouldn't see numbers past 10 printed
    """

    logging_config = deepcopy(DEFAULT_LOGGING_CONFIG)

    dictConfig(logging_config)

    event_loop.set_exception_handler(async_exception_handler)

    todo_queue = asyncio.Queue(loop=event_loop)
    for num in range(20):
        todo_queue.put_nowait(num)

    def task_done_callback(fut: asyncio.Future):
        try:
            fut.result()
        finally:
            todo_queue.task_done()

    container = {'num': 0}

    async def process_number(n, sum_container):
        await asyncio.sleep(0.002 * n)
        container['num'] = n

        print(n)
        if n == 10:
            raise SpecialException('Something has gone terribly wrong')

        return n + 1

    while not todo_queue.empty():
        num = todo_queue.get_nowait()

        collection_task = asyncio.ensure_future(process_number(num, container), loop=event_loop)
        collection_task.add_done_callback(task_done_callback)

    await todo_queue.join()
