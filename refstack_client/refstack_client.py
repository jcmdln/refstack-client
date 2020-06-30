#!/usr/bin/env python
#
# Copyright (c) 2014 Piston Cloud Computing, Inc. All Rights Reserved.
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


"""
Run Tempest and upload results to RefStack.

This module runs the Tempest test suite on an OpenStack environment given a
Tempest configuration file.

"""

from __future__ import absolute_import

import argparse
import binascii
import hashlib
import itertools
import json
import logging
import os
import subprocess
import time

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat import backends
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config_tempest import main
from config_tempest import constants as C
from keystoneauth1 import exceptions as KE
from openstack import exceptions as OSE

import requests
import requests.exceptions
from six import moves
from six.moves.urllib import parse
from refstack_client.subunit_processor import SubunitProcessor
from refstack_client.list_parser import TestListParser
import yaml


def get_input():
    """
    Wrapper for raw_input. Necessary for testing.
    """
    return moves.input().lower()  # pragma: no cover


def read_accounts_yaml(path):
    """Reads a set of accounts from the specified file"""
    with open(path, 'r') as yaml_file:
        accounts = yaml.load(yaml_file)
    return accounts


class RefstackClient:
    log_format = "%(asctime)s %(name)s:%(lineno)d %(levelname)s %(message)s"

    def __init__(self, args):
        '''Prepare a tempest test against a cloud.'''
        self.logger = logging.getLogger("refstack_client")
        self.console_log_handle = logging.StreamHandler()
        self.console_log_handle.setFormatter(
            logging.Formatter(self.log_format))
        self.logger.addHandler(self.console_log_handle)

        self.args = args
        self.current_dir = os.path.dirname(os.path.realpath(__file__))
        self.refstack_dir = os.path.dirname(self.current_dir)
        self.tempest_dir = os.path.join(self.refstack_dir, '.tempest')

        # set default log level to INFO.
        if self.args.silent:
            self.logger.setLevel(logging.WARNING)
        elif self.args.verbose:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)

    def _prep_test(self):
        '''Prepare a tempest test against a cloud.'''

        # Check that the config file exists.
        if not os.path.isfile(self.args.conf_file):
            self.logger.error("Conf file not valid: %s" % self.args.conf_file)
            exit(1)

        if not os.access(self.args.conf_file, os.R_OK):
            self.logger.error("You do not have read access to: %s"
                              % self.args.conf_file)
            exit(1)

        # Initialize environment variables with config file info
        os.environ["TEMPEST_CONFIG_DIR"] = os.path.abspath(
            os.path.dirname(self.args.conf_file))
        os.environ["TEMPEST_CONFIG"] = os.path.basename(self.args.conf_file)

        # Check that the Tempest directory is an existing directory.
        if not os.path.isdir(self.tempest_dir):
            self.logger.error("Tempest directory given is not a directory or "
                              "does not exist: %s" % self.tempest_dir)
            exit(1)

        self.conf_file = self.args.conf_file
        # Note: SafeConfigParser deprecated on Python 3.2
        # Use ConfigParser directly
        self.conf = moves.configparser.ConfigParser()
        self.conf.read(self.args.conf_file)

    def _prep_upload(self):
        '''Prepare an upload to the RefStack_api'''
        if not os.path.isfile(self.args.file):
            self.logger.error("File not valid: %s" % self.args.file)
            exit(1)

        self.upload_file = self.args.file

    def _get_next_stream_subunit_output_file(self, tempest_dir):
        '''This method reads from the next-stream file in the .testrepository
           or .stestr directory according to Tempest version of the given
           Tempest path. The integer here is the name of the file where subunit
           output will be saved to. It also check if the repository is
           initialized and if not, initialize it'''
        if os.path.exists(os.path.join(self.tempest_dir, '.stestr.conf')):
            sub_dir = '.stestr'
            cmd = 'stestr'
        else:
            sub_dir = '.testrepository'
            cmd = 'testr'
        try:
            if not os.path.exists(os.path.join(tempest_dir, sub_dir)):
                self.logger.debug('No repository found, creating one.')
                os.chdir(tempest_dir)
                process = subprocess.Popen([cmd, 'init'])
                process.communicate()
                os.chdir(self.refstack_dir)

            subunit_file = open(os.path.join(
                                tempest_dir, sub_dir,
                                'next-stream'), 'r').read().rstrip()
        except (IOError, OSError):
            self.logger.debug('The ' + sub_dir + '/next-stream file was not '
                              'found. Assuming subunit results will be stored '
                              'in file 0.')

            # First test stream is saved into $sub_dir/0
            subunit_file = "0"

        return os.path.join(tempest_dir, sub_dir, subunit_file)

    def _get_keystone_config(self, conf_file):
        '''This will get and return the keystone configs from config file.'''
        try:
            # Prefer Keystone V3 API if it is enabled
            auth_version = 'v3'
            if conf_file.has_option('identity', 'auth_version'):
                auth_version = conf_file.get('identity', 'auth_version')

            if auth_version == 'v2':
                auth_url = '%s/tokens' % (conf_file.get('identity', 'uri')
                                          .rstrip('/'))
            elif auth_version == 'v3':
                auth_url = '%s/auth/tokens' % (conf_file.get('identity',
                                               'uri_v3').rstrip('/'))

            domain_name = 'Default'
            if conf_file.has_option('identity', 'domain_name'):
                domain_name = conf_file.get('identity', 'domain_name')
            if conf_file.has_option('auth', 'test_accounts_file'):
                account_file = os.path.expanduser(
                    conf_file.get('auth', 'test_accounts_file'))
                if not os.path.isfile(account_file):
                    self.logger.error(
                        'Accounts file not found: %s' % account_file)
                    exit(1)

                accounts = read_accounts_yaml(account_file)
                if not accounts:
                    self.logger.error('Accounts file %s found, '
                                      'but was empty.' % account_file)
                    exit(1)
                account = accounts[0]
                username = account.get('username')
                password = account.get('password')
                project_id = (account.get('tenant_id') or
                              account.get('project_id'))
                project_name = (account.get('tenant_name') or
                                account.get('project_name'))
                return {'auth_version': auth_version,
                        'auth_url': auth_url,
                        'domain_name': domain_name,
                        'username': username, 'password': password,
                        'tenant_id': project_id, 'tenant_name': project_name,
                        'project_id': project_id, 'project_name': project_name
                        }
            elif conf_file.has_option('identity', 'username'):
                self.logger.warn('Using identity section of tempest config '
                                 'file to specify user credentials is '
                                 'deprecated and won\'t be supported soon. '
                                 'User credentials should be defined in the '
                                 'accounts file as described in the Tempest '
                                 'configuration guide (http://docs.openstack.'
                                 'org/developer/tempest/configuration.html).')

                username = conf_file.get('identity', 'username')
                password = conf_file.get('identity', 'password')

                if self.conf.has_option('identity', 'tenant_id'):
                    project_id = conf_file.get('identity', 'tenant_id')
                elif self.conf.has_option('identity', 'project_id'):
                    project_id = conf_file.get('identity', 'project_id')
                else:
                    project_id = None
                project_name = (conf_file.get('identity', 'tenant_name') or
                                conf_file.get('identity', 'project_name'))
                return {'auth_version': auth_version,
                        'auth_url': auth_url,
                        'domain_name': domain_name,
                        'username': username, 'password': password,
                        'tenant_id': project_id, 'tenant_name': project_name,
                        'project_id': project_id, 'project_name': project_name}
            else:
                self.logger.error('User credentials cannot be found. '
                                  'User credentials should be defined in the '
                                  'accounts file as described in the Tempest '
                                  'configuration guide (http://docs.openstack.'
                                  'org/developer/tempest/configuration.html).')
                exit(1)
        except moves.configparser.Error as e:
            # Most likely a missing section or option in the config file.
            self.logger.error("Invalid Config File: %s" % e)
            exit(1)

    def _generate_keystone_data(self, auth_config):
        '''This will generate data for http post to keystone
        API from auth_config.'''
        auth_version = auth_config['auth_version']
        auth_url = auth_config['auth_url']
        if auth_version == 'v2':
            password_credential = {'username': auth_config['username'],
                                   'password': auth_config['password']}
            if auth_config['tenant_id']:
                data = {
                    'auth': {
                        'tenantId': auth_config['tenant_id'],
                        'passwordCredentials': password_credential
                    }
                }
            else:
                data = {
                    'auth': {
                        'tenantName': auth_config['tenant_name'],
                        'passwordCredentials': password_credential
                    }
                }
            return auth_version, auth_url, data
        elif auth_version == 'v3':
            identity = {
                'methods': ['password'],
                'password': {
                    'user': {
                        'name': auth_config['username'],
                        'domain': {'name': auth_config['domain_name']},
                        'password': auth_config['password']
                    }}}

            data = {
                'auth': {
                    'identity': identity,
                    'scope': {
                        'project': {
                            'name': auth_config['tenant_name'],
                            'domain': {'name': auth_config['domain_name']}
                        }
                    }
                }
            }
            return auth_version, auth_url, data

    def _get_cpid_from_keystone(self, auth_version, auth_url, content):
        '''This will get the Keystone service ID which is used as the CPID.'''
        try:
            headers = {'content-type': 'application/json'}
            response = requests.post(auth_url,
                                     data=json.dumps(content),
                                     headers=headers,
                                     verify=not self.args.insecure)
            rsp = response.json()
            if response.status_code in (200, 203):
                # keystone API v2 response.
                access = rsp['access']
                for service in access['serviceCatalog']:
                    if service['type'] == 'identity':
                        if service['endpoints'][0]['id']:
                            return service['endpoints'][0]['id']
                # Raise a key error if 'identity' was not found so that it
                # can be caught and have an appropriate error displayed.
                raise KeyError
            elif response.status_code == 201:
                # keystone API v3 response.
                token = rsp['token']
                for service in token['catalog']:
                    if service['type'] == 'identity' and service['id']:
                        return service['id']
                # Raise a key error if 'identity' was not found.
                # It will be caught below as well.
                raise KeyError
            else:
                message = ('Invalid request with error '
                           'code: %s. Error message: %s'
                           '' % (rsp['error']['code'],
                                 rsp['error']['message']))
                raise requests.exceptions.HTTPError(message)
            # If a Key or Index Error was raised, one of the expected keys or
            # indices for retrieving the identity service ID was not found.
        except (KeyError, IndexError) as e:
            self.logger.warning('Unable to retrieve CPID from Keystone %s '
                                'catalog. The catalog or the identity '
                                'service endpoint was not '
                                'found.' % auth_version)
        except Exception as e:
            self.logger.warning('Problems retrieving CPID from Keystone '
                                'using %s endpoint (%s) with error (%s)'
                                % (auth_version, auth_url, e))
        return self._generate_cpid_from_endpoint(auth_url)

    def _generate_cpid_from_endpoint(self, endpoint):
        '''This method will md5 hash the hostname of a Keystone endpoint to
           generate a CPID.'''
        self.logger.info('Creating hash from endpoint to use as CPID.')
        url_parts = parse.urlparse(endpoint)
        if url_parts.scheme not in ('http', 'https'):
            raise ValueError('Invalid Keystone endpoint format. Make sure '
                             'the endpoint (%s) includes the URL scheme '
                             '(i.e. http/https).' % endpoint)
        return hashlib.md5(url_parts.hostname.encode('utf-8')).hexdigest()

    def _form_result_content(self, cpid, duration, results):
        '''This method will create the content for the request. The spec at
           'https://opendev.org/osf/refstack/src/branch/master/specs/prior'
           '/implemented/api-v1.md'.
           defines the format expected by the API.'''
        content = {}
        content['cpid'] = cpid
        content['duration_seconds'] = duration
        content['results'] = results
        return content

    def _save_json_results(self, results, path):
        '''Save the output results from the Tempest run as a JSON file'''
        file = open(path, "w+")
        file.write(json.dumps(results, indent=4, separators=(',', ': ')))
        file.close()

    def _user_query(self, q):
        """Ask user a query. Return true if user agreed (yes/y)"""
        if self.args.quiet:
            return True
        try:
            inp = moves.input(q + ' (yes/y): ')
        except KeyboardInterrupt:
            return
        else:
            return inp.lower() in ('yes', 'y')

    def _upload_prompt(self, upload_content):
        if self._user_query('Test results will be uploaded to %s. '
                            'Ok?' % self.args.url):
            self.post_results(self.args.url, upload_content,
                              sign_with=self.args.priv_key)

    def get_passed_tests(self, result_file):
        '''Get a list of tests IDs that passed Tempest from a subunit file.'''
        subunit_processor = SubunitProcessor(result_file)
        results = subunit_processor.process_stream()
        return results

    def post_results(self, url, content, sign_with=None):
        '''Post the combined results back to the refstack server.'''
        endpoint = '%s/v1/results/' % url
        headers = {'Content-type': 'application/json'}
        data = json.dumps(content)
        self.logger.debug('API request content: %s ' % content)
        if sign_with:
            with open(sign_with) as private_key_file:
                try:
                    private_key = serialization.load_pem_private_key(
                        private_key_file.read().encode('utf-8'),
                        password=None,
                        backend=backends.default_backend())
                except (IOError, UnsupportedAlgorithm, ValueError) as e:
                    self.logger.info('Error during upload key pair %s' %
                                     private_key_file)
                    self.logger.exception(e)
                    return
            signer = private_key.signer(padding.PKCS1v15(), hashes.SHA256())
            signer.update(data.encode('utf-8'))
            signature = binascii.b2a_hex(signer.finalize())
            pubkey = private_key.public_key().public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH)

            headers['X-Signature'] = signature
            headers['X-Public-Key'] = pubkey
        try:
            response = requests.post(endpoint,
                                     data=data,
                                     headers=headers,
                                     verify=not self.args.insecure)
            self.logger.info(endpoint + " Response: " + str(response.text))
        except Exception as e:
            self.logger.info('Failed to post %s - %s ' % (endpoint, e))
            self.logger.exception(e)
            return

        if response.status_code == 201:
            resp = response.json()
            print('Test results uploaded!\nURL: %s' % resp.get('url', ''))

    def generate_tempest_config(self):
        '''Generate tempest.conf for a deployed OpenStack Cloud.'''
        start_time = time.time()

        # Write tempest.conf in refstack_client/etc folder
        if not self.args.out:
            config_path = os.path.join(self.refstack_dir, 'etc/tempest.conf')
        else:
            config_path = self.args.out
        self.logger.info("Generating in %s" % config_path)

        # Generate Tempest configuration
        try:
            cloud_creds = main.get_cloud_creds(self.args)
        except KE.MissingRequiredOptions as e:
            self.logger.error("Credentials are not sourced - %s" % e)
        except OSE.ConfigException:
            self.logger.error("Named cloud %s was not found"
                              % self.args.os_cloud)

        # tempestconf arguments
        kwargs = {'non_admin': True,
                  'test_accounts': self.args.test_accounts,
                  'image_path': self.args.image,
                  'network_id': self.args.network_id,
                  'out': config_path,
                  'cloud_creds': cloud_creds}

        # Look for extra overrides to be replaced in tempest.conf
        # (TODO:chkumar246) volume-feature-enabled.api_v2=True is deprecated
        # in ROCKY release, but it is required for interop tests and it is out
        # of scope of python-tempestconf, adding it hardcoded here as a extra
        # overrides.
        cinder_overrides = "volume-feature-enabled.api_v2=True"
        overrides_format = cinder_overrides.replace('=', ',').split(',')
        overrides = []
        if self.args.overrides:
            overrides = self.args.overrides.replace('=', ',').split(',')
            if cinder_overrides not in self.args.overrides:
                overrides = overrides + overrides_format
        else:
            overrides = overrides_format
        kwargs.update({'overrides': main.parse_overrides(overrides)})

        # Generate accounts.yaml if accounts.file is not given
        if not self.args.test_accounts:
            account_file = os.path.join(self.refstack_dir, 'etc/accounts.yaml')
            kwargs.update({'create_accounts_file': account_file})
            self.logger.info('Account file will be generated at %s.'
                             % account_file)

        # Generate tempest.conf
        main.config_tempest(**kwargs)

        if os.path.isfile(config_path):
            end_time = time.time()
            elapsed = end_time - start_time
            duration = int(elapsed)
            self.logger.info('Tempest Configuration successfully generated '
                             'in %s second at %s' % (duration, config_path))
        else:
            try:
                import config_tempest # noqa
                self.logging.warning('There is an error in syntax, please '
                                     'check $ refstack-client config -h')
            except ImportError:
                self.logger.warning('Please make sure python-tempestconf'
                                    'python package is installed')

    def test(self):
        '''Execute Tempest test against the cloud.'''
        self._prep_test()
        results_file = self._get_next_stream_subunit_output_file(
            self.tempest_dir)
        keystone_config = self._get_keystone_config(self.conf)
        auth_version, auth_url, content = \
            self._generate_keystone_data(keystone_config)
        cpid = self._get_cpid_from_keystone(auth_version, auth_url, content)

        self.logger.info("Starting Tempest test...")
        start_time = time.time()

        # Run tempest run command, conf file specified at _prep_test method
        # Use virtual environment (wrapper script)
        # Run the tests serially if parallel not enabled (--serial).
        wrapper = os.path.join(self.tempest_dir, 'tools', 'with_venv.sh')
        cmd = [wrapper, 'tempest', 'run']
        if not self.args.parallel:
            cmd.append('--serial')
        # TODO(mkopec) until refstack-client uses tempest tag which contains
        # the following change https://review.openstack.org/#/c/641349/
        # let's hardcode concurrency here, when the change is merged, the
        # value of concurrency will be set as default in tempest so the
        # following two lines can be deleted
        cmd.append('--concurrency')
        cmd.append('0')
        # If a test list was specified, have it take precedence.
        if self.args.test_list:
            self.logger.info("Normalizing test list...")
            parser = TestListParser(os.path.abspath(self.tempest_dir),
                                    insecure=self.args.insecure)
            # get whitelist
            list_file = parser.create_whitelist(self.args.test_list)
            if list_file:
                if os.path.getsize(list_file) > 0:
                    cmd += ('--whitelist_file', list_file)
                else:
                    self.logger.error("Test list is either empty or no valid "
                                      "test cases for the tempest "
                                      "environment were found.")
                    exit(1)
            else:
                self.logger.error("Error normalizing passed in test list.")
                exit(1)
        elif 'arbitrary_args' in self.args:
            # Additional arguments for tempest run
            # otherwise run  all Tempest API tests.
            # keep usage(-- testCaseName)
            tmp = self.args.arbitrary_args[1:]
            if tmp:
                cmd += (tmp if tmp[0].startswith('-') else ['--regex'] + tmp)
        else:
            cmd += ['--regex', "tempest.api"]

        # If there were two verbose flags, show tempest results.
        if self.args.verbose > 0:
            stderr = None
        else:
            # Suppress tempest results output. Note that testr prints
            # results to stderr.
            stderr = open(os.devnull, 'w')

        # Execute the tempest run command in a subprocess.
        os.chdir(self.tempest_dir)
        process = subprocess.Popen(cmd, stderr=stderr)
        process.communicate()
        os.chdir(self.refstack_dir)

        # If the subunit file was created, then test cases were executed via
        # tempest run and there is test output to process.
        if os.path.isfile(results_file):
            end_time = time.time()
            elapsed = end_time - start_time
            duration = int(elapsed)

            self.logger.info('Tempest test complete.')
            self.logger.info('Subunit results located in: %s' % results_file)

            results = self.get_passed_tests(results_file)
            self.logger.info("Number of passed tests: %d" % len(results))

            content = self._form_result_content(cpid, duration, results)

            if self.args.result_tag:
                file_name = os.path.basename(results_file)
                directory = os.path.dirname(results_file)
                file_name = '-'.join([self.args.result_tag, file_name])
                results_file = os.path.join(directory, file_name)

            json_path = results_file + ".json"
            self._save_json_results(content, json_path)
            self.logger.info('JSON results saved in: %s' % json_path)

            # If the user specified the upload argument, then post
            # the results.
            if self.args.upload:
                self.post_results(self.args.url, content,
                                  sign_with=self.args.priv_key)
        else:
            msg1 = ("tempest run command did not generate a results file "
                    "under the Refstack os.path.dirname(results_file) "
                    "directory. Review command and try again.")
            msg2 = ("Problem executing tempest run command. Results file "
                    "not generated hence no file to upload. Review "
                    "arbitrary arguments.")
            if process.returncode != 0:
                self.logger.warning(msg1)
            if self.args.upload:
                self.logger.error(msg2)

        return process.returncode

    def upload(self):
        '''Perform upload to RefStack URL.'''
        self._prep_upload()
        json_file = open(self.upload_file)
        json_data = json.load(json_file)
        json_file.close()
        self._upload_prompt(json_data)

    def upload_subunit(self):
        '''Perform upload to RefStack URL from a subunit file.'''
        self._prep_upload()

        cpid = self._generate_cpid_from_endpoint(self.args.keystone_endpoint)
        # Forgo the duration for direct subunit uploads.
        duration = 0

        # Formulate JSON from subunit
        results = self.get_passed_tests(self.upload_file)
        self.logger.info('Number of passed tests in given subunit '
                         'file: %d ' % len(results))

        content = self._form_result_content(cpid, duration, results)
        self._upload_prompt(content)

    def yield_results(self, url, start_page=1,
                      start_date='', end_date='', cpid=''):
        endpoint = '%s/v1/results/' % url
        headers = {'Content-type': 'application/json'}
        for page in itertools.count(start_page):
            params = {'page': page}
            for param in ('start_date', 'end_date', 'cpid'):
                if locals()[param]:
                    params.update({param: locals()[param]})
            try:
                resp = requests.get(endpoint, headers=headers, params=params,
                                    verify=not self.args.insecure)
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                self.logger.info('Failed to list %s - %s ' % (endpoint, e))
                raise StopIteration
            else:
                resp = resp.json()
                results = resp.get('results', [])
                yield results
                if resp['pagination']['total_pages'] == page:
                    raise StopIteration

    def list(self):
        """Retrieve list with last test results from RefStack."""
        results = self.yield_results(self.args.url,
                                     start_date=self.args.start_date,
                                     end_date=self.args.end_date)
        for page_of_results in results:
            for r in page_of_results:
                print('%s - %s' % (r['created_at'], r['url']))
            try:
                moves.input('Press Enter to go to next page...')
            except KeyboardInterrupt:
                return

    def _sign_pubkey(self):
        """Generate self signature for public key"""
        private_key_file = self.args.priv_key_to_sign
        try:
            with open(private_key_file) as pkf:
                private_key = serialization.load_pem_private_key(
                    pkf.read().encode('utf-8'),
                    password=None,
                    backend=backends.default_backend())
        except (IOError, UnsupportedAlgorithm, ValueError) as e:
            self.logger.error('Error reading private key %s'
                              '' % private_key_file)
            self.logger.exception(e)
            return

        public_key_file = '.'.join((private_key_file, 'pub'))
        try:
            with open(public_key_file) as pkf:
                pub_key = pkf.read()
        except IOError:
            self.logger.error('Public key file %s not found. '
                              'Public key is generated from private one.'
                              '' % public_key_file)
            pub_key = private_key.public_key().public_bytes(
                serialization.Encoding.OpenSSH,
                serialization.PublicFormat.OpenSSH)

        signer = private_key.signer(padding.PKCS1v15(), hashes.SHA256())
        signer.update('signature'.encode('utf-8'))
        signature = binascii.b2a_hex(signer.finalize())
        return pub_key, signature

    def self_sign(self):
        """Generate signature for public key."""
        pub_key, signature = self._sign_pubkey()
        print('Public key:\n%s\n' % pub_key)
        print('Self signature:\n%s\n' % signature)


def parse_cli_args(args=None):

    usage_string = ('refstack-client [-h] <ARG> ...\n\n'
                    'To see help on specific argument, do:\n'
                    'refstack-client <ARG> -h')

    parser = argparse.ArgumentParser(
        description='RefStack-client arguments',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        usage=usage_string
    )

    subparsers = parser.add_subparsers(help='Available subcommands.')

    # Arguments that go with all subcommands.
    shared_args = argparse.ArgumentParser(add_help=False)
    group = shared_args.add_mutually_exclusive_group()
    group.add_argument('-s', '--silent',
                       action='store_true',
                       help='Suppress output except warnings and errors.')

    group.add_argument('-v', '--verbose',
                       action='count',
                       help='Show verbose output.')

    shared_args.add_argument('-y',
                             action='store_true',
                             dest='quiet',
                             required=False,
                             help='Assume Yes to all prompt queries')

    # Arguments that go with network-related  subcommands (test, list, etc.).
    network_args = argparse.ArgumentParser(add_help=False)
    network_args.add_argument('--url',
                              action='store',
                              required=False,
                              default=os.environ.get(
                                  'REFSTACK_URL',
                                  'https://refstack.openstack.org/api'),
                              type=str,
                              help='RefStack API URL to upload results to. '
                                   'Defaults to env[REFSTACK_URL] or '
                                   'https://refstack.openstack.org/'
                                   'api if it is not set '
                                   '(--url http://localhost:8000).')

    network_args.add_argument('-k', '--insecure',
                              action='store_true',
                              dest='insecure',
                              required=False,
                              help='Skip SSL checks while interacting '
                                   'with RefStack API and Keystone endpoint')

    network_args.add_argument('-i', '--sign',
                              type=str,
                              required=False,
                              dest='priv_key',
                              help='Path to private RSA key. '
                                   'OpenSSH RSA keys format supported')

    # Upload command
    parser_upload = subparsers.add_parser(
        'upload', parents=[shared_args, network_args],
        help='Upload an existing result JSON file.'
    )

    parser_upload.add_argument('file',
                               type=str,
                               help='Path of JSON results file.')

    parser_upload.set_defaults(func="upload")

    # Upload-subunit command
    parser_subunit_upload = subparsers.add_parser(
        'upload-subunit', parents=[shared_args, network_args],
        help='Upload results from a subunit file.'
    )

    parser_subunit_upload.add_argument('file',
                                       type=str,
                                       help='Path of subunit file.')

    parser_subunit_upload.add_argument('--keystone-endpoint',
                                       action='store',
                                       required=True,
                                       dest='keystone_endpoint',
                                       type=str,
                                       help='The Keystone URL of the cloud '
                                            'the subunit results belong to. '
                                            'This is used to generate a Cloud '
                                            'Provider ID.')

    parser_subunit_upload.set_defaults(func="upload_subunit")

    # Config Command
    parser_config = subparsers.add_parser(
        'config', parents=[shared_args, network_args],
        help='Generate tempest.conf for a cloud')

    parser_config.add_argument('--use-test-accounts',
                               action='store',
                               required=False,
                               dest='test_accounts',
                               type=str,
                               help='Path of the accounts.yaml file.')

    parser_config.add_argument('--network-id',
                               action='store',
                               required=False,
                               dest='network_id',
                               help='The ID of an existing network in our '
                                    'openstack instance with external '
                                    'connectivity')

    parser_config.add_argument('--image',
                               action='store',
                               required=False,
                               dest='image',
                               default=C.DEFAULT_IMAGE,
                               help='An image name chosen from `$ openstack '
                                    'image list` or a filepath/URL of an '
                                    'image to be uploaded to glance and set '
                                    'as a reference to be used by tests. The '
                                    'name of the image is the leaf name of '
                                    'the path. Default is %s'
                                    % C.DEFAULT_IMAGE)

    parser_config.add_argument('--out',
                               action='store',
                               required=False,
                               dest='out',
                               help='File path to write tempest.conf')

    parser_config.add_argument('--os-cloud',
                               action='store',
                               required=False,
                               dest='os_cloud',
                               help='Named cloud to connect to.')

    parser_config.add_argument('--overrides',
                               action='store',
                               required=False,
                               dest='overrides',
                               help='Comma seperated values which needs to be'
                                    'overridden in tempest.conf.'
                                    'Example --overrides'
                                    'compute.image_ref=<value>,'
                                    'compute.flavor_ref=<value>')

    parser_config.set_defaults(func='generate_tempest_config')

    # Test command
    parser_test = subparsers.add_parser(
        'test', parents=[shared_args, network_args],
        help='Run Tempest against a cloud.')

    parser_test.add_argument('-c', '--conf-file',
                             action='store',
                             required=True,
                             dest='conf_file',
                             type=str,
                             help='Path of the Tempest configuration file to '
                                  'use.')

    parser_test.add_argument('-r', '--result-file-tag',
                             action='store',
                             required=False,
                             dest='result_tag',
                             type=str,
                             help='Specify a string to prefix the result '
                                  'file with to easier distinguish them. ')

    parser_test.add_argument('--test-list',
                             action='store',
                             required=False,
                             dest='test_list',
                             type=str,
                             help='Specify the file path or URL of a test '
                                  'list text file. This test list will '
                                  'contain specific test cases that should '
                                  'be tested.')

    parser_test.add_argument('-u', '--upload',
                             action='store_true',
                             required=False,
                             help='After running Tempest, upload the test '
                                  'results to the default RefStack API server '
                                  'or the server specified by --url.')

    parser_test.add_argument('-p', '--parallel',
                             action='store_true',
                             required=False,
                             help='Run the tests in parallel. Note this '
                                  'requires multiple users/projects in '
                                  'accounts.yaml.')

    # This positional argument will allow arbitrary arguments to be passed in
    # with the usage of '--'.
    parser_test.add_argument('arbitrary_args',
                             nargs=argparse.REMAINDER,
                             help='After the first "--", you can pass '
                                  'arbitrary arguments to the tempest run '
                                  'runner. This can be used for running '
                                  'specific test cases or test lists. '
                                  'Some examples are: -- --regex '
                                  'tempest.api.compute.images.'
                                  'test_list_image_filters OR '
                                  '-- --whitelist_file /tmp/testid-list.txt')
    parser_test.set_defaults(func="test")

    # List command
    parser_list = subparsers.add_parser(
        'list', parents=[shared_args, network_args],
        help='List last results from RefStack')
    parser_list.add_argument('--start-date',
                             required=False,
                             dest='start_date',
                             type=str,
                             help='Specify a date for start listing of '
                                  'test results '
                                  '(e.g. --start-date "2015-04-24 01:23:56").')
    parser_list.add_argument('--end-date',
                             required=False,
                             dest='end_date',
                             type=str,
                             help='Specify a date for end listing of '
                                  'test results '
                                  '(e.g. --end-date "2015-04-24 01:23:56").')
    parser_list.set_defaults(func='list')

    # Sign command
    parser_sign = subparsers.add_parser(
        'sign', parents=[shared_args],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        help='Generate signature for public key.')
    parser_sign.add_argument('priv_key_to_sign',
                             type=str,
                             help='Path to private RSA key. '
                                  'OpenSSH RSA keys format supported')

    parser_sign.set_defaults(func='self_sign')

    return parser.parse_args(args=args)


def entry_point():
    args = parse_cli_args()
    test = RefstackClient(args)
    raise SystemExit(getattr(test, args.func)())
