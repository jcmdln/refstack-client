# Copyright (c) 2015 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#


import atexit
import logging
import os
import re
import requests
import subprocess
import tempfile


class TestListParser(object):

    """This class is for normalizing test lists to match the tests in the
    current Tempest environment.
    """

    def __init__(self, tempest_dir, insecure=False):
        """
        Initialize the TestListParser.

        :param tempest_dir: Absolute path of the Tempest directory.
        :param insecure: Whether https requests, if any, should be insecure.
        """
        self.logger = logging.getLogger(__name__)
        self.tempest_dir = tempest_dir
        self.insecure = insecure

    def _get_tempest_test_ids(self):
        """This does a 'testr list-tests' or 'stestr list' according to
        Tempest version on the Tempest directory in order to get a list
        of full test IDs for the current Tempest environment. Test ID
        mappings are then formed for these tests.
        """
        if os.path.exists(os.path.join(self.tempest_dir, '.stestr.conf')):
            init_cmd = (os.path.join(self.tempest_dir, 'tools/with_venv.sh'),
                        'stestr', 'init')
            subprocess.Popen(init_cmd, stdout=subprocess.PIPE,
                             cwd=self.tempest_dir)
            cmd = (os.path.join(self.tempest_dir, 'tools/with_venv.sh'),
                   'stestr', 'list')
        else:
            cmd = (os.path.join(self.tempest_dir, 'tools/with_venv.sh'),
                   'testr', 'list-tests')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                   cwd=self.tempest_dir)
        (stdout, stderr) = process.communicate()

        if process.returncode != 0:
            self.logger.error(stdout)
            self.logger.error(stderr)
            raise subprocess.CalledProcessError(process.returncode,
                                                ' '.join(cmd))
        try:
            return self._form_test_id_mappings(stdout.split('\n'))
        except TypeError:
            return self._form_test_id_mappings(stdout.decode().split('\n'))

    def _form_test_id_mappings(self, test_list):
        """This takes in a list of full test IDs and forms a dict containing
        base test IDs mapped to their attributes. A full test ID also contains
        test attributes such as '[gate,smoke]'
        Ex:
            'tempest.api.test1': '[gate]'
            'tempest.api.test2': ''
            'tempest.api.test3(some_scenario)': '[smoke,gate]'

        :param test_list: List of full test IDs
        """
        test_mappings = {}
        for testcase in test_list:
            if testcase != "":
                # Search for any strings like '[smoke, gate]' in the test ID.
                match = re.search(r'(\[.*\])', testcase)

                if match:
                    testcase = re.sub(r'\[.*\]', '', testcase)
                    test_mappings[testcase] = match.group(1)
                else:
                    test_mappings[testcase] = ""
        return test_mappings

    def _get_base_test_ids_from_list_file(self, list_location):
        """This takes in a test list file and finds all the base test IDs
        for the tests listed.
        Ex:
            'tempest.test1[gate,id-2]' -> 'tempest.test1'
            'tempest.test2[gate,id-3](scenario)' -> 'tempest.test2(scenario)'

        :param list_location: file path or URL location of list file
        """
        try:
            response = requests.get(list_location,
                                    verify=not self.insecure)
            testcase_list = response.text.split('\n')
            test_mappings = self._form_test_id_mappings(testcase_list)
        # If the location isn't a valid URL, we assume it is a file path.
        except requests.exceptions.MissingSchema:
            try:
                with open(list_location) as data_file:
                    testcase_list = [line.rstrip('\n') for line in data_file]
                test_mappings = self._form_test_id_mappings(testcase_list)
            except Exception:
                self.logger.error("Error reading the passed in test list " +
                                  "file.")
                raise
        except Exception:
            self.logger.error("Error reading the passed in test list file.")
            raise

        return list(test_mappings.keys())

    def _get_full_test_ids(self, tempest_ids, base_ids):
        """This will remake the test ID list with the full IDs of the current
        Tempest environment. The Tempest test ID dict should have the correct
        mappings.

        :param tempest_ids: dict containing test ID mappings
        :param base_ids: list containing base test IDs
        """
        test_list = []
        for test_id in base_ids:
            try:
                attr = tempest_ids[test_id]
                # If the test has a scenario in the test ID, but also has some
                # additional attributes, the attributes need to go before the
                # scenario.
                if '(' in test_id and attr:
                    components = test_id.split('(', 1)
                    test_portion = components[0]
                    scenario = "(" + components[1]
                    test_list.append(test_portion + attr + scenario)
                else:
                    test_list.append(test_id + attr)
            except KeyError:
                self.logger.debug("Test %s not found in Tempest list." %
                                  test_id)
        self.logger.debug("Number of tests: " + str(len(test_list)))
        return test_list

    def _write_normalized_test_list(self, test_ids):
        """Create a temporary file to pass into testr containing a list of test
        IDs that should be tested.

        :param test_ids: list of full test IDs
        """
        temp = tempfile.NamedTemporaryFile(delete=False)
        for test_id in test_ids:
            temp.write(("%s\n" % test_id).encode('utf-8'))
        temp.flush()

        # Register the created file for cleanup.
        atexit.register(self._remove_test_list_file, temp.name)
        return temp.name

    def _remove_test_list_file(self, file_path):
        """Delete the given file.

        :param file_path: string containing the location of the file
        """
        if os.path.isfile(file_path):
            os.remove(file_path)

    def get_normalized_test_list(self, list_location):
        """This will take in the user's test list and will normalize it
        so that the test cases in the list map to actual full test IDS in
        the Tempest environment.

        :param list_location: file path or URL of the test list
        """
        tempest_test_ids = self._get_tempest_test_ids()
        if not tempest_test_ids:
            return None
        base_test_ids = self._get_base_test_ids_from_list_file(list_location)
        full_capability_test_ids = self._get_full_test_ids(tempest_test_ids,
                                                           base_test_ids)
        list_file = self._write_normalized_test_list(full_capability_test_ids)
        return list_file

    def create_include_list(self, list_location):
        """This takes in a test list file, get normalized, and get list of
        include regexes using full qualified test names (one per line).
        Ex:
          'tempest.test1[id-2,gate]' -> tempest.test1\[ # noqa: W605
          'tempest.test2[id-3,smoke](scenario)' -> tempest.test2\[ # noqa: W605
          'tempest.test3[compute,id-4]' -> tempest.test3\[ # noqa: W605

        :param list_location: file path or URL location of list file
        """
        normalized_list = open(self.get_normalized_test_list(list_location),
                               'r').read()
        # Keep the names
        tests_list = [re.sub(r"\[", r"\[", test)
                      for test in re.findall(r".*\[", normalized_list)]

        return self._write_normalized_test_list(tests_list)
