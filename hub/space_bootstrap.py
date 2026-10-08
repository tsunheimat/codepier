"""Personal Space bootstrap and conservative retirement of unused Legacy.

Runs inside Store's migration transaction. No resource or history is transferred.
Unrecognized Space references prevent retirement; historical audit references
keep an inactive tombstone instead of losing their original provenance.
"""
from __future__ import annotations

import time
import uuid


def ensure_personal_space(db, user_id, label='Personal'):
    row = db.execute('''SELECT s.id FROM spaces s JOIN memberships m ON m.space_id=s.id
        JOIN iam_users u ON u.user_id=m.user_id WHERE m.user_id=? AND s.kind='personal'
        AND m.level='owner' ORDER BY CASE WHEN s.id=u.personal_space_id THEN 0 ELSE 1 END,
        s.active DESC,m.active DESC,s.created,s.id LIMIT 1''', (user_id,)).fetchone()
    if row:
        identifier = row[0]
    else:
        identifier = 'sp_' + uuid.uuid4().hex
        db.execute('INSERT INTO spaces(id,label,kind,created) VALUES(?,?,?,?)',
                   (identifier, label[:100], 'personal', time.time()))
        db.execute("INSERT INTO memberships(space_id,user_id,level) VALUES(?,?,'owner')", (identifier, user_id))
    db.execute('UPDATE iam_users SET personal_space_id=? WHERE user_id=?', (identifier, user_id))
    return identifier


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def legacy_references(db):
    """Include FK references with other column names and untyped space_id fields."""
    references = {}
    for entry in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        table = entry[0]
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({_quote(table)})')}
        selected = {column for column in columns if column.lower() == 'space_id'}
        selected.update(row[3] for row in db.execute(f'PRAGMA foreign_key_list({_quote(table)})') if row[2].lower() == 'spaces')
        if selected:
            predicate = ' OR '.join(_quote(column) + '=?' for column in sorted(selected))
            count = db.execute(f'SELECT count(*) FROM {_quote(table)} WHERE {predicate}', ['legacy'] * len(selected)).fetchone()[0]
            if count:
                references[table] = count
    # Save receipts have serialized/keyed Space references rather than FKs.
    if db.execute("SELECT 1 FROM meta WHERE key LIKE 'project_save:legacy:%' LIMIT 1").fetchone():
        references['project_save_metadata'] = 1
    return references


def retire_unused_legacy(db):
    if not db.execute("SELECT 1 FROM spaces WHERE id='legacy' AND active=1").fetchone():
        return
    references = legacy_references(db)
    # Membership controls and audit provenance are retained with the tombstone.
    # All resource, policy, grant, receipt, history and unknown references block.
    if set(references) - {'memberships', 'membership_blocks', 'audit', 'iam_users'}:
        return
    # A recorded personal pointer to Legacy is invalid, not authority to retire.
    if db.execute("SELECT 1 FROM iam_users WHERE personal_space_id='legacy'").fetchone():
        return
    db.execute("UPDATE spaces SET active=0,version=version+1 WHERE id='legacy'")
    db.execute("UPDATE memberships SET active=0,version=version+1 WHERE space_id='legacy' AND active=1")
    db.execute("INSERT OR IGNORE INTO meta VALUES('legacy_space_retired','1')")


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(iam_users)')}
    if 'personal_space_id' not in columns:
        db.execute('ALTER TABLE iam_users ADD COLUMN personal_space_id TEXT REFERENCES spaces(id)')
    # Older audit columns required a manufactured Legacy default even for
    # instance-level events. Preserve every row/ID while allowing unscoped audit.
    audit_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='audit'").fetchone()[0]
    if "space_id TEXT NOT NULL DEFAULT 'legacy'" in audit_sql or "space_id TEXT DEFAULT 'legacy'" in audit_sql:
        from hub.iam_schema import _rebuild
        triggers = [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='audit'")]
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='audit'").fetchone()
        _rebuild(db, 'audit', [("space_id TEXT NOT NULL DEFAULT 'legacy'", 'space_id TEXT'),
                             ("space_id TEXT DEFAULT 'legacy'", 'space_id TEXT')])
        for trigger in triggers:
            db.execute(trigger)
        if sequence:
            updated = db.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='audit'", (sequence[0],))
            if not updated.rowcount:
                db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('audit',?)", (sequence[0],))
    db.execute('DROP TRIGGER IF EXISTS iam_local_user')
    db.execute('''CREATE TRIGGER iam_local_user AFTER INSERT ON users BEGIN
        INSERT INTO iam_users(user_id,instance_admin) VALUES(NEW.id,
            CASE WHEN NEW.password_hash='!oidc-only' THEN 0
                 WHEN (SELECT count(*) FROM iam_users)=0 THEN 1 ELSE 0 END);
        UPDATE iam_users SET personal_space_id='sp_'||lower(hex(randomblob(16))) WHERE user_id=NEW.id;
        INSERT INTO spaces(id,label,kind,created)
            SELECT personal_space_id,'Personal','personal',NEW.created FROM iam_users WHERE user_id=NEW.id;
        INSERT INTO memberships(space_id,user_id,level)
            SELECT personal_space_id,NEW.id,'owner' FROM iam_users WHERE user_id=NEW.id;
        END''')
    for row in db.execute('SELECT user_id FROM iam_users').fetchall():
        ensure_personal_space(db, row[0])
    retire_unused_legacy(db)
    db.execute("INSERT OR IGNORE INTO meta VALUES('personal_space_schema','1')")
    if db.execute("SELECT value FROM meta WHERE key='personal_space_schema'").fetchone()[0] != '1':
        raise RuntimeError('Unsupported personal Space bootstrap schema')
