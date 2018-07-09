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
from itertools import cycle

from subunit import ProtocolTestCase, TestProtocolClient
from subunit.test_results import AutoTimingTestResultDecorator

from testtools import ConcurrentTestSuite, iterate_tests

import bb.utils
import oe.path

_all__ = [
    'ConcurrentTestSuite',
    'fork_for_tests',
    'partition_tests',
]

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
        for process_tests in test_blocks:
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

                    subunit_result = AutoTimingTestResultDecorator(
                        TestProtocolClient(stream)
                    )
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
                result.append(test)
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
