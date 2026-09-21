"""
The test runner for `manage.py test`: Django's own, plus one tolerance.

WHY: the test database is a SQLite FILE (settings.py, TEST MODE) because the
reply threads write concurrently and in-memory SQLite fails those writes at
once with "database table is locked". A file honours a busy timeout. But on
Windows a file that any connection still holds cannot be deleted, and the
daemon reply threads (`delayed_response`, the photo and contact-card sends)
can still hold theirs when the run ends, so Django's teardown raised
PermissionError and the whole gate exited 1 after every test had passed.

HOW: each run gets its own file (the pid is in the name, settings.py), so a
file left behind never collides with the next run; teardown ignores only that
"file in use" error; and files from earlier runs are swept at startup.
"""

import glob
import os
import tempfile

from django.test.runner import DiscoverRunner

TEST_DB_PREFIX = 'plumbot_test_db_'


class PlumbotTestRunner(DiscoverRunner):

    def setup_databases(self, **kwargs):
        # Leftovers from runs whose teardown could not delete them. A file a
        # live run still holds cannot be removed, and is skipped.
        for path in glob.glob(os.path.join(tempfile.gettempdir(), TEST_DB_PREFIX + '*')):
            try:
                os.remove(path)
            except OSError:
                pass
        return super().setup_databases(**kwargs)

    def teardown_databases(self, old_config, **kwargs):
        try:
            super().teardown_databases(old_config, **kwargs)
        except PermissionError:
            # A reply thread still holds the file. Every test has already run
            # and reported; the file is swept by the next run's setup.
            pass
