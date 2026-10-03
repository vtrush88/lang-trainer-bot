import pytest

import daily
import db as db_module


@pytest.fixture()
def conn():
    c = db_module.connect(":memory:")
    db_module.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fresh_locks():
    """user_lock живёт в модульном dict daily._locks; asyncio.Lock привязывается к петле при
    первой конкуренции — без сброса следующий тест (другая петля) падает. Autouse для всех
    модулей: хендлеры daily тоже конкурируют за лок."""
    daily._locks.clear()
    yield
    daily._locks.clear()
