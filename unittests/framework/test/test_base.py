# Copyright (c) 2014, 2016 Intel Corporation

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

""" Tests for the exectest module """

from __future__ import (
    absolute_import, division, print_function, unicode_literals
)
import os
import textwrap
try:
    import subprocess32 as subprocess
except ImportError:
    import subprocess

import pytest
import six

from framework import dmesg
from framework import log
from framework import monitoring
from framework import status
from framework.options import _Options as Options
from framework.test import base

from ..test_status import PROBLEMS
from .. import skip

# pylint: disable=invalid-name,no-self-use


class _Test(base.Test):
    """Helper with stubbed interpret_results method."""
    def interpret_result(self):
        super(_Test, self).interpret_result()


class TestTest(object):
    """Tests for the Test class."""

    class TestRun(object):
        """Tests for Test.run."""

        def test_return_early_when_run_command_excepts(self, mocker):
            """Test.run exits early when Test._run_command raises an exception.
            """
            t = _Test(['foo'])
            mocker.patch.object(t, '_run_command',
                                side_effect=base.TestRunError('foo', 'pass'))
            mocker.patch.object(t, 'interpret_result',
                                side_effect=Exception('Test failure'))

            t.run()

    @pytest.mark.skipif(six.PY2 and subprocess.__name__ != 'subprocess32',
                        reason='Python 2.7 requires subprocess32 to run this test')
    @skip.posix
    class TestRunCommand(object):
        """Tests for Test._run_command."""

        @classmethod
        def setup_class(cls):
            if os.name == 'posix':
                cls.command = ['sleep', '60']
            else:
                cls.command = ['timeout', '/t', '/nobreak', '60']

        @pytest.mark.timeout(6)
        def test_timeout_kill_children(self, mocker, tmpdir):
            """test.base.Test: kill children if terminate fails.

            This creates a process that forks multiple times, and then checks
            that the children have been killed.

            This test could leave processes running if it fails.
            """
            # This function is the only user of psutil, and by putting it in
            # the function itself we avoid needed to try/except it, and then
            # skip if we don't have it.
            import psutil

            class PopenProxy(object):
                """An object that proxies Popen, and saves the Popen instance
                as an attribute.

                This is useful for testing the Popen instance.
                """

                def __init__(self):
                    self.popen = None

                def __call__(self, *args, **kwargs):
                    self.popen = subprocess.Popen(*args, **kwargs)

                    # if communicate is called successfully then the proc will
                    # be reset to None, which will make the test fail.
                    self.popen.communicate = mocker.Mock(
                        return_value=('out', 'err'))

                    return self.popen

            # localpath doesn't have a seek option
            with tmpdir.join('test').open(mode='w', ensure=True) as f:
                # Create a file that wll be executed as a python script Create
                # a process with two subproccesses (not threads) that will run
                # for a long time.
                f.write(textwrap.dedent("""\
                    import time
                    from multiprocessing import Process

                    def p():
                        for _ in range(100):
                            time.sleep(1)

                    if __name__ == "__main__":
                        a = Process(target=p)
                        b = Process(target=p)
                        a.start()
                        b.start()
                        a.join()
                        b.join()
                """))
                f.seek(0)  # we'll need to read the file back

                # Create an object that will return a popen object, but also
                # store it so we can access it later
                proxy = PopenProxy()

                test = _Test(['python' + ('2' if six.PY2 else '3'),
                              six.text_type(f)])
                test.timeout = 1

                # mock out subprocess.Popen with our proxy object
                mock_subp = mocker.patch('framework.test.base.subprocess')
                mock_subp.Popen = proxy
                mock_subp.TimeoutExpired = subprocess.TimeoutExpired
                test.run()

                # Check to see if the Popen has children, even after it should
                # have received a TimeoutExpired.
                proc = psutil.Process(os.getsid(proxy.popen.pid))
                children = proc.children(recursive=True)

            if children:
                # If there are still running children attempt to clean them up,
                # starting with the final generation and working back to the
                # first
                for child in reversed(children):
                    child.kill()

                raise Exception('Test process had children when it should not')

        @pytest.mark.slow
        @pytest.mark.timeout(6)
        def test_timeout(self):
            """test.base.Test: Stops running test after timeout expires.

            This is a little bit of extra time here, but without a sleep of 60
            seconds if the test runs 6 seconds it's run too long, we do need to
            allow a bit of time for teardown to happen.
            """
            test = _Test(self.command)
            test.timeout = 1
            test.run()

        @pytest.mark.slow
        @pytest.mark.timeout(6)
        def test_timeout_status(self):
            """test.base.Test: Setst the status to 'timeout' when then timeout
            is exceeded

            This is a little bit of extra time here, but without a sleep of 60
            seconds if the test runs 6 seconds it's run too long, we do need to
            allow a bit of time for teardown to happen.
            """
            test = _Test(self.command)
            test.timeout = 1
            test.run()
            assert test.result.result is status.TIMEOUT

    class TestExecuteTraceback(object):
        """Test.execute tests for Traceback handling."""

        class Sentinal(Exception):
            pass

        @pytest.fixture
        def shared_test(self, mocker):
            """Do some setup."""
            test = _Test(['foo'])
            test.run = mocker.Mock(side_effect=self.Sentinal)

            test.execute(mocker.Mock(spec=six.text_type),
                         mocker.Mock(spec=log.BaseLog),
                         mocker.Mock(spec=dmesg.BaseDmesg),
                         mocker.Mock(spec=monitoring.Monitoring))
            return test.result

        def test_result(self, shared_test):
            """Test.execute (exception): Sets the result to fail."""
            assert shared_test.result is status.FAIL

        def test_traceback(self, shared_test):
            """Test.execute (exception): Sets the traceback.

            It's fragile to record the actual traceback, and it's unlikely that
            it can easily be implemented differently than the way the original
            code is implemented, so this doesn't do that, it just verifies
            there is a value.
            """
            assert shared_test.traceback != ''
            assert isinstance(shared_test.traceback, six.string_types)

        def test_exception(self, shared_test):
            """Test.execute (exception): Sets the exception.

            This is much like the traceback, it's difficult to get the correct
            value, so just make sure it's being set.
            """
            assert shared_test.exception != ''
            assert isinstance(shared_test.exception, six.string_types)

    class TestCommand(object):
        """Tests for Test.command."""

        def test_string_for_command(self):
            """Asserts if it is passed a string instead of a list."""
            with pytest.raises(AssertionError):
                _Test('foo')

        def test_mutation(self):
            """test.base.Test.command: does not mutate the value it was
            provided.

            There was a very subtle bug in all.py that causes the key values to
            be changed before they are assigned in some cases. This is because
            the right side of an assignment is evalated before the left side,
            so:

            >>> profile = {}
            >>> args = ['a', 'b']
            >>> profile[' '.join(args)] = PiglitGLTest(args)
            >>> list(profile.keys())
            ['bin/a b']
            """
            class MyTest(_Test):
                def __init__(self, *args, **kwargs):
                    super(MyTest, self).__init__(*args, **kwargs)
                    self._command[0] = 'bin/' + self._command[0]

            args = ['a', 'b']
            _Test(args)

            assert args == ['a', 'b']

    class TestInterpretResult(object):
        """Tests for Test.interpret_result."""

        def test_returncode_greater_zero(self):
            """A test with status > 0 is fail."""
            test = _Test(['foobar'])
            test.result.returncode = 1
            test.result.out = 'this is some\nstdout'
            test.result.err = 'this is some\nerrors'
            test.interpret_result()

            assert test.result.result is status.FAIL


class TestWindowResizeMixin(object):
    """Tests for the WindowResizeMixin class."""

    def test_rerun(self):
        """test.base.WindowResizeMixin: runs multiple when spurious resize
        detected.
        """
        # Because of Python's inheritance order we need another mixin.
        class Mixin(object):
            def __init__(self, *args, **kwargs):
                super(Mixin, self).__init__(*args, **kwargs)
                self.__return_spurious = True

            def _run_command(self):
                self.result.returncode = None

                # IF this is run only once we'll have "got spurious window resize"
                # in result.out, if it runs multiple times we'll get 'all good'
                if self.__return_spurious:
                    self.result.out = "Got spurious window resize"
                    self.__return_spurious = False
                else:
                    self.result.out = 'all good'

        class Test_(base.WindowResizeMixin, Mixin, _Test):
            pass

        test = Test_(['foo'])
        test.run()
        assert test.result.out == 'all good'


class TestValgrindMixin(object):
    """Tests for the ValgrindMixin class."""

    def test_command(self, mocker):
        """test.base.ValgrindMixin.command: overrides self.command."""
        opts = mocker.patch('framework.test.base.options.OPTIONS',
                            new_callable=Options)

        class Test(base.ValgrindMixin, _Test):
            pass

        opts.valgrind = True

        test = Test(['foo'])
        assert test.command == ['valgrind', '--quiet', '--error-exitcode=1',
                                '--tool=memcheck', 'foo']

    class TestRun(object):
        """Tests for the run method."""

        @classmethod
        def setup_class(cls):
            class _NoRunTest(_Test):
                def run(self):
                    self.interpret_result()

            class Test(base.ValgrindMixin, _NoRunTest):
                pass

            cls.test = Test

        # The ids function here is a bit of a hack to work around the
        # pytest-timeout plugin, which is broken. when 'timeout' is passed as a
        # string using six.text_type it tries to grab that value and a flaming
        # pile ensues
        @pytest.mark.parametrize("starting", PROBLEMS,
                                 ids=lambda x: six.text_type(x).upper())
        def test_problem_status_changes_valgrind_enabled(self, starting, mocker):
            """When running with valgrind mode we're actually testing the test
            binary itself, so any status other than pass is irrelavent, and
            should be marked as skip.
            """
            mock_opts = mocker.patch('framework.test.base.options.OPTIONS',
                                     new_callable=Options)
            mock_opts.valgrind = True

            test = self.test(['foo'])
            test.result.result = starting
            test.run()
            assert test.result.result is status.SKIP

        @pytest.mark.parametrize("starting", PROBLEMS,
                                 ids=lambda x: six.text_type(x).upper())
        def test_problems_with_valgrind_disabled(self, starting, mocker):
            """When valgrind is disabled nothign shoud change
            """
            mock_opts = mocker.patch('framework.test.base.options.OPTIONS',
                                     new_callable=Options)
            mock_opts.valgrind = False

            test = self.test(['foo'])
            test.result.result = starting
            test.result.returncode = 0
            test.run()
            assert test.result.result is starting

        def test_passed_valgrind(self, mocker):
            """test.base.ValgrindMixin.run: when test is 'pass' and returncode
            is '0' result is pass.
            """
            mock_opts = mocker.patch('framework.test.base.options.OPTIONS',
                                     new_callable=Options)
            test = self.test(['foo'])
            mock_opts.valgrind = True
            test.result.result = status.PASS
            test.result.returncode = 0
            test.run()
            assert test.result.result is status.PASS

        def test_failed_valgrind(self, mocker):
            """test.base.ValgrindMixin.run: when a test is 'pass' but
            returncode is not 0 it's 'fail'.
            """
            mock_opts = mocker.patch('framework.test.base.options.OPTIONS',
                                     new_callable=Options)
            mock_opts.valgrind = True
            test = self.test(['foo'])
            test.result.result = status.PASS
            test.result.returncode = 1
            test.interpret_result()
            assert test.result.result is status.FAIL
