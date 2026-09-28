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

from flask_babel import force_locale, lazy_gettext as N_

from cps import app, config, db, logger
from cps.services.worker import CalibreTask, STAT_CANCELLED, STAT_ENDED


class TaskScanImport(CalibreTask):
    def __init__(self, sub_path='', task_message=N_('Scanning library')):
        super(TaskScanImport, self).__init__(task_message)
        self.log = logger.create()
        self.sub_path = sub_path

    def run(self, worker_thread):
        # imported here to avoid a circular import (import_scan registers the route)
        from cps.import_scan import run_scan

        if self.stat == STAT_CANCELLED or self.stat == STAT_ENDED:
            return
        # no request context in the worker thread: pin the locale so gettext calls
        # inside the scan don't try to resolve it from the request
        with app.app_context(), force_locale(config.config_default_locale):
            calibre_dbb = db.CalibreDB(app)
            try:
                run_scan(self, calibre_dbb)
                self._handleSuccess()
            except Exception as ex:
                self.log.error_or_exception('Library scan failed: ' + str(ex))
                self._handleError('Library scan failed: ' + str(ex))
                calibre_dbb.session.rollback()

    @property
    def name(self):
        return N_('Scan Library')

    def __str__(self):
        return "Scan library{}".format(": " + self.sub_path if self.sub_path else "")

    @property
    def is_cancellable(self):
        return True
