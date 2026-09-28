# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#    Copyright (C) 2023 OzzieIsaacs
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

from tempfile import gettempdir
import os
import shutil
import zipfile
import mimetypes
import hashlib
from io import BytesIO

from . import logger, constants

log = logger.create()

try:
    import magic
    error = None
except (ImportError, FileNotFoundError) as e:
    error = "Cannot import python-magic, checking uploaded file metadata will not work: {}".format(e)


def get_mimetype(ext):
    # overwrite some mimetypes for proper file detection
    mimes = {".fb2": "text/xml",
             ".cbz": "application/zip",
             ".cbr": "application/x-rar"
             }
    return mimes.get(ext, mimetypes.types_map[ext])


def get_temp_dir():
    base_tmp_dir = os.path.join(gettempdir(), 'calibre_web')
    from . import config
    instance_value = "{}|{}".format(config.config_calibre_dir or "", config.config_port or "")
    instance_key = hashlib.md5(instance_value.encode('utf-8')).hexdigest()  # nosec
    tmp_dir = os.path.join(base_tmp_dir, "instance_{}".format(instance_key[:12]))
    if not os.path.isdir(tmp_dir):
        os.makedirs(tmp_dir)
    return tmp_dir


def del_temp_dir():
    tmp_dir = get_temp_dir()
    shutil.rmtree(tmp_dir)


def get_local_book_cover_path(book):
    # Books imported in place keep their covers in the sidecar dir (keyed by book id),
    # so no files need to be written into the original book folders. Books living in
    # calibre-style folders still use the classic cover.jpg.
    from . import config
    sidecar_cover = os.path.join(config.get_book_path(), constants.COVER_SIDECAR_DIR, str(book.id) + '.jpg')
    if os.path.isfile(sidecar_cover):
        return sidecar_cover
    return os.path.join(config.get_book_path(), book.path, 'cover.jpg')


def store_book_cover_sidecar(book_id, cover_source_path):
    from . import config
    sidecar_dir = os.path.join(config.get_book_path(), constants.COVER_SIDECAR_DIR)
    os.makedirs(sidecar_dir, exist_ok=True)
    dest = os.path.join(sidecar_dir, str(book_id) + '.jpg')
    shutil.copyfile(cover_source_path, dest)
    return dest


def remove_book_cover_sidecar(book_id):
    from . import config
    sidecar_cover = os.path.join(config.get_book_path(), constants.COVER_SIDECAR_DIR, str(book_id) + '.jpg')
    try:
        if os.path.isfile(sidecar_cover):
            os.remove(sidecar_cover)
    except OSError as e:
        log.error("Failed to remove sidecar cover for book %s: %s", book_id, e)


def validate_mime_type(file_buffer, allowed_extensions):
    if error:
        log.error(error)
        return False
    mime = magic.Magic(mime=True)
    allowed_mimetypes = list()
    for x in allowed_extensions:
        try:
            allowed_mimetypes.append(get_mimetype("." + x))
        except KeyError:
            log.error("Unkown mimetype for Extension: {}".format(x))
    tmp_mime_type = mime.from_buffer(file_buffer.read())
    file_buffer.seek(0)
    if any(mime_type in tmp_mime_type for mime_type in allowed_mimetypes):
        return True
    # Some epubs show up as zip mimetypes
    elif "zip" in tmp_mime_type:
        try:
            with zipfile.ZipFile(BytesIO(file_buffer.read()), 'r') as epub:
                file_buffer.seek(0)
                if "mimetype" in epub.namelist():
                    return True
        except:
            file_buffer.seek(0)
    log.error("Mimetype '{}' not found in allowed types".format(tmp_mime_type))
    return False
