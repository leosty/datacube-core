import pytest

from datacube.drivers.s3block_index.index import S3BlockIndex
from datacube.index._api import Index
from datacube.index.postgres import PostgresDb, _core
from integration_tests.utils import alter_log_level


@pytest.fixture(params=["US/Pacific", "UTC", ])
def uninitialised_postgres_db(local_config, request):
    """
    Return a connection to an empty PostgreSQL database
    """
    timezone = request.param

    db = PostgresDb.from_config(local_config, application_name='test-run', validate_connection=False)

    # Drop tables so our tests have a clean db.
    with db.connect() as c:
        _core.drop_db(c._connection)
        c.execute('alter database %s set timezone = %r' % (local_config.db_database, str(timezone)))

    yield db
    db.close()


def test_with_standard_index(uninitialised_postgres_db):
    with alter_log_level(_core._LOG):
        index = Index(uninitialised_postgres_db)

        index.init_db()


def test_can_create_s3_index(uninitialised_postgres_db):
    with alter_log_level(_core._LOG):
        s3index = S3BlockIndex(uninitialised_postgres_db)

        assert not s3index.connected_to_s3_database()

        s3index.init_db()

        assert s3index.connected_to_s3_database()
