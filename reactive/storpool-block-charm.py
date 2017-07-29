from __future__ import print_function

import time

from charms import reactive
from charmhelpers.core import hookenv

from spcharms import repo as sprepo

def rdebug(s):
	with open('/tmp/storpool-charms.log', 'a') as f:
		print('{tm} [block-charm] {s}'.format(tm=time.ctime(), s=s), file=f)

@reactive.when('storpool-repo-add.available')
def whee():
	rdebug('wheeeeeee')
	hookenv.status_set('maintenance', 'the charm finalizing the final stuff for the final time')

	policy = sprepo.apt_pkg_policy(['txn-install', 'storpool-config', 'meowmeow'])
	rdebug('got some kind of policy: {p}'.format(p=policy))

	hookenv.status_set('active', 'so far so good so what')
