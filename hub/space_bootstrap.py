"""Create an ordinary workspace; it is never an ambient account selector."""
import time
import uuid


def ensure_personal_space(db, user_id, label='Personal'):
    row = db.execute("SELECT id FROM spaces WHERE owner_user_id=? AND kind='personal' ORDER BY created,id LIMIT 1", (user_id,)).fetchone()
    if row:
        return row[0]
    from hub.permissions import owner_policy
    identifier = 'sp_' + uuid.uuid4().hex
    db.execute("INSERT INTO spaces(id,label,owner_user_id,kind,created) VALUES(?,?,?,'personal',?)", (identifier, label[:100], user_id, time.time()))
    db.execute('INSERT INTO memberships(space_id,user_id,policy) VALUES(?,?,?)', (identifier, user_id, owner_policy().model_dump_json()))
    db.execute('UPDATE iam_users SET personal_space_id=? WHERE user_id=?', (identifier, user_id))
    return identifier
