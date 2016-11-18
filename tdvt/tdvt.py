"""
    Test driver script for the Tableau Datasource Verification Tool

"""

import os
import sys
import argparse
import subprocess
import shutil
import tempfile
import threading
import time
import json
import logging
from zipfile import ZipFile
import glob
from .tdvt_core import generate_files, run_diff, run_failed_tests, run_tests, configure_tabquery_path, TdvtTestConfig
from .config_gen.test_config import SingleTestConfig, SingleLogicalTestConfig, SingleExpressionTestConfig

#This contains the dictionary of configs you can run.
from .config_gen.datasource_list import WindowsRegistry,MacRegistry
from .config_gen.test_config import TestSet

class TestOutputFiles(object):
        output_actuals = 'tdvt_actuals_combined.zip'
        output_csv ="test_results_combined.csv"
        output_json = "tdvt_output_combined.json"
        all_output_files = [output_actuals, output_csv, output_json]

class TestRunner(threading.Thread):
    def __init__(self, test_set, test_config, lock, verbose):
        threading.Thread.__init__(self)
        self.test_config = test_config
        self.error_code = 0
        self.verbose = verbose
        self.thread_lock = lock
        self.temp_dir = tempfile.mkdtemp(prefix=self.test_config.suite_name)
        self.test_config.output_dir = self.temp_dir
        self.sub_thread_count = 1

    def copy_actual_files(self):
        dst = os.path.join(os.getcwd(), TestOutputFiles.output_actuals)
        mode = 'w' if not os.path.isfile(dst) else 'a'
        glob_path = os.path.join(self.temp_dir, 'actual.*')
        actual_files = glob.glob( glob_path )
        with ZipFile(dst, mode) as myzip:
            for actual in actual_files:
                myzip.write( actual )

    def copy_output_files(self):
        self.copy_output_file("test_results.csv", TestOutputFiles.output_csv, True)

    def copy_output_file(self, src, dst, trim_header):
        src = os.path.join(self.temp_dir, src)
        dst = os.path.join(os.getcwd(), dst)
        try:
            dst_exists = os.path.isfile(dst)
            src_file = open(src, 'r', encoding='utf8')
            mode = 'w' if not dst_exists else 'a'
            dst_file = open(dst, mode, encoding='utf8')

            line_count = 0
            for line in src_file:
                line_count += 1
                if line_count == 1 and trim_header and dst_exists:
                    continue
                dst_file.write(line)

            src_file.close()
            dst_file.close()
        except IOError:
            return

    def copy_test_result_file(self):
        src = os.path.join(self.temp_dir, "tdvt_output.json")
        dst = os.path.join(os.getcwd(), TestOutputFiles.output_json)
        try:
            if not os.path.isfile(dst):
                shutil.copyfile(src, dst)
            else:
                src_file = open(src, 'r', encoding='utf8')
                results = json.load(src_file)
                src_file.close()

                dst_file = open(dst, 'r', encoding='utf8')
                existing_results = json.load(dst_file)
                dst_file.close()

                existing_results['failed_tests'].extend(results['failed_tests'])

                existing_results['successful_tests'].extend(results['successful_tests'])
                
                dst_file = open(dst, 'w', encoding='utf8')
                json.dump(existing_results, dst_file)
                dst_file.close()
        except IOError:
            return

    def run(self):
        #Send output to null.
        DEVNULL = open(os.devnull, 'wb')
        output = DEVNULL if not self.verbose else None
        self.thread_lock.acquire()
        print ('\n')
        print ("Calling " + str(self.test_config))
        print ('\n')
        self.thread_lock.release()

        start_time = time.time()
        error_code = run_tests(self.test_config)
        self.thread_lock.acquire()
        run_type = 'logical' if self.test_config.logical else 'expression'
        try:
            self.copy_actual_files()
            self.copy_output_files()
            self.copy_test_result_file()
        except Exception as e:
            print (e)
            pass
        self.thread_lock.release()
        
        self.error_code = error_code

    def __del__(self):
        try:
            shutil.rmtree(self.temp_dir)
        except:
            pass

def delete_output_files(root_dir):
    for f in TestOutputFiles.all_output_files:
        out_file = os.path.join(root_dir, f)
        if os.path.exists(out_file):
            try:
                os.unlink(out_file)
            except Exception as e:
                print (e)
                continue

def get_datasource_registry(platform):
    """Get the datasources to run based on the suite parameter."""
    if sys.platform.startswith("darwin"):
        reg= MacRegistry()
    else:
        reg = WindowsRegistry()

    return reg

def print_configurations(ds_reg, dsname):
    print ("\nAvailable datasources")
    ds_all = ds_reg.get_datasources('all')
    for ds in sorted(ds_all):
        print (ds)
    if dsname:
        ds_to_run = ds_reg.get_datasources(dsname)
        print ("\nDatasource set: " + dsname)
        for ds in ds_to_run:
            print ("\n\t" + ds)
            test_config = ds_reg.get_datasource_info(ds)
            if not test_config:
                continue
            print ("\tLogical tests:")
            for x in test_config.get_logical_tests():
                print ("\t"*2 + x.config_file_name)
            print ("\tExpression tests:")
            for x in test_config.get_expression_tests():
                print ("\t"*2 + x.config_file_name)
    print ("\nAvailable suites:")
    for suite in ds_reg.suite_map:
        print (suite)

def get_temporary_logical_test_config(temp_configs, test_pattern, tds_pattern, exclude_pattern, ds_info):
        if not test_pattern or not tds_pattern:
            return None
        single_test = SingleLogicalTestConfig(test_pattern, tds_pattern, exclude_pattern, ds_info)
        temp_configs.append(single_test)
        return single_test

def get_temporary_expression_test_config(temp_configs, test_pattern, tds_pattern, exclude_pattern, ds_info):
        if not test_pattern or not tds_pattern:
            return None
        single_test = SingleExpressionTestConfig(test_pattern, tds_pattern, exclude_pattern, ds_info)
        temp_configs.append(single_test)
        return single_test

def get_test_sets_to_run(function_call, test_pattern, single_test):
        test_sets_to_run = [] 
        if single_test and single_test.valid:
            test_sets_to_run.append(TestSet(single_test.temp_cfg_path, single_test.tds_name, '', ''))
        else:
            test_sets_to_run = function_call(test_pattern)

        return test_sets_to_run

def enqueue_tests(is_logical, ds_info, args, single_test, suite, lock, test_threads, test_run):

    tests = None
    if is_logical:
        tests = get_test_sets_to_run(ds_info.get_logical_tests, args.logical_only, single_test)
    else:
        tests = get_test_sets_to_run(ds_info.get_expression_tests, args.expression_only, single_test)

    if not tests:
        return

    for test_set in tests:
        test_config = TdvtTestConfig(from_args=args)
        test_config.suite_name = suite
        test_config.logical = is_logical
        test_config.d_override = ds_info.d_override
        test_config.tds = test_set.tds_name
        test_config.config_file = test_set.config_file_name

        runner = TestRunner(test_set, test_config, lock, VERBOSE)
        test_threads.append(runner)
        test_run += 1

def get_level_of_parallelization(args, total_threads):
    #This indicates how many database/test suite combinations to run at once
    max_threads = 12
    #This indicates how many tests in each test suite thread to run at once. Each test is a database connection.
    max_sub_threads = 4

    if args.thread_count or args.thread_count_tdvt:
        if args.thread_count:
            max_threads = args.thread_count
        if args.thread_count_tdvt:
            max_sub_threads = args.thread_count_tdvt
    else:
        if total_threads < max_threads and total_threads > 0:
            #There are fewer main threads needed than available threads, so increase the tdvt threads.
            sub_threads = int((max_threads * max_sub_threads)/total_threads)
            #Keep it reasonable.
            max_sub_threads = min(sub_threads, 16)
    print ("Setting tdvt thread count to: " + str(max_threads))
    print ("Setting sub thread count to : " + str(max_sub_threads))
    return max_threads, max_sub_threads

def usage_text():
    return '''
    TDVT Driver. Run groups of logical and expression tests against one or more datasources.

    Show all test suites
        tdvt_runner --list

    See what a test suite consists of
        tdvt_runner --list sqlserver
        tdvt_runner --list standard

    The 'run' argument can take a single datasource, a list of data sources, or a test suite name. in any combination.
        tdvt_runner --run vertica
        tdvt_runner --run sqlserver,vertica
        tdvt_runner --run standard

    Both logical and expression tests are run by default.
    Run all sqlserver expression tests
        tdvt_runner -e --run sqlserver

    Run all vertica logical tests
        tdvt_runner -q --run vertica

    There are two groups of expression tests, standard and LOD (level of detail). The config files that drive the tests are named expression_test.sqlserver.cfg and expression.lod.sqlserver.cfg.
    To run just one of those try entering part of the config name as an argument:
        tdvt_runner -e lod --run sqlserver
    This will run all the LOD tests against sqlserver.

    And you can run all the LOD tests against the standard datasources like
        tdvt_runner -e lod --run standard

    Run one test against many datasources
        tdvt_runner --exp exprtests/standard/setup.date.datepart.second*.txt --tdp cast_calcs.*.tds --run sqlserver,vertica

    The 'exp' argument is a glob pattern that is used to find the test file. It is the same style as what you will find in the existing *.cfg files.
    The 'test-ex' argument can be used to exclude test files. This is a regular expression pattern.
    The tds pattern is used to find the tds. Use a '*' character where the tds name will be substituted, ie cast_calcs.*.tds for cast_calcs.sqlserver.tds etc.

    Run one logical query test against many datasources
        tdvt_runner --logp logicaltests/setup/calcs/setup.BUGS.B1713.?.xml --tdp cast_calcs.*.tds --run postgres

    This can be combined with * to run an arbitrary set of 'correct' logical query tests against a datasources
        tdvt_runner --logp logicaltests/setup/calcs/setup.BUGS.*.?.xml --tdp cast_calcs.*.tds --run postgres
    Alternatively
        tdvt_runner --logp logicaltests/setup/calcs/setup.BUGS.*.dbo.xml --tdp cast_calcs.*.tds --run sqlserver

    But skip 59740?
        tdvt_runner --logp logicaltests/setup/calcs/setup.BUGS.*.dbo.xml --tdp cast_calcs.*.tds --test-ex 59740 --run sqlserver

    '''

def create_parser():
    parser = argparse.ArgumentParser(description='TDVT Driver.', usage=usage_text())
    parser.add_argument('--list', dest='list_ds', help='List datasource config.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--generate', dest='generate', action='store_true', help='Force config file generation.', required=False)
    parser.add_argument('--run', '-r', dest='ds', help='Comma separated list of Datasource names to test or \'all\'.', required=False)
    parser.add_argument('--logical', '-q', dest='logical_only', help='Only run logical tests whose config file name matches the supplied string, or all if blank.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--expression', '-e', dest='expression_only', help='Only run expression tests whose config file name matches the suppled string, or all if blank.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--expected-dir', dest='expected_dir', help='Unique subdirectory for expected files.', required=False)
    parser.add_argument('--threads', '-t', dest='thread_count', type=int, help='Max number of threads to use.', required=False)
    parser.add_argument('--threads_sub', '-tt', dest='thread_count_tdvt', type=int, help='Max number of threads to use for the subprocess calls. There is one database connection per subprocess call.', required=False)
    parser.add_argument('--verbose', dest='verbose', action='store_true', help='Verbose output.', required=False)
    parser.add_argument('--no-clean', dest='noclean', action='store_true', help='Leave temp dirs.', required=False)
    parser.add_argument('--exp', dest='expression_pattern', help='Only run expression tests whose name and path matches the suppled string. This is a glob pattern. Also set the tds-pattern to use when running the test.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--logp', dest='logical_pattern', help='Only run logical tests whose name and path matches the suppled string. this is a glob pattern. Also set the tds-pattern to use when running the test. Use a ? to replace the logical query config component of the test name.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--tdp', dest='tds_pattern', help='The datasource tds pattern to use when running the test. See exp and logp arguments.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--test-ex', dest='test_pattern_exclude', help='Exclude tests whose name matches the suppled string. This is a regular expression pattern. Can be used with exp and logp arguments. Also set the tds-pattern to use when running the test.', required=False, default=None, const='', nargs='?')
    parser.add_argument('--compare-sql', dest='compare_sql', action='store_true', help='Compare SQL.', required=False)
    parser.add_argument('--nocompare-tuples', dest='nocompare_tuples', action='store_true', help='Do not compare Tuples.', required=False)
    parser.add_argument('--diff-test', '-dd', dest='diff', help='Diff the results of the given test (ie exprtests/standard/setup.calcs_data.txt) against the expected files. Can be used with the sql and tuple options.', required=False)
    parser.add_argument('-f', dest='run_file', help='Json file containing failed tests to run.', required=False)
    return parser

def init():
    parser = create_parser()
    args = parser.parse_args()
    global VERBOSE
    VERBOSE = args.verbose
    #Create logger.
    logging.basicConfig(filename='tdvt_log_combined.txt',level=logging.DEBUG, filemode='w', format='%(asctime)s %(message)s')
    logger = logging.getLogger()
    if VERBOSE:
        #Log to console also.
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        logger.addHandler(ch)
    
    ds_reg = get_datasource_registry(sys.platform)
    configure_tabquery_path()

    return parser, ds_reg, args

def active_thread_count(threads):
    active = 0
    for t in threads:
        if t.is_alive():
            active += 1
    return active

def run_desired_tests(args, ds_registry):
    generate_files(False)
    lock = threading.Lock()
    ds_to_run = ds_registry.get_datasources(args.ds)
    if not ds_to_run:
        print ("Nothing to run.")
        sys.exit(0)

    if len(ds_to_run) > 0:
        delete_output_files(os.getcwd())

    temporary_test_configs = []
    test_threads = []
    error_code = 0
    test_run = 0
    start_time = time.time()
    for ds in ds_to_run:
        ds_info = ds_registry.get_datasource_info(ds)
        if not ds_info:
            continue

        print ("Testing " + ds)

        suite = ds
        run_expr_tests = True if args.logical_only is None and args.logical_pattern is None else False
        run_logical_tests = True if args.expression_only is None and args.expression_pattern is None else False

        if VERBOSE: print("Run expression tests? " + str(run_expr_tests))
        if VERBOSE: print("Run logical tests? " + str(run_logical_tests))

            
        if run_logical_tests:
            #Check if the user wants to run a single test file. If so then create a temporary cfg file to hold that config.
            single_test = get_temporary_logical_test_config(temporary_test_configs, args.logical_pattern, args.tds_pattern, args.test_pattern_exclude, ds_info)
            enqueue_tests(True, ds_info, args, single_test, suite, lock, test_threads, test_run)

        if run_expr_tests:
            #Check if the user wants to run a single test file. If so then create a temporary cfg file to hold that config.
            single_test = get_temporary_expression_test_config(temporary_test_configs, args.expression_pattern, args.tds_pattern, args.test_pattern_exclude, ds_info)
            enqueue_tests(False, ds_info, args, single_test, suite, lock, test_threads, test_run)

    if not test_threads:
        print ("No tests found. Check arguments.")
        sys.exit()

    max_threads, max_sub_threads = get_level_of_parallelization(args, len(test_threads))

    for test_thread in test_threads:
        test_thread.sub_thread_count = max_sub_threads

    for test_thread in test_threads:
        while active_thread_count(test_threads) > max_threads:
            time.sleep(0.5)
        test_thread.daemon = True
        test_thread.start()

    for test_thread in test_threads:
        test_thread.join()

    for test_thread in test_threads:
        if args.noclean:
            print ("Left temp dir: " + test_thread.temp_dir)
        error_code += test_thread.error_code if test_thread.error_code else 0

    print ('\n')
    print ("Total time: " + str(time.time() - start_time))
    print ("Total failed tests " + str(error_code))
    
    return error_code

def main():
    parser, ds_registry, args = init()

    if args.generate:
        print ("Generating config files...")
        generate_files(True)
        print ("Done")
        sys.exit(0)
    elif args.diff:
        #Set verbose so the user sees something from the diff.
        VERBOSE = True
        test_config = TdvtTestConfig(from_args=args)
        run_diff(test_config, args.diff)
        sys.exit(0)
    elif args.run_file:
        sys.exit(run_failed_tests(args.run_file))
    elif args.list_ds is not None:
        print_configurations(ds_registry, args.list_ds)
        sys.exit(0)

    error_code = run_desired_tests(args, ds_registry)

    sys.exit(error_code)

if __name__ == '__main__':
    main()