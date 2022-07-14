```sh
# Load your OpenStack credentials
source admin-openrc.sh

# Setup refstack-client
./setup_env -p 3 -c $(curl -s https://api.github.com/repos/openstack/tempest/commits/master | jq -r '.sha')
source .venv/bin/activate
refstack-client config --use-test-accounts="etc/accounts.yaml" --overrides \
  "auth.tempest_roles='',compute.fixed_network_name=Tempest,compute-feature-enabled.hostname_fqdn_sanitization=true,compute-feature-enabled.xenapi_apis=true,identity.v3_endpoint_type=public,validation.image_ssh_password=gocubsgo,validation.ssh_shell_prologue='set -eu -o pipefail;',validation.ssh_timeout=120"

# (Errata) Patch Tempest tempurl bug
cd .tempest
git remote add jcmdln https://github.com/jcmdln/tempest
git pull --squash jcmdln master
cd ..

# Install ansible and openstackclient if you don't already have them locally
pip install ansible-core openstackclient

# Prepare the cloud
ansible-playbook setup-cloud.yaml

# Confirm you can run a test
refstack-client test -v -c etc/tempest.conf -- \
  --regex tempest.api.identity.v3.test_tokens.TokensV3Test.test_create_token

# Run refstack-client using the required tests in the 2021.11 list
refstack-client test -v -c etc/tempest.conf --test-list \
  "https://refstack.openstack.org/api/v1/guidelines/2021.11/tests?target=platform&type=required&alias=true&flag=false"

# Upload your results
# https://opendev.org/openinfra/refstack/src/branch/master/doc/source/uploading_private_results.rst
refstack-client upload -v -i ~/.ssh/refstack .tempest/.stestr/<result-id>.json
```
