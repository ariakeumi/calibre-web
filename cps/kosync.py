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
    """Verify the x-auth-user / x-auth-key headers against the Calibre-Web
    username and the user's personal KOSync key. Returns the User or None."""
    username = request.headers.get('x-auth-user', '')
    key = request.headers.get('x-auth-key', '')
    if not username or not key:
        return None
    user = ub.session.query(ub.User).filter(ub.User.name == username).first()
    if not user or not user.kosync_key:
        return None
    if key != kosync_md5(user.kosync_key):
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
    # accounts are the Calibre-Web users; the sync key is managed in the user profile
    return _json({"message": "Registration is disabled, sign in with your Calibre-Web username "
                             "and the KOSync key from your user profile"}, 403)


@kosync.route("/users/auth", methods=["GET"])
@csrf.exempt
def auth_user():
    payload = _request_json()
    username = str(payload.get('username') or '').strip()
    password = str(payload.get('password') or '')
    if not username or not password:
        return _json({"message": "Credentials missing", "code": 2001}, 401)
    user = ub.session.query(ub.User).filter(ub.User.name == username).first()
    # the client sends md5(key), the key itself is stored in the user profile
    if not user or not user.kosync_key or password != kosync_md5(user.kosync_key):
        return _json({"message": "Bad credentials", "code": 2001}, 401)
    return _json({"authorized": kosync_md5(user.kosync_key)})


@kosync.route("/syncs/progress", methods=["GET"])
@csrf.exempt
def get_progress():
    user = _auth_user()
    if not user:
        return _json({"message": "Bad credentials", "code": 2001}, 401)
    document = _document_hash()
    if not document:
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

    entry = (ub.session.query(ub.KosyncProgress)
             .filter(ub.KosyncProgress.user_id == user.id)
             .filter(ub.KosyncProgress.document == document)
             .first())
    if entry is None:
        entry = ub.KosyncProgress(user_id=user.id, document=document)
        ub.session.add(entry)
    elif percentage < entry.percentage - 0.001:
        # only the furthest reading position is kept, like the official sync server
        return _json({"document": entry.document,
                      "progress": entry.progress,
                      "percentage": entry.percentage}, 200)
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
        return _json({"message": "Database error", "code": 2000}, 500)
    return _json({"document": entry.document,
                  "progress": entry.progress,
                  "percentage": entry.percentage}, 200)


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
