# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web fork.
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

import hashlib
import json
from datetime import datetime, timezone

from flask import Blueprint, request, Response
from sqlalchemy.sql.expression import func

from . import csrf, db, logger, ub

log = logger.create()

kosync = Blueprint('kosync', __name__)

# KOReader identifies books by a sparse MD5 over the file: 1024-byte samples taken
# at offsets 256, 1024, 4096, 16384, ... 1GiB (offset = 1024 * 4^i, i = -1..10)
KOSYNC_HASH_OFFSETS = [1024 << (2 * i) if i >= 0 else 1024 >> (2 * -i) for i in range(-1, 11)]


def kosync_md5(value):
    return hashlib.md5(value.encode('utf-8')).hexdigest()  # nosec


def generate_kosync_key():
    """Personal sync key: 8 chars from an unambiguous lowercase alphabet
    (no 0/O/1/l/I) so it can be typed on an e-reader without pain."""
    import secrets
    alphabet = 'abcdefghjkmnpqrstuvwxyz23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(8))


def partial_md5(file_path):
    md5_hash = hashlib.md5()  # nosec
    with open(file_path, 'rb') as f:
        for offset in KOSYNC_HASH_OFFSETS:
            f.seek(offset)
            sample = f.read(1024)
            if not sample:
                break
            md5_hash.update(sample)
    return md5_hash.hexdigest()


def _json(payload, code=200):
    return Response(json.dumps(payload), status=code, mimetype='application/json')


def _request_json():
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def _auth_user():
    """Verify the request against a Calibre-Web username and the user's personal
    KOSync key. KOReader authenticates via the x-auth-user / x-auth-key headers
    (the key being md5(key)); body/query fields are accepted as a fallback.
    Returns the User or None."""
    payload = _request_json()
    username = (request.headers.get('x-auth-user') or payload.get('username')
                or request.args.get('username') or '')
    key = (request.headers.get('x-auth-key') or payload.get('password')
           or request.args.get('password') or '')
    username = str(username).strip()
    key = str(key).strip()
    if not username or not key:
        return None
    user = ub.session.query(ub.User).filter(ub.User.name == username).first()
    if not user or not user.kosync_key:
        return None
    # tolerate both the raw key and its md5, depending on what the client sends
    if key not in (user.kosync_key, kosync_md5(user.kosync_key)):
        return None
    return user


def _document_hash():
    document = _request_json().get('document') or request.args.get('document') or ''
    document = str(document).strip().lower()
    if len(document) != 32:
        return None
    return document


@kosync.route("/healthcheck")
@csrf.exempt
def healthcheck():
    return _json({"state": "OK"})


@kosync.route("/users/create", methods=["POST"])
@csrf.exempt
def create_user():
    # there are no separate sync accounts: registering with an existing
    # Calibre-Web username and the matching profile key just authorizes
    payload = _request_json()
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '').strip()
    user = ub.session.query(ub.User).filter(ub.User.name == username).first() if username else None
    if not user or not user.kosync_key or password not in (user.kosync_key, kosync_md5(user.kosync_key)):
        return _json({"message": "Sign in with your Calibre-Web username and the KOSync key "
                                 "from your user profile", "code": 2001}, 401)
    return _json({"username": username, "authorized": kosync_md5(user.kosync_key)})


@kosync.route("/users/auth", methods=["GET", "POST"])
@csrf.exempt
def auth_user():
    # KOReader sends the credentials as x-auth-user / x-auth-key headers
    user = _auth_user()
    if not user:
        return _json({"message": "Bad credentials", "code": 2001}, 401)
    return _json({"authorized": kosync_md5(user.kosync_key)})


@kosync.route("/syncs/progress", methods=["GET"])
@kosync.route("/syncs/progress/<document>", methods=["GET"])
@csrf.exempt
def get_progress(document=None):
    user = _auth_user()
    if not user:
        return _json({"message": "Bad credentials", "code": 2001}, 401)
    # Readest requests the progress by path segment, KOReader by body/query field
    document = str(document or _document_hash() or '').strip().lower()
    if len(document) != 32:
        return _json({"message": "Invalid document", "code": 2002}, 400)
    entry = (ub.session.query(ub.KosyncProgress)
             .filter(ub.KosyncProgress.user_id == user.id)
             .filter(ub.KosyncProgress.document == document)
             .first())
    if not entry:
        return _json({"message": "Requested document not found", "code": 2006}, 404)
    return _json({"document": entry.document,
                  "progress": entry.progress,
                  "percentage": entry.percentage,
                  "device": entry.device,
                  "device_id": entry.device_id,
                  "time": int(entry.timestamp.replace(tzinfo=timezone.utc).timestamp())
                  if entry.timestamp else int(datetime.now(timezone.utc).timestamp())})


def upsert_progress(user_id, document, percentage, progress, device, device_id=""):
    """Store the reading position, keeping only the furthest one per user+document."""
    entry = (ub.session.query(ub.KosyncProgress)
             .filter(ub.KosyncProgress.user_id == user_id)
             .filter(ub.KosyncProgress.document == document)
             .first())
    if entry is None:
        entry = ub.KosyncProgress(user_id=user_id, document=document)
        ub.session.add(entry)
    elif percentage < entry.percentage - 0.001:
        # only the furthest reading position is kept, like the official sync server
        return entry, False
    entry.progress = progress
    entry.percentage = percentage
    entry.device = device
    entry.device_id = device_id
    entry.timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        ub.session.commit()
    except Exception as ex:
        ub.session.rollback()
        log.error_or_exception("KOSync progress update failed: %s", ex)
        return None, False
    return entry, True


def match_book_by_metadata(metadata):
    """Best-effort match of a reported document to a library book by title/authors.
    Used when a device reports progress for a file whose bytes differ from the
    library copy (e.g. watermarked re-downloads), so progress still lands on the
    right book."""
    from . import calibre_db, db
    from sqlalchemy.sql.expression import or_
    title = str(metadata.get('title') or '').strip()
    authors = metadata.get('authors') or []
    if isinstance(authors, str):
        authors = [authors]
    authors = [str(a).strip() for a in authors if str(a).strip()]
    if not title and not authors:
        return None
    query = calibre_db.session.query(db.Books)
    candidates = query.filter(db.Books.title.ilike('%' + title + '%')).all() if title \
        else calibre_db.session.query(db.Books).all()
    if len(candidates) == 1:
        return candidates[0].id
    if authors:
        author_filter = or_(*[db.Books.authors.any(db.Authors.name.ilike('%' + a + '%'))
                              for a in authors])
        candidates = [b for b in candidates if True] and \
            calibre_db.session.query(db.Books).filter(author_filter).all() \
            if not candidates else [b for b in candidates if b.authors and
                                    any(a.name.lower() in ' '.join(x.name for x in b.authors).lower()
                                        for a in authors)]
        if len(candidates) == 1:
            return candidates[0].id
    return None


def ensure_document_mapping(document, book_id=None, metadata=None):
    """Make sure the document hash is mapped to a book. Unknown hashes are matched
    by the metadata the client reported (if any)."""
    if ub.session.query(ub.KosyncDocument).filter(ub.KosyncDocument.document == document).first():
        return True
    if book_id is None and metadata:
        try:
            book_id = match_book_by_metadata(metadata)
        except Exception as ex:
            log.error_or_exception("KOSync metadata matching failed: %s", ex)
    if book_id is None:
        return False
    ub.session.add(ub.KosyncDocument(document=document, book_id=book_id))
    try:
        ub.session.commit()
        log.info("KOSync: document %s... mapped to book %s", document[:12], book_id)
        return True
    except Exception as ex:
        ub.session.rollback()
        log.error_or_exception("KOSync document mapping failed: %s", ex)
        return False


@kosync.route("/syncs/progress", methods=["PUT"])
@csrf.exempt
def put_progress():
    user = _auth_user()
    if not user:
        return _json({"message": "Bad credentials", "code": 2001}, 401)
    payload = _request_json()
    document = str(payload.get('document') or '').strip().lower()
    if len(document) != 32:
        return _json({"message": "Invalid document", "code": 2002}, 400)
    progress = str(payload.get('progress') or '')
    try:
        percentage = float(payload.get('percentage') or 0.0)
    except (TypeError, ValueError):
        percentage = 0.0
    percentage = max(0.0, min(1.0, percentage))
    device = str(payload.get('device') or '')
    device_id = str(payload.get('device_id') or '')

    entry, _updated = upsert_progress(user.id, document, percentage, progress, device, device_id)
    if entry is None:
        return _json({"message": "Database error", "code": 2000}, 500)
    return _json({"document": entry.document,
                  "progress": entry.progress,
                  "percentage": entry.percentage}, 200)


@kosync.route("/admin/progress/<username>", methods=["GET"])
@csrf.exempt
def admin_list_progress(username):
    # diagnostics: list the stored sync progress of a user (admin only)
    from .cw_login import current_user
    if not current_user or not current_user.is_authenticated or not current_user.role_admin():
        return _json({"message": "forbidden"}, 403)
    user = ub.session.query(ub.User).filter(ub.User.name == username).first()
    if not user:
        return _json({"message": "user not found"}, 404)
    rows = (ub.session.query(ub.KosyncProgress, ub.KosyncDocument.book_id)
            .outerjoin(ub.KosyncDocument, ub.KosyncDocument.document == ub.KosyncProgress.document)
            .filter(ub.KosyncProgress.user_id == user.id)
            .all())
    result = [{"document": p.document,
               "percentage": p.percentage,
               "progress": p.progress,
               "device": p.device,
               "time": p.timestamp.strftime("%Y-%m-%d %H:%M:%S") if p.timestamp else None,
               "book_id": book_id}
              for p, book_id in rows]
    return _json({"user": username, "count": len(result), "progress": result})


def map_book_documents(book, session=None):
    """Compute the KOSync document hash for every file format of the book and
    cache the document -> book_id mapping, so progress can be shown on the
    book page and looked up by hash."""
    own_session = session is None
    if own_session:
        session = ub.session
    from . import config
    import os
    added = 0
    for data_format in book.data:
        file_path = os.path.join(config.get_book_path(), book.path,
                                 data_format.name + '.' + data_format.format.lower())
        try:
            document = partial_md5(file_path)
        except (OSError, IOError) as ex:
            log.warning("KOSync: cannot hash %s: %s", file_path, ex)
            continue
        if not session.query(ub.KosyncDocument).filter(ub.KosyncDocument.document == document).first():
            session.add(ub.KosyncDocument(document=document, book_id=book.id))
            added += 1
    if added:
        try:
            session.commit()
        except Exception as ex:
            session.rollback()
            log.error_or_exception("KOSync document mapping failed: %s", ex)
    return added


def record_reader_progress(book_id, book_format, percentage, progress, device="Calibre-Web"):
    """Store progress made in the built-in web reader under the document hash of
    the requested file format, so it is visible to KOReader/Readest and on the
    book page. The hash is computed from the file itself (cheap sparse read)."""
    from . import calibre_db, config
    from .cw_login import current_user
    import os
    percentage = max(0.0, min(1.0, percentage))
    book = calibre_db.get_book(book_id)
    if not book:
        return None
    data = next((d for d in book.data if d.format.lower() == book_format.lower()), None)
    if not data:
        return None
    file_path = os.path.join(config.get_book_path(), book.path,
                             data.name + '.' + data.format.lower())
    try:
        document = partial_md5(file_path)
    except (OSError, IOError) as ex:
        log.warning("KOSync: cannot hash %s: %s", file_path, ex)
        return None
    if not ub.session.query(ub.KosyncDocument).filter(ub.KosyncDocument.document == document).first():
        ub.session.add(ub.KosyncDocument(document=document, book_id=book.id))
        try:
            ub.session.commit()
        except Exception as ex:
            ub.session.rollback()
            log.error_or_exception("KOSync document mapping failed: %s", ex)
    entry, _updated = upsert_progress(int(current_user.id), document, percentage, progress, device)
    return entry


def get_book_progress(book_id, user_id):
    """Reading progress of the given user on any document mapped to the book.
    Books without a cached document hash are mapped on first access."""
    documents = [row.document for row in
                 ub.session.query(ub.KosyncDocument).filter(ub.KosyncDocument.book_id == book_id).all()]
    if not documents:
        try:
            from . import calibre_db
            book = calibre_db.get_book(book_id)
            if book:
                map_book_documents(book)
                documents = [row.document for row in
                             ub.session.query(ub.KosyncDocument)
                             .filter(ub.KosyncDocument.book_id == book_id).all()]
        except Exception as ex:
            log.error_or_exception("KOSync: lazy document mapping failed: %s", ex)
    if not documents:
        return None
    return (ub.session.query(ub.KosyncProgress)
            .filter(ub.KosyncProgress.user_id == user_id)
            .filter(ub.KosyncProgress.document.in_(documents))
            .order_by(ub.KosyncProgress.timestamp.desc())
            .first())
