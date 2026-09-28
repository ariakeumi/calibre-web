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

import os
import shutil
import hashlib
from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, url_for, request
from flask_babel import gettext as _
from flask_babel import lazy_gettext as N_

from . import app, config, constants, db, isoLanguages, logger, uploader
from .admin import admin_required
from .binary_helper import resolve_binary_path, SUPPORTED_UNRAR_BINARIES
from .cw_login import current_user
from .file_helper import get_temp_dir, store_book_cover_sidecar
from .helper import add_book_to_thumbnail_cache, get_sorted_author, split_authors, uniq
from .services.worker import WorkerThread, STAT_CANCELLED, STAT_ENDED
from .string_helper import strip_whitespaces
from .tasks.scan import TaskScanImport
from .usermanagement import user_login_required

log = logger.create()

scanimport = Blueprint('scanimport', __name__)

# formats are grouped into one book when they share folder and file name;
# the first format in this order becomes the one metadata is extracted from
PREFERRED_FORMATS = ['epub', 'kepub', 'azw3', 'mobi', 'pdf']

# NAS system folders that must never be scanned for books
SKIP_DIRS = {'@eaDir', '#recycle', '@Recycle', '$RECYCLE.BIN', 'System Volume Information', 'lost+found'}


@scanimport.route("/admin/scanimport", methods=["POST"])
@user_login_required
@admin_required
def queue_scan_import():
    sub_path = request.form.get("scan_subpath", "").strip().replace('\\', '/')
    if sub_path:
        parts = [p for p in sub_path.split('/') if p not in ('', '.')]
        if not parts or '..' in parts or os.path.isabs(sub_path):
            flash(_("Invalid subfolder path"), category="error")
            return redirect(url_for('admin.admin'))
        sub_path = '/'.join(parts)
    WorkerThread.add(current_user.name, TaskScanImport(sub_path))
    flash(_("Library scan queued, check the Tasks page for progress"), category="success")
    return redirect(url_for('admin.admin'))


def collect_book_groups(root):
    """Walk root and group supported files into books by (relative folder, file name stem),
    mirroring the convention that several formats of one book share the same file name."""
    groups = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d not in SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, root).replace('\\', '/')
        if rel_dir == '.':
            rel_dir = ''
        for filename in filenames:
            if filename.startswith('.'):
                continue
            file_path = os.path.join(dirpath, filename)
            if os.path.islink(file_path):
                continue
            stem, ext = os.path.splitext(filename)
            ext = ext[1:].lower()
            if ext in constants.EXTENSIONS_UPLOAD and stem:
                groups.setdefault((rel_dir, stem), []).append((ext, file_path))
    return groups


def _pick_primary_format(files):
    def rank(entry):
        ext = entry[0]
        return PREFERRED_FORMATS.index(ext) if ext in PREFERRED_FORMATS else len(PREFERRED_FORMATS)
    return sorted(files, key=rank)[0]


def _extract_metadata(file_path, stem, ext, rar_executable):
    """Extract metadata without touching the original file: the extraction helpers write
    extracted covers next to the given path, so a symlink (or copy) in the temp dir is used."""
    tmp_dir = get_temp_dir()
    tmp_path = os.path.join(tmp_dir, 'scan_' + hashlib.md5(file_path.encode('utf-8')).hexdigest()[:16] + '.' + ext)
    if os.path.lexists(tmp_path):
        os.remove(tmp_path)
    try:
        os.symlink(file_path, tmp_path)
    except OSError:
        shutil.copyfile(file_path, tmp_path)
    try:
        return uploader.process(tmp_path, stem, '.' + ext, rar_executable)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _get_or_add(session, db_object, db_filter, value, new_element):
    # the NOCASE column collation gives ASCII-case-insensitive matching without the
    # unidecode transliteration of the custom lower() function, which never matches CJK
    element = session.query(db_object).filter(db_filter == value).first()
    if not element:
        element = new_element
        session.add(element)
        session.flush()
    return element


def _resolve_languages(session, languages_string):
    languages = []
    if languages_string:
        unknown = []
        try:
            codes = isoLanguages.get_valid_language_codes_from_code('en', languages_string.split(','), unknown)
        except Exception:
            codes = []
        for code in uniq([c for c in codes if c]):
            languages.append(session.query(db.Languages).filter(db.Languages.lang_code == code).first()
                             or _add_language(session, code))
    return languages


def _add_language(session, code):
    language = db.Languages(code)
    session.add(language)
    session.flush()
    return language


def run_scan(task, calibre_dbb):
    book_root = config.get_book_path()
    sub_path = task.sub_path
    root = os.path.normpath(os.path.join(book_root, sub_path)) if sub_path else book_root
    if os.path.abspath(root) != os.path.abspath(book_root) \
            and not os.path.abspath(root).startswith(os.path.abspath(book_root) + os.sep):
        raise Exception("Scan path outside library: {}".format(sub_path))
    if not os.path.isdir(root):
        raise Exception("Scan path not found: {}".format(root))

    calibre_dbb.create_functions(config)
    rar_executable = resolve_binary_path(config.config_rarfile_location, SUPPORTED_UNRAR_BINARIES)

    groups = collect_book_groups(root)
    total = len(groups)
    log.info("Library scan started: %d candidate book(s) found under %s", total, root)
    if not total:
        task.message = N_("No importable book files found")
        return
    task.message = N_("%(count)s book(s) found, importing", count=total)

    # index what is already in the database, keyed by (relative folder, file name stem)
    known_books = {}
    for book in calibre_dbb.session.query(db.Books).all():
        for data in book.data:
            known_books[(book.path.replace('\\', '/'), data.name.casefold())] = book.id

    imported = added_formats = skipped = 0
    for index, ((rel_dir, stem), files) in enumerate(sorted(groups.items())):
        if task.stat in (STAT_CANCELLED, STAT_ENDED):
            log.info("Library scan cancelled after %d of %d book(s)", index, total)
            return
        try:
            book_id = known_books.get((rel_dir, stem.casefold()))
            if book_id:
                added_formats += _add_missing_formats(calibre_dbb.session, book_id, stem, files)
                skipped += 1
            else:
                book_id = _import_book(calibre_dbb.session, rel_dir, stem, files, rar_executable)
                known_books[(rel_dir, stem.casefold())] = book_id
                imported += 1
        except Exception as ex:
            calibre_dbb.session.rollback()
            log.error_or_exception("Failed to import {}/{}: {}".format(rel_dir, stem, ex))
        task.progress = (index + 1) / total
        task.message = N_("Imported %(imported)s book(s), %(skipped)s already in library (of %(total)s)",
                          imported=imported, skipped=skipped, total=total)

    log.info("Library scan finished: %d imported, %d known (%d format(s) added), %d failed",
             imported, skipped, added_formats, total - imported - skipped)


def _add_missing_formats(session, book_id, stem, files):
    book = session.query(db.Books).filter(db.Books.id == book_id).first()
    if not book:
        return 0
    present = {data.format.lower() for data in book.data}
    added = 0
    for ext, file_path in files:
        if ext not in present:
            book.data.append(db.Data(book.id, ext.upper(), os.path.getsize(file_path), stem))
            added += 1
    if added:
        session.commit()
    return added


def _import_book(session, rel_dir, stem, files, rar_executable):
    ext, primary_path = _pick_primary_format(files)
    meta = _extract_metadata(primary_path, stem, ext, rar_executable)

    title = strip_whitespaces(meta.title) or stem
    author = strip_whitespaces(meta.author)
    input_authors = uniq([a.strip().replace(',', '|') for a in split_authors([author])]) if author else []
    input_authors = [a for a in input_authors if a] or [_('Unknown')]

    author_objects = []
    for author_name in input_authors:
        db_author = session.query(db.Authors).filter(db.Authors.name == author_name).first()
        if not db_author:
            db_author = db.Authors(author_name, get_sorted_author(author_name.replace('|', ',')))
            session.add(db_author)
            session.flush()
        author_objects.append(db_author)
    sort_authors = ' & '.join([a.sort or a.name for a in author_objects])

    try:
        pubdate = datetime.strptime(meta.pubdate[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        pubdate = db.Books.DEFAULT_PUBDATE
    now = datetime.now(timezone.utc)

    cover_source = meta.cover if meta.cover and os.path.isfile(meta.cover) else None
    book = db.Books(title, sort_authors, sort_authors, now, pubdate, '1', now, rel_dir, cover_source,
                    [], [], "")
    session.add(book)
    session.flush()
    # the Books constructor ignores the authors argument, the link is made via the relationship
    for author_object in author_objects:
        book.authors.append(author_object)

    for ext_format, file_path in files:
        book.data.append(db.Data(book.id, ext_format.upper(), os.path.getsize(file_path), stem))

    for language in _resolve_languages(session, meta.languages):
        book.languages.append(language)

    for tag_name in uniq([t.strip() for t in (meta.tags or '').split(',') if t.strip()]):
        book.tags.append(_get_or_add(session, db.Tags, db.Tags.name, tag_name, db.Tags(tag_name)))

    publisher = strip_whitespaces(meta.publisher or '')
    if publisher:
        book.publishers.append(_get_or_add(session, db.Publishers, db.Publishers.name, publisher,
                                           db.Publishers(publisher, None)))

    series = strip_whitespaces(meta.series or '')
    if series:
        book.series.append(_get_or_add(session, db.Series, db.Series.name, series,
                                       db.Series(series, db.title_sort(series, config))))
        book.series_index = meta.series_id or '1.0'

    description = strip_whitespaces(meta.description or '')
    if description:
        session.add(db.Comments(description, book.id))

    for identifier_type, identifier_value in meta.identifiers:
        if identifier_value and identifier_type:
            session.add(db.Identifiers(identifier_value, identifier_type, book.id))

    if cover_source:
        store_book_cover_sidecar(book.id, meta.cover)
        try:
            os.remove(meta.cover)
        except OSError:
            pass

    if not session.query(db.Metadata_Dirtied).filter(db.Metadata_Dirtied.book == book.id).one_or_none():
        session.add(db.Metadata_Dirtied(book.id))
    session.commit()
    add_book_to_thumbnail_cache(book.id)
    try:
        from .kosync import map_book_documents
        map_book_documents(book)
    except Exception as ex:
        log.warning("KOSync document mapping failed for book %s: %s", book.id, ex)
    return book.id
