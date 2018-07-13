#!/usr/bin/env python3
#
# Modified for use in OE by Richard Purdie, 2018
#
# Modified by: Corey Goldberg, 2013
#   License: GPLv2+
#
# Original code from:
#   Bazaar (bzrlib.tests.__init__.py, v2.6, copied Jun 01 2013)
#   Copyright (C) 2005-2011 Canonical Ltd
#   License: GPLv2+

import os
import sys
import traceback
import unittest
import subprocess
import testtools
from itertools import cycle
from queue import Queue
import threading
import time
import io

from subunit import ProtocolTestCase, TestProtocolClient
from subunit.test_results import AutoTimingTestResultDecorator

from testtools import ThreadsafeForwardingResult, iterate_tests

import bb.utils
import oe.path

_all__ = [
    'ConcurrentTestSuite',
    'fork_for_tests',
    'partition_tests',
]

class BBThreadsafeForwardingResult(ThreadsafeForwardingResult):

    def __init__(self, target, semaphore, testnum, total):
        super(BBThreadsafeForwardingResult, self).__init__(target, semaphore)
        self.testnum = testnum
        self.total = total

    def _add_result_with_semaphore(self, method, test, *args, **kwargs):
        self.semaphore.acquire()
        try:
            self.result.starttime[test.id()] = self._test_start.timestamp()
            self.result.proginfo2[self.testnum].append(test.id())
            self.result.proginfo[test.id()] = "%s:%s/%s (%ss) (%s)" % (self.testnum, len(self.result.proginfo2[self.testnum]), self.total, "{0:.2f}".format(time.time()-self._test_start.timestamp()), test.id())
        finally:
            self.semaphore.release()
        super(BBThreadsafeForwardingResult, self)._add_result_with_semaphore(method, test, *args, **kwargs)

class dummybuf(object):
   def __init__(self, parent):
       self.p = parent
   def write(self, data):
       self.p.write(data.decode("utf-8"))

class ConcurrentTestSuite(unittest.TestSuite):
    """A TestSuite whose run() calls out to a concurrency strategy."""

    def __init__(self, suite, make_tests):
        """Create a ConcurrentTestSuite to execute suite.

        :param suite: A suite to run concurrently.
        :param make_tests: A helper function to split the tests in the
            ConcurrentTestSuite into some number of concurrently executing
            sub-suites. make_tests must take a suite, and return an iterable
            of TestCase-like object, each of which must have a run(result)
            method.
        """
        super(ConcurrentTestSuite, self).__init__([suite])
        self.make_tests = make_tests

    def run(self, result):
        """Run the tests concurrently.

        This calls out to the provided make_tests helper, and then serialises
        the results so that result only sees activity from one TestCase at
        a time.

        ConcurrentTestSuite provides no special mechanism to stop the tests
        returned by make_tests, it is up to the make_tests to honour the
        shouldStop attribute on the result object they are run with, which will
        be set if an exception is raised in the thread which
        ConcurrentTestSuite.run is called in.
        """
        tests = self.make_tests(self)
        try:
            threads = {}
            queue = Queue()
            semaphore = threading.Semaphore(1)
            result.proginfo2 = {}
            for i, (test, testnum) in enumerate(tests):
                result.proginfo2[i] = []
                process_result = BBThreadsafeForwardingResult(result, semaphore, i, testnum)
                # Force buffering of stdout/stderr so the console doesn't get corrupted by test output
                # as per default in parent code
                process_result.buffer = True
                process_result._stderr_buffer = io.StringIO()
                process_result._stderr_buffer.buffer = dummybuf(process_result._stderr_buffer)
                process_result._stdout_buffer = io.StringIO()
                process_result._stdout_buffer.buffer = dummybuf(process_result._stdout_buffer)
                reader_thread = threading.Thread(
                    target=self._run_test, args=(test, process_result, queue))
                threads[test] = reader_thread, process_result
                reader_thread.start()
            while threads:
                finished_test = queue.get()
                threads[finished_test][0].join()
                del threads[finished_test]
        except:
            for thread, process_result in threads.values():
                process_result.stop()
            raise

    def _run_test(self, test, process_result, queue):
        try:
            try:
                test.run(process_result)
            except Exception:
                # The run logic itself failed.
                case = testtools.ErrorHolder(
                    "broken-runner",
                    error=sys.exc_info())
                case.run(process_result)
        finally:
            queue.put(test)


def fork_for_tests(concurrency_num):
    """Implementation of `make_tests` used to construct `ConcurrentTestSuite`.
    :param concurrency_num: number of processes to use.
    """
    def removebuilddir(d):
        delay = 5
        while delay and os.path.exists(d + "/bitbake.lock"):
            time.sleep(1)
            delay = delay - 1
        bb.utils.prunedir(d)

    def do_fork(suite):
        """Take suite and start up multiple runners by forking (Unix only).
        :param suite: TestSuite object.
        :return: An iterable of TestCase-like objects which can each have
        run(result) called on them to feed tests to result.
        """
        result = []
        test_blocks = partition_tests(suite, concurrency_num)
        # Clear the tests from the original suite so it doesn't keep them alive
        suite._tests[:] = []
        print(str(test_blocks))
        for process_tests in test_blocks:
            print(str(process_tests))
            numtests = len(process_tests)
            process_suite = unittest.TestSuite(process_tests)
            # Also clear each split list so new suite has only reference
            process_tests[:] = []
            c2pread, c2pwrite = os.pipe()
            sys.stdout.flush()
            sys.stderr.flush()
            pid = os.fork()
            if pid == 0:
                ourpid = os.getpid()
                try:
                    newbuilddir = None
                    stream = os.fdopen(c2pwrite, 'wb', 1)
                    os.close(c2pread)

                    # Create a new separate BUILDDIR for each group of tests
                    if 'BUILDDIR' in os.environ:
                        builddir = os.environ['BUILDDIR']
                        newbuilddir = builddir + "-st-" + str(ourpid)
                        selftestdir = os.path.abspath(builddir + "/../meta-selftest")
                        newselftestdir = newbuilddir + "/meta-selftest"

                        bb.utils.mkdirhier(newbuilddir)
                        oe.path.copytree(builddir + "/conf", newbuilddir + "/conf")
                        oe.path.copytree(builddir + "/cache", newbuilddir + "/cache")
                        oe.path.copytree(selftestdir, newselftestdir)

                        for e in os.environ:
                            if builddir in os.environ[e]:
                                os.environ[e] = os.environ[e].replace(builddir, newbuilddir)

                        subprocess.check_output("git init; git add *; git commit -a -m 'initial'", cwd=newselftestdir, shell=True)

                        # Tried to used bitbake-layers add/remove but it requires recipe parsing and hence is too slow
                        subprocess.check_output("sed %s/conf/bblayers.conf -i -e 's#%s#%s#g'" % (newbuilddir, selftestdir, newselftestdir), cwd=newbuilddir, shell=True)

                        os.chdir(newbuilddir)

                        for t in process_suite:
                            if not hasattr(t, "tc"):
                                continue
                            cp = t.tc.config_paths
                            for p in cp:
                                if selftestdir in cp[p] and newselftestdir not in cp[p]:
                                    cp[p] = cp[p].replace(selftestdir, newselftestdir)
                                if builddir in cp[p] and newbuilddir not in cp[p]:
                                    cp[p] = cp[p].replace(builddir, newbuilddir)

                    # Leave stderr and stdout open so we can see test noise
                    # Close stdin so that the child goes away if it decides to
                    # read from stdin (otherwise its a roulette to see what
                    # child actually gets keystrokes for pdb etc).
                    newsi = os.open(os.devnull, os.O_RDWR)
                    os.dup2(newsi, sys.stdin.fileno())

                    subunit_client = TestProtocolClient(stream)
                    # Force buffering of stdout/stderr so the console doesn't get corrupted by test output
                    # as per default in parent code
                    subunit_client.buffer = True
                    subunit_result = AutoTimingTestResultDecorator(subunit_client)
                    process_suite.run(subunit_result)
                    if ourpid != os.getpid():
                        os._exit(0)
                    if newbuilddir:
                        removebuilddir(newbuilddir)
                except:
                    # Don't do anything with process children
                    if ourpid != os.getpid():
                        os._exit(1)
                    # Try and report traceback on stream, but exit with error
                    # even if stream couldn't be created or something else
                    # goes wrong.  The traceback is formatted to a string and
                    # written in one go to avoid interleaving lines from
                    # multiple failing children.
                    try:
                        stream.write(traceback.format_exc().encode('utf-8'))
                    except:
                        sys.stderr.write(traceback.format_exc())
                    finally:
                        if newbuilddir:
                            removebuilddir(newbuilddir)
                        os._exit(1)
                os._exit(0)
            else:
                os.close(c2pwrite)
                stream = os.fdopen(c2pread, 'rb', 1)
                test = ProtocolTestCase(stream)
                result.append((test, numtests))
        return result
    return do_fork


def partition_tests(suite, count):
    """Partition suite into count lists of tests."""
    # This just assigns tests in a round-robin fashion.  On one hand this
    # splits up blocks of related tests that might run faster if they shared
    # resources, but on the other it avoids assigning blocks of slow tests to
    # just one partition.  So the slowest partition shouldn't be much slower
    # than the fastest.
    modules = {}
    for test in iterate_tests(suite):
        m = test.__module__ + "." + test.__class__.__name__
        if m not in modules:
            modules[m] = []
        modules[m].append(test)

    partitions = [list() for _ in range(count)]
    for partition, m in zip(cycle(partitions), modules):
        partition.extend(modules[m])

    # No point in empty threads so drop them
    return [p for p in partitions if p]


if __name__ == '__main__':
    import time

    class SampleTestCase(unittest.TestCase):
        """Dummy tests that sleep for demo."""

        def test_me_1(self):
            time.sleep(0.5)

        def test_me_2(self):
            time.sleep(0.5)

        def test_me_3(self):
            time.sleep(0.5)

        def test_me_4(self):
            time.sleep(0.5)

    # Load tests from SampleTestCase defined above
    suite = unittest.TestLoader().loadTestsFromTestCase(SampleTestCase)
    runner = unittest.TextTestRunner()

    # Run tests sequentially
    runner.run(suite)

    # Run same tests across 4 processes
    suite = unittest.TestLoader().loadTestsFromTestCase(SampleTestCase)
    concurrent_suite = ConcurrentTestSuite(suite, fork_for_tests(4))
    runner.run(concurrent_suite)
