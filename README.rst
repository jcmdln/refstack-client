===============
RefStack Client
===============

RefStack-client team and repository tags
########################################


.. image:: https://governance.openstack.org/tc/badges/refstack-client.svg
    :target: https://governance.openstack.org/tc/reference/tags/index.html


Overview
########

refstack-client is a command line utility that allows you to execute Tempest
test runs based on configurations you specify.  When finished running Tempest
it can send the passed test data to a RefStack API server.

Environment setup
#################

We've created an "easy button" for Ubuntu, Centos, RHEL and openSUSE.

1. Make sure you have ``git`` installed
2. Get the refstack client: ``git clone https://opendev.org/osf/refstack-client.git``
3. Go into the refstack-client directory: ``cd refstack-client``
4. Run the "easy button" setup: ``./setup_env``

   **Options:**

   a. -c option allows to specify SHA of commit or branch in Tempest repository
   which will be installed.

   b. -t option allows to specify tag in Tempest repository which will be installed.
   For example: execute ``./setup_env -t tags/3`` to install Tempest tag-3.

   c. By default, Tempest will be installed from commit
   8316f962c52b01edc5be466b18e54904e2a1248a (Sept, 2018).

Usage
#####

1. Prepare a tempest configuration file that is customized to your cloud
   environment. Samples of minimal Tempest configurations are provided in
   the ``etc`` directory in ``tempest.conf.sample`` and ``accounts.yaml.sample``.
   Note that these samples will likely need changes or additional information
   to work with your cloud.

   Note: Use Tempest Pre-Provisioned credentials_ to provide user test accounts. ::

.. _credentials: https://docs.openstack.org/tempest/latest/configuration.html#pre-provisioned-credentials

2. Go into the refstack-client directory::

       cd ~/refstack-client

3. Source to use the correct Python environment::

       source .venv/bin/activate

4. Generate tempest.conf using refstack-client::

       refstack-client config --use-test-accounts <path to account file>

   The above command will create the tempest.conf in `etc` folder.

   Note: If account file is not available, then:
   * Source the keystonerc file containing cloud credentials and run::

         refstack-client config

     It will create accounts.yaml and temepst.conf file in `etc` folder.

5. Validate your setup by running a short test::

       refstack-client test -c <Path of the tempest configuration file to use> -v -- --regex tempest.api.identity.v3.test_tokens.TokensV3Test.test_create_token

6. Run tests.

   To run the entire API test set::

       refstack-client test -c <Path of the tempest configuration file to use> -v

   To run only those tests specified in an OpenStack Powered (TM) Guideline::

       refstack-client test -c <Path of the tempest configuration file to use> -v --test-list <Absolute path  of test list>

   For example::

       refstack-client test -c ~/tempest.conf -v --test-list "https://refstack.openstack.org/api/v1/guidelines/2018.02/tests?target=platform&type=required&alias=true&flag=false"

   This will run only the test cases required by the 2018.02 guidelines
   that have not been flagged.

   **Note:**

   a. Adding the ``-v`` option will show the Tempest test result output.
   b. Adding the ``--upload`` option will have your test results be uploaded to the
      default RefStack API server or the server specified by ``--url``.
   c. Adding the ``--test-list`` option will allow you to specify the file path or URL of
      a test list text file. This test list should contain specific test cases that
      should be tested. Tests lists passed in using this argument will be normalized
      with the current Tempest environment to eliminate any attribute mismatches.
   d. Adding the ``--url`` option will allow you to change where test results should
      be uploaded.
   e. Adding the ``-r`` option with a string will prefix the JSON result file with the
      given string (e.g. ``-r my-test`` will yield a result file like
      'my-test-0.json').
   f. Adding ``--`` enables you to pass arbitrary arguments to tempest run.
      After the first ``--``, all other subsequent arguments will be passed to
      tempest run as is. This is mainly used for quick verification of the
      target test cases. (e.g. ``-- --regex tempest.api.identity.v2.test_token``)
   g. If you have provisioned multiple user/project accounts you can run parallel
      test execution by enabling the ``--parallel`` flag.

   Use ``refstack-client test --help`` for the full list of arguments.

6. Upload your results.

   If you previously ran a test with refstack-client without the ``--upload``
   option, you can later upload your results to a RefStack API server
   with your digital signature. By default, the results are private and you can
   decide to share or delete the results later.

   Following is the command to upload your result::

       refstack-client upload <Path of results file> -i <path-to-private-key>

   The results file is a JSON file generated by refstack-client when a test has
   completed. This is saved in .tempest/.stestr. When you use the
   ``upload`` command, you can also override the RefStack API server uploaded to
   with the ``--url`` option.

   Alternatively, you can use the ``upload-subunit`` command to upload results
   using an existing subunit file. This requires that you pass in the Keystone
   endpoint URL for the cloud that was tested to generate the subunit data::

       refstack-client upload-subunit \
         --keystone-endpoint http://some.url:5000/v3 <Path of subunit file> \
         -i <path-to-private-key>

   Intructions for uploading data with signature can be found at
   https://opendev.org/osf/refstack/src/branch/master/doc/source/uploading_private_results.rst

7. Create a JSON web token to use for authentication to your privately
   uploaded data

   In order to authenticate to the refstack-server to which you have uploaded
   your data, you will need to generate a JSON webtoken. To generate a valid
   token, use the command::

       jwt --key="$( cat %path to private key% )" --alg=RS256 user_openid=%openstackid% exp=+100500

   To test authentication in the API, use the command::

       curl -k --header "Authorization: Bearer %token%" https://localhost.org/v1/profile

8. List uploaded test set.

   You can list previously uploaded data from a RefStack API server by using
   the following command::

       refstack-client list --url <URL of the RefStack API server>


Tempest hacking
###############

By default, refstack-client installs Tempest into the ``.tempest`` directory.
If you're interested in working with Tempest directly for debugging or
configuration, you can activate a working Tempest environment by
switching to that directory and using the installed dependencies.

1. ``cd .tempest``
2. ``source ./.venv/bin/activate``
   and run tests manually with ``tempest run``.

This will make the entire Tempest environment available for you to run,
including ``tempest run``.
