"""Native spool hard cutoff, concurrent initialization and unchanged-data refusal."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading

import pytest

from shared.native_cli import NATIVE_SCHEMA,database,public


@pytest.mark.parametrize('version',[0,1,2,3,4,6,999])
def test_old_or_unknown_spool_is_refused_without_migrating_or_deleting(tmp_path,version):
    path=tmp_path/'native.sqlite3'
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE saved_content(id TEXT PRIMARY KEY,body TEXT)')
        db.execute("INSERT INTO saved_content VALUES('private','keep exact')")
        db.execute(f'PRAGMA user_version={version}')
        before=list(db.iterdump())
    with pytest.raises(RuntimeError,match='NATIVE_DATABASE_REQUIRED'):database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        assert list(db.iterdump())==before
        assert db.execute('PRAGMA user_version').fetchone()[0]==version


def test_current_restart_preserves_private_ownership_and_bytes(tmp_path):
    with closing(database(tmp_path)) as db,db:
        db.execute("INSERT INTO native_security VALUES('session','fixture','user:alice','workspace-a')")
        db.execute("INSERT INTO output VALUES('fixture',0,?)",(b'private-original-output',))
    with closing(database(tmp_path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==NATIVE_SCHEMA
        assert tuple(db.execute('SELECT owner,space FROM native_security').fetchone())==('user:alice','workspace-a')
        assert db.execute('SELECT data FROM output').fetchone()[0]==b'private-original-output'
        assert not db.execute('SELECT id FROM sessions').fetchall()


def test_concurrent_initialization_publishes_only_one_complete_current_schema(tmp_path):
    barrier=threading.Barrier(6)
    def open_spool(index):
        barrier.wait(timeout=10)
        with closing(database(tmp_path)) as db,db:
            assert db.execute('PRAGMA user_version').fetchone()[0]==NATIVE_SCHEMA
            db.execute('INSERT INTO native_security VALUES(?,?,?,?)',('session',str(index),'user:fixture','test-space'))
    with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(open_spool,range(6)))
    with closing(database(tmp_path)) as db:
        assert db.execute('SELECT count(*) FROM native_security').fetchone()[0]==6
        assert {r[1] for r in db.execute('PRAGMA table_info(sessions)')} >= {'mode','native_thread','chat_settings','worker_pid','child_pid'}


def test_interrupted_initialization_closes_connection_and_rolls_back_all_tables(tmp_path,monkeypatch):
    original=sqlite3.connect;opened=[]
    class Interrupted(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if 'CREATE TABLE commands' in sql:raise sqlite3.OperationalError('injected fresh schema failure')
            return super().execute(sql,*args,**kwargs)
    def connect(*args,**kwargs):
        db=original(*args,factory=Interrupted,**kwargs);opened.append(db);return db
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,'connect',connect)
        with pytest.raises(sqlite3.OperationalError,match='fresh schema failure'):database(tmp_path)
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):opened[0].execute('SELECT 1')
    with closing(original(tmp_path/'native.sqlite3')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==0
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    with closing(database(tmp_path)) as db:assert db.execute('PRAGMA user_version').fetchone()[0]==NATIVE_SCHEMA


@pytest.mark.parametrize('missing',['mode','native_thread','chat_settings'])
def test_public_record_never_defaults_missing_old_metadata_to_current_authority(tmp_path,missing):
    with closing(database(tmp_path)) as db,db:
        db.execute("""INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated)
            VALUES('fixture','project','device','/fixture','/fixture','pi','Fixture','exited',1,1)""")
        row=dict(db.execute('SELECT * FROM sessions').fetchone())
    assert public(row)['mode']=='terminal'
    row.pop(missing)
    with pytest.raises(ValueError,match='explicit current'):public(row)
