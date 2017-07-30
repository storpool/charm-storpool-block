from __future__ import print_function

import subprocess
import time

from charms import reactive
from charmhelpers.core import hookenv

from spcharms import repo as sprepo

def rdebug(s):
	with open('/tmp/storpool-charms.log', 'a') as f:
		print('{tm} [block-charm] {s}'.format(tm=time.ctime(), s=s), file=f)

@reactive.when('storpool-config.config-written')
def whee():
	rdebug('wheeeeeee')

	hookenv.status_set('maintenance', 'checking our storpool-repo-add installation')
	policy = sprepo.apt_pkg_policy(['txn-install', 'storpool-config', 'meowmeow'])
	rdebug('got some kind of policy: {p}'.format(p=policy))

	hookenv.status_set('maintenance', 'checking our storpool-config installation')
	lines_b = subprocess.check_output(['/usr/sbin/storpool_confshow', '-n', 'SP_OURID'])
	lines = lines_b.decode().split('\n')
	rdebug('got some kind of output from storpool_confshow -n SP_OURID: {out}'.format(out=lines))

	hookenv.status_set('active', 'so far so good so what')
