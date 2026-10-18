"""Current-schema startup, interruption, keys and concurrent persistence.

Old databases are refusal fixtures only; no migration, grant conversion or
legacy initializer is exercised or retained by these tests.
"""
from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeoutError
from contextlib import closing
import os
import sqlite3
import threading

import pytest
from cryptography.fernet import Fernet

from hub.store import Store
from tests.current_model import CurrentModel


def test_concurrent_openers_never_observe_a_partial_master_key(tmp_path,monkeypatch):
    directory=tmp_path/'hub';generating=threading.Event();release=threading.Event()
    original_generate=Fernet.generate_key
    def delayed_key():
        generating.set();assert release.wait(10);return original_generate()
    monkeypatch.setattr(Fernet,'generate_key',delayed_key)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first=workers.submit(Store,directory)
        try:
            assert generating.wait(10)
            assert not (directory/'master.key').exists()
            second=workers.submit(Store,directory)
        finally:release.set()
        stores=[first.result(timeout=10),second.result(timeout=10)]
    try:
        assert stores[1].decrypt(stores[0].encrypt('survives-concurrent-startup'))=='survives-concurrent-startup'
        assert len((directory/'master.key').read_bytes())==44
    finally:
        for store in stores:store.close()


@pytest.mark.parametrize('existing',[False,True])
def test_many_concurrent_openers_share_one_current_schema_and_key(tmp_path,existing):
    directory=tmp_path/'hub'
    if existing:
        with closing(Store(directory)) as store:store.audit('fixture','before-restart')
    start=threading.Barrier(8)
    def open_store():
        start.wait(timeout=10)
        with closing(Store(directory)) as store:
            store.audit('fixture','concurrent-open');return store.encrypt('same-key')
    with ThreadPoolExecutor(max_workers=8) as workers:
        encrypted=list(workers.map(lambda _:open_store(),range(8)))
    with closing(Store(directory)) as store:
        assert all(store.decrypt(value)=='same-key' for value in encrypted)
        assert store.one("SELECT count(*) AS n FROM audit WHERE action='concurrent-open'")['n']==8
        assert store.one("SELECT value FROM meta WHERE key='schema'")['value']=='workspace-1'
        assert 'resource' in {r['name'] for r in store.all('PRAGMA table_info(grants)')}
        assert not store.all('PRAGMA foreign_key_check')
        if existing:assert store.one("SELECT action FROM audit WHERE action='before-restart'")


@pytest.mark.parametrize('encrypted_record',['device','operation'])
def test_missing_master_key_cannot_silently_orphan_encrypted_records(tmp_path,encrypted_record):
    directory=tmp_path/'hub';store=Store(directory)
    model=CurrentModel(store);model.user('owner');space=model.workspace('key-test-space',owner='owner')
    key=(directory/'master.key').read_bytes();encrypted=store.encrypt('must-remain-recoverable')
    if encrypted_record=='device':
        store.execute("INSERT INTO devices(id,name,secret,created,owner_user_id) VALUES('dev','fixture',?,1,'owner')",(encrypted,))
    else:
        store.execute('''INSERT INTO operations
            (id,actor,tool,args_summary,fingerprint,state,created,updated,payload,space_id,owner_user_id)
            VALUES('op','owner','fs_read','{}','fingerprint','queued',1,1,?,?,'owner')''',(encrypted,space))
    store.close();(directory/'master.key').unlink()
    with pytest.raises(RuntimeError,match='master.key is missing'):Store(directory)
    assert not (directory/'master.key').exists()
    (directory/'master.key').write_bytes(key)
    with closing(Store(directory)) as recovered:
        table,column=('devices','secret') if encrypted_record=='device' else ('operations','payload')
        assert recovered.decrypt(recovered.one(f'SELECT {column} FROM {table}')[column])=='must-remain-recoverable'


def test_key_flush_failure_does_not_publish_key_and_closes_connection(tmp_path,monkeypatch):
    directory=tmp_path/'hub';original_connect=sqlite3.connect;connections=[]
    def capture_connection(*args,**kwargs):
        connection=original_connect(*args,**kwargs);connections.append(connection);return connection
    def fail_fsync(fd):raise OSError('injected key flush failure')
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,'connect',capture_connection);patch.setattr(os,'fsync',fail_fsync)
        with pytest.raises(OSError,match='injected key flush'):Store(directory)
    assert not (directory/'master.key').exists() and not list(directory.glob('.master-key-*'))
    assert len(connections)==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):connections[0].execute('SELECT 1')
    Store(directory).close()


def test_interrupted_fresh_initialization_rolls_back_without_partial_authority(tmp_path,monkeypatch):
    directory=tmp_path/'hub';original_connect=sqlite3.connect;connections=[]
    class InterruptedConnection(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if 'CREATE TABLE access_profiles' in sql:
                raise sqlite3.OperationalError('injected initialization interruption')
            return super().execute(sql,*args,**kwargs)
    def interrupted_connect(*args,**kwargs):
        connection=original_connect(*args,factory=InterruptedConnection,**kwargs)
        connections.append(connection);return connection
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,'connect',interrupted_connect)
        with pytest.raises(sqlite3.OperationalError,match='injected initialization interruption'):Store(directory)
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):connections[0].execute('SELECT 1')
    with closing(sqlite3.connect(directory/'hub.sqlite3')) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    with closing(Store(directory)) as store:
        assert store.one("SELECT value FROM meta WHERE key='schema'")['value']=='workspace-1'
        assert not store.all('PRAGMA foreign_key_check')
        assert not store.all('SELECT id FROM grants')


@pytest.mark.parametrize('marker',['1','2','7','10','999','workspace-0'])
def test_old_or_future_schema_is_refused_without_conversion_or_data_loss(tmp_path,marker):
    directory=tmp_path/'hub';directory.mkdir()
    # This is deliberately not an executable former application schema.
    # Arbitrary valuable data must not be migrated, dropped or reauthorized.
    with closing(sqlite3.connect(directory/'hub.sqlite3')) as db,db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO meta VALUES('schema',?)",(marker,))
        db.execute('CREATE TABLE preserved_data(id TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO preserved_data VALUES('fixture','keep exactly')")
        before=list(db.iterdump())
    with pytest.raises(RuntimeError,match='WORKSPACE_DATABASE_REQUIRED'):Store(directory)
    with closing(sqlite3.connect(directory/'hub.sqlite3')) as db:assert list(db.iterdump())==before


def test_close_waits_for_an_active_database_user(tmp_path):
    store=Store(tmp_path/'hub');close_started=threading.Event()
    def close_store():close_started.set();store.close()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with store.lock:
            closed=workers.submit(close_store);assert close_started.wait(10)
            with pytest.raises(FutureTimeoutError):closed.result(timeout=.1)
            assert store.db.execute('SELECT 1').fetchone()[0]==1 and not closed.done()
        closed.result(timeout=10)
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):store.db.execute('SELECT 1')
